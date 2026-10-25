import json
from pathlib import Path
import stat
import sys

import pytest

from marking_erp import nk_pilot_cli
from marking_erp.nk_pilot import PilotError, capture_pilot
from marking_erp.nk_read_cli import check_capture
from test_nk_read_contract import WINDOW, details, listing
from test_nk_read_transport import TOKEN, client, response


def run(reader, path, **kwargs):
    return capture_pilot(reader, environment='sandbox', window=WINDOW, output=path, **kwargs)


def load(path, name='run.json'):
    return json.loads((path / name).read_bytes())


def test_complete_capture_roundtrips_offline_checker_preserves_bytes_and_private_modes(tmp_path):
    output = tmp_path / 'pilot'
    bodies = [listing([1]), details([1])]
    with client(lambda request: response(bodies.pop(0))) as reader:
        result = run(reader, output)
    checked = check_capture(output / 'manifest.json')
    assert result['state'] == 'complete' and checked['coverage'] == 'bounded_pass'
    assert result['card_count'] == 1 and result['error'] is None
    assert (output / 'list-0.json').read_bytes() == listing([1])
    assert (output / 'details-0.json').read_bytes() == details([1])
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    assert len(load(output)['received']) == 2
    assert all(TOKEN.encode() not in path.read_bytes() for path in output.iterdir())
    assert result['atomic_snapshot'] is result['authenticity_verified'] is result['erp_imported'] is False


def test_page_limit_keeps_a_valid_incomplete_capture_and_stops_network(tmp_path):
    requests = []
    ids = list(range(1, 26))
    with client(lambda request: requests.append(request) or response(
            listing(ids, total=26) if len(requests) == 1 else details(ids))) as reader:
        result = run(reader, tmp_path / 'pilot')
    assert result['state'] == 'page_limit' and len(requests) == 2
    assert check_capture(tmp_path / 'pilot/manifest.json')['coverage'] == 'incomplete'


def test_multi_page_run_last_limit_reduced_and_checker_agrees(tmp_path):
    requests = []
    ids = list(range(1, 26))
    bodies = [listing(ids, total=26), details(ids),
              listing([26], total=26, offset=25, limit=1), details([26])]
    with client(lambda request: requests.append(request) or response(bodies.pop(0))) as reader:
        result = run(reader, tmp_path / 'pilot', max_pages=2)
    assert result['state'] == 'complete' and result['card_count'] == 26
    assert requests[2].url.params['limit'] == '1'
    assert check_capture(tmp_path / 'pilot/manifest.json')['page_count'] == 2


def test_empty_capture_no_details_file(tmp_path):
    with client(lambda request: response(listing([]))) as reader:
        result = run(reader, tmp_path / 'pilot')
    assert result['state'] == 'complete' and result['card_count'] == 0
    assert load(tmp_path / 'pilot', 'manifest.json')['pages'][0]['details'] is None
    assert not (tmp_path / 'pilot/details-0.json').exists()


def test_bad_http200_body_saved_as_evidence_but_never_committed_to_manifest(tmp_path):
    body = b'{"invalid":"contract"}'
    with client(lambda request: response(body)) as reader:
        result = run(reader, tmp_path / 'pilot')
    assert result['state'] == 'failed' and result['error']['code'] == 'response_envelope'
    assert (tmp_path / 'pilot/list-0.json').read_bytes() == body
    assert load(tmp_path / 'pilot', 'manifest.json')['pages'] == []
    assert load(tmp_path / 'pilot')['received'][0]['size'] == len(body)
    assert check_capture(tmp_path / 'pilot/manifest.json')['coverage'] == 'incomplete'


def test_detail_http_failure_preserves_listing_and_status_without_upstream_body(tmp_path):
    requests = []
    with client(lambda request: requests.append(request) or (
            response(listing([1])) if len(requests) == 1 else response(TOKEN.encode(), status=403))) as reader:
        result = run(reader, tmp_path / 'pilot')
    assert result['error']['status'] == 403 and not result['error']['retryable']
    assert result['state'] == 'failed' and result['page_count'] == 0
    assert len(requests) == 2 and (tmp_path / 'pilot/list-0.json').exists()
    assert all(TOKEN.encode() not in path.read_bytes() for path in (tmp_path / 'pilot').iterdir())


def test_byte_budget_stops_before_writing_details_preserving_partial(tmp_path):
    with client(lambda request: response(listing([1]) if request.url.path.endswith('product-list') else details([1]))) as reader:
        result = run(reader, tmp_path / 'pilot', max_bytes=len(listing([1])))
    assert result['state'] == 'failed' and result['error']['code'] == 'nk_capture_byte_budget'
    assert result['saved_body_bytes'] == len(listing([1]))
    assert not (tmp_path / 'pilot/details-0.json').exists()
    assert check_capture(tmp_path / 'pilot/manifest.json')['coverage'] == 'incomplete'


def test_ctrl_c_preserves_previous_committed_page_and_marks_interrupted(tmp_path):
    ids = list(range(1, 26))
    bodies = [listing(ids, total=26), details(ids)]

    def handler(request):
        if not bodies:
            raise KeyboardInterrupt
        return response(bodies.pop(0))

    with client(handler) as reader:
        result = run(reader, tmp_path / 'pilot', max_pages=2)
    assert result['state'] == 'interrupted' and result['page_count'] == 1
    assert check_capture(tmp_path / 'pilot/manifest.json')['card_count'] == 25


def test_existing_and_symlink_paths_never_overwritten_or_requested(tmp_path):
    requests = []
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'sentinel').write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(existing, target_is_directory=True)
    for output in [existing, link, link / 'new']:
        with client(lambda r: requests.append(r)) as reader:
            with pytest.raises(PilotError):
                run(reader, output)
    assert requests == [] and (existing / 'sentinel').read_text() == 'keep'
    assert not (existing / 'new').exists()


@pytest.mark.parametrize('kwargs', [{'max_pages': 0}, {'max_pages': 401}, {'max_pages': True},
                                   {'max_bytes': 0}, {'max_bytes': 256 * 1024 * 1024 + 1}])
def test_invalid_budgets_do_not_create_files_or_request(tmp_path, kwargs):
    requests = []
    with client(lambda r: requests.append(r)) as reader:
        with pytest.raises(PilotError):
            run(reader, tmp_path / 'pilot', **kwargs)
    assert not (tmp_path / 'pilot').exists() and requests == []


@pytest.mark.parametrize('output', [Path('relative'), Path('/'), Path(__file__).parent / 'pilot',
                                  Path('/tmp/../pilot')])
def test_output_must_be_absolute_outside_project_without_parent_segments(output):
    with pytest.raises(PilotError, match='nk_private_output_path'):
        from marking_erp.nk_pilot import validate_output
        validate_output(output)


def arguments(path):
    return ['nk-pilot', '--environment', 'sandbox', '--from-date', WINDOW.from_date,
            '--to-date', WINDOW.to_date, '--output', str(path)]


def test_cli_hidden_token_complete_and_offline_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path / 'pilot'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(nk_pilot_cli.getpass, 'getpass', lambda _: TOKEN)
    monkeypatch.setattr(nk_pilot_cli, 'NkReadClient', lambda **kwargs: client(lambda _: response(listing([]))))
    with pytest.raises(SystemExit) as caught:
        nk_pilot_cli.main()
    output = capsys.readouterr()
    assert caught.value.code == 0 and TOKEN not in output.out + output.err
    assert json.loads(output.out)['state'] == 'complete'
    assert check_capture(tmp_path / 'pilot/manifest.json')['coverage'] == 'bounded_pass'


def test_cli_non_tty_never_prompts_or_connects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path / 'pilot'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(nk_pilot_cli.getpass, 'getpass', lambda _: pytest.fail('must not prompt'))
    monkeypatch.setattr(nk_pilot_cli, 'NkReadClient', lambda **kwargs: pytest.fail('must not connect'))
    with pytest.raises(SystemExit) as caught:
        nk_pilot_cli.main()
    assert caught.value.code == 1 and capsys.readouterr().err == 'nk_token_requires_tty\n'
    assert not (tmp_path / 'pilot').exists()


def test_cli_unknown_token_argument_not_reflected_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path / 'pilot') + ['--token', TOKEN])
    with pytest.raises(SystemExit) as caught:
        nk_pilot_cli.main()
    output = capsys.readouterr()
    assert caught.value.code == 2 and TOKEN not in output.out + output.err


def test_failed_manifest_commit_preserves_previous_manifest_and_raw_files(tmp_path, monkeypatch):
    from marking_erp import nk_pilot
    replace = nk_pilot.os.replace
    commits = []

    def fail_commit(source, target, **kwargs):
        if target == 'manifest.json':
            commits.append(target)
            if len(commits) == 2:
                raise OSError('synthetic disk failure')
        return replace(source, target, **kwargs)

    monkeypatch.setattr(nk_pilot.os, 'replace', fail_commit)
    with client(lambda request: response(listing([1]) if request.url.path.endswith('product-list') else details([1]))) as reader:
        result = run(reader, tmp_path / 'pilot')
    assert result['state'] == 'failed' and result['error']['code'] == 'nk_capture_write'
    assert check_capture(tmp_path / 'pilot/manifest.json')['coverage'] == 'incomplete'
    assert (tmp_path / 'pilot/details-0.json').exists()
    assert not list((tmp_path / 'pilot').glob('.pending-*'))


@pytest.mark.parametrize('mode,expected', [('partial', 2), ('failure', 1), ('interrupt', 130)])
def test_cli_partial_failed_interrupted_exit_codes(tmp_path, monkeypatch, capsys, mode, expected):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path / 'pilot'))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(nk_pilot_cli.getpass, 'getpass', lambda _: TOKEN)
    calls = []

    def handler(request):
        calls.append(request)
        if mode == 'interrupt':
            raise KeyboardInterrupt
        if mode == 'failure':
            return response(TOKEN.encode(), status=403)
        ids = list(range(1, 26))
        return response(listing(ids, total=26) if len(calls) == 1 else details(ids))

    monkeypatch.setattr(nk_pilot_cli, 'NkReadClient', lambda **kwargs: client(handler))
    with pytest.raises(SystemExit) as caught:
        nk_pilot_cli.main()
    output = capsys.readouterr()
    assert caught.value.code == expected and TOKEN not in output.out + output.err
    assert json.loads(output.out)['coverage'] == 'incomplete'


def test_cli_invalid_budget_never_prompts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path / 'pilot') + ['--max-pages', '0'])
    monkeypatch.setattr(nk_pilot_cli.getpass, 'getpass', lambda _: pytest.fail('must not prompt'))
    with pytest.raises(SystemExit) as caught:
        nk_pilot_cli.main()
    assert caught.value.code == 1 and capsys.readouterr().err == 'nk_page_budget\n'
