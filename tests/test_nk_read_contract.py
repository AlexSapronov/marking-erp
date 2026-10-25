"""Synthetic contract regressions; never evidence of a productive NK pilot."""
import hashlib
import json
import os
import sys

import pytest

from marking_erp.nk_read_contract import ContractError, PageRequest, ReadPass, Window, check_page, decode_json
from marking_erp.nk_read_cli import check_capture, main

WINDOW = Window('2026-10-01 00:00:00', '2026-10-25 00:00:00')


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def listing(ids, total=None, offset=0, limit=25, **overrides):
    return encode({'apiversion': 4, 'result': {
        'total': len(ids) if total is None else total, 'offset': offset,
        'limit': limit, 'goods': [{'good_id': n} for n in ids], **overrides}})


def details(ids):
    return encode({'apiversion': 3, 'result': [{'good_id': n, 'unknown_future_field': 'сохранено в raw'} for n in ids]})


def test_bounded_pass_final_limit_exact_raw_hash_and_reordered_details():
    start = ReadPass(WINDOW, 2)
    first = listing([1, 2], total=3, limit=2)
    second = start.accept(first, details([2, 1]))
    assert start.summary()['coverage'] == 'incomplete' and start.count == 0
    assert second.pages[0].listing_sha256 == hashlib.sha256(first).hexdigest()
    request = second.next_request()
    assert request.listing_params() == {**PageRequest(WINDOW).listing_params(), 'offset': 2, 'limit': 1}
    final = second.accept(listing([3], total=3, offset=2, limit=1), details([3]))
    assert final.summary() == {'coverage': 'bounded_pass', 'page_count': 2, 'card_count': 3,
                              'reported_total': 3, 'atomic_snapshot': False,
                              'authenticity_verified': False, 'erp_imported': False}
    with pytest.raises(ContractError, match='pass_already_finished'):
        final.next_request()


def test_empty_pass_requires_explicit_total_and_no_details_request():
    final = ReadPass(WINDOW).accept(listing([]))
    assert final.finished and final.count == 0
    assert final.pages[0].details_sha256 is None
    with pytest.raises(ContractError, match='unexpected_details'):
        check_page(PageRequest(WINDOW), listing([]), details([]))


def test_missing_total_never_means_first_page_is_complete():
    response = json.loads(listing([1]))
    del response['result']['total']
    with pytest.raises(ContractError, match='listing_total'):
        ReadPass(WINDOW).accept(encode(response), details([1]))


def test_details_set_equality_does_not_hide_duplicates():
    with pytest.raises(ContractError, match='details_membership'):
        ReadPass(WINDOW).accept(listing([1, 2]), details([1, 1, 2]))


@pytest.mark.parametrize('ids', [[1], [1, 2, 3], [1, 3]])
def test_missing_extra_or_wrong_details_stop_whole_page(ids):
    initial = ReadPass(WINDOW)
    with pytest.raises(ContractError, match='details_membership'):
        initial.accept(listing([1, 2]), details(ids))
    assert initial.pages == ()


def test_unstable_total_or_repeated_ids_requires_restart():
    initial = ReadPass(WINDOW, 1).accept(listing([1], total=3, limit=1), details([1]))
    with pytest.raises(ContractError, match='total_changed_restart_pass'):
        initial.accept(listing([2], total=4, offset=1, limit=1), details([2]))
    with pytest.raises(ContractError, match='repeated_id_restart_pass'):
        initial.accept(listing([1], total=3, offset=1, limit=1), details([1]))
    assert initial.count == 1


@pytest.mark.parametrize('body', [
    listing([1], total=2), listing([], total=1), listing([1, 2], total=1),
    listing([1, 1]), listing([True]), listing(['1']), listing([0]),
    listing([1], offset=1), listing([1], limit=24), listing([1], total=True),
    listing([1], total='1'), listing([1], total=10001),
])
def test_inconsistent_page_not_accepted(body):
    with pytest.raises(ContractError):
        ReadPass(WINDOW).accept(body, details([1]))


@pytest.mark.parametrize('body', [b'{"apiversion":4,"result":{},"result":{}}',
    b'{"result":NaN}', b'{"result":Infinity}', b'\xff', b'not-json',
    b'{"apiversion":3,"result":{}}', b'{"apiversion":true,"result":{}}',
    b'{"apiversion":4,"result":{},"error":"APIKEY-PRIVATE"}'])
def test_bad_json_envelopes_and_error_messages_never_echo_values(body):
    with pytest.raises(ContractError) as caught:
        ReadPass(WINDOW).accept(body)
    assert 'APIKEY-PRIVATE' not in str(caught.value)


def test_limits_dates_and_detail_body_bounds():
    for args in [(0, 26), (9999, 2), (True, 1), (-1, 1), (10000, 1)]:
        with pytest.raises(ContractError, match='page_request'):
            PageRequest(WINDOW, *args)
    for start, end in [('2026-1-01 00:00:00', WINDOW.to_date),
                       (WINDOW.to_date, WINDOW.from_date),
                       (WINDOW.from_date + '+03:00', WINDOW.to_date),
                       (None, WINDOW.to_date)]:
        with pytest.raises(ContractError, match='explicit_time_window'):
            Window(start, end)
    with pytest.raises(ContractError, match='body_size'):
        decode_json(b' ' * (8 * 1024 * 1024 + 1))
    with pytest.raises(ContractError, match='missing_details'):
        ReadPass(WINDOW).accept(listing([1]))


def capture(tmp_path, pages=True):
    (tmp_path / 'list.json').write_bytes(listing([123]))
    (tmp_path / 'details.json').write_bytes(details([123]))
    data = {'format': 1, 'window': {'from_date': WINDOW.from_date, 'to_date': WINDOW.to_date},
            'page_size': 25, 'pages': [{'offset': 0, 'limit': 25, 'listing': 'list.json',
                                      'details': 'details.json'}] if pages else []}
    path = tmp_path / 'manifest.json'
    path.write_bytes(encode(data))
    return path, data


def test_offline_cli_exit_status_and_no_payload_output(tmp_path, monkeypatch, capsys):
    path, _ = capture(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['nk-check', '--manifest', str(path)])
    main()
    output = json.loads(capsys.readouterr().out)
    assert output['coverage'] == 'bounded_pass' and len(output['evidence']) == 1
    assert 'good_id' not in json.dumps(output)
    capture(tmp_path, pages=False)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)['coverage'] == 'incomplete'


def test_manifest_rejects_paths_links_wrong_offset_and_extra_pages(tmp_path):
    path, data = capture(tmp_path)
    page = data['pages'][0]
    for name in ['../list.json', '/list.json', 'sub/list.json', 'sub\\list.json']:
        page['listing'] = name
        path.write_bytes(encode(data))
        with pytest.raises(ContractError, match='capture_file_name'):
            check_capture(path)
    (tmp_path / 'link.json').symlink_to(tmp_path / 'list.json')
    page['listing'] = 'link.json'
    path.write_bytes(encode(data))
    with pytest.raises(ContractError, match='capture_file_name'):
        check_capture(path)
    page['listing'] = 'list.json'
    page['offset'] = 1
    path.write_bytes(encode(data))
    with pytest.raises(ContractError, match='capture_page_order'):
        check_capture(path)
    page['offset'] = 0
    data['pages'].append(dict(page))
    path.write_bytes(encode(data))
    with pytest.raises(ContractError, match='pass_already_finished'):
        check_capture(path)


def test_cli_failure_does_not_print_untrusted_file_or_json(tmp_path, monkeypatch, capsys):
    path, data = capture(tmp_path)
    (tmp_path / 'list.json').write_bytes(b'APIKEY-PRIVATE')
    monkeypatch.setattr(sys, 'argv', ['nk-check', '--manifest', str(path)])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == '' and output.err == 'invalid_json\n'


def test_capture_rejects_non_regular_files_without_waiting(tmp_path):
    path, data = capture(tmp_path)
    os.mkfifo(tmp_path / 'pipe.json')
    data['pages'][0]['listing'] = 'pipe.json'
    path.write_bytes(encode(data))
    with pytest.raises(ContractError, match='capture_file_type'):
        check_capture(path)
