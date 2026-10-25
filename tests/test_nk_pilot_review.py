from copy import deepcopy
import json
from pathlib import Path
import stat
import sys

import pytest

from marking_erp import nk_review_cli
from marking_erp.nk_pilot_review import diagnose_card, review_pilot, write_review
from marking_erp.nk_read_contract import ContractError
from test_nk_pilot import load, run
from test_nk_read_contract import encode, listing
from test_nk_read_transport import client, response
from test_browser import browser

GTIN = '04640638349704'


def card(good_id=1, **overrides):
    return {'good_id': good_id, 'good_status': 'published', 'good_signed': True,
            'brand_name': 'BRAND', 'tnved': '8536',
            'good_attrs': [{'attr_id': 13914, 'attr_value': 'PART-1'},
                           {'attr_id': 13933, 'attr_value': '8536901000'}],
            'identified_by': [{'type': 'gtin', 'level': 'trade-unit', 'value': GTIN, 'multiplier': 1}],
            **overrides}


def capture(tmp_path, cards=None):
    cards = [card()] if cards is None else cards
    ids = [c['good_id'] for c in cards]
    bodies = [listing(ids), encode({'apiversion': 3, 'result': cards})]
    path = tmp_path / 'capture'
    with client(lambda _: response(bodies.pop(0))) as reader:
        run(reader, path)
    return path


def test_consistent_pilot_diagnostics_preserve_candidates_without_approval(tmp_path):
    report = review_pilot(capture(tmp_path))
    assert report['receipt_hashes_match'] and report['receipt_count'] == 2
    assert report['coverage'] == 'bounded_pass' and report['warnings'] == []
    assert report['cards'][0] == {'good_id': 1, 'pn_candidate': 'PART-1', 'gtin_candidates': [GTIN],
                                  'tnved_candidate': '8536901000', 'reported_status': 'published',
                                  'reported_signed': True, 'issues': []}
    assert not report['contract_verified'] and not report['import_allowed']
    assert not report['authenticity_verified'] and not report['erp_imported']


def test_semantically_valid_changed_body_fails_original_receipt_hash(tmp_path):
    path = capture(tmp_path)
    body = (path / 'details-0.json').read_bytes().replace(b'PART-1', b'PART-2')
    (path / 'details-0.json').write_bytes(body)
    with pytest.raises(ContractError, match='review_body_integrity'):
        review_pilot(path)


@pytest.mark.parametrize('mutation', ['missing_receipt', 'duplicate_receipt', 'bad_size', 'path', 'bad_time'])
def test_bad_receipt_inventory_rejected(tmp_path, mutation):
    path = capture(tmp_path)
    data = load(path)
    if mutation == 'missing_receipt':
        data['received'].pop()
        data['saved_body_bytes'] = data['received'][0]['size']
    elif mutation == 'duplicate_receipt':
        data['received'].append(deepcopy(data['received'][0]))
    elif mutation == 'bad_size':
        data['received'][0]['size'] = True
    elif mutation == 'path':
        data['received'][0]['file'] = '../outside.json'
    else:
        data['received'][0]['received_at'] = '2026-10-25'
    (path / 'run.json').write_bytes(encode(data))
    with pytest.raises(ContractError):
        review_pilot(path)


def test_pending_bad_json_is_hash_checked_but_never_treated_as_a_card(tmp_path):
    path = tmp_path / 'capture'
    with client(lambda _: response(b'invalid-json')) as reader:
        run(reader, path)
    report = review_pilot(path)
    assert report['card_count'] == 0 and report['uncommitted_body_count'] == 1
    assert report['warnings'] == ['incomplete_coverage', 'run_not_complete', 'uncommitted_bodies']
    (path / 'list-0.json').write_bytes(b'different')
    with pytest.raises(ContractError, match='review_body_integrity'):
        review_pilot(path)


def test_checkpoint_drift_and_untracked_files_reported_without_guessing_recovery(tmp_path):
    path = capture(tmp_path)
    data = load(path)
    data.update(state='in_progress', card_count=0, page_count=0, coverage='incomplete')
    (path / 'run.json').write_bytes(encode(data))
    (path / 'orphan.json').write_text('must not be parsed')
    report = review_pilot(path)
    assert report['card_count'] == 1 and report['untracked_file_count'] == 1
    assert report['warnings'] == ['run_not_complete', 'checkpoint_mismatch', 'untracked_files']


def test_pn_not_inferred_from_brand_or_attribute_names_and_ambiguity_retained():
    raw = card(good_attrs=[{'attr_id': 1, 'attr_name': 'Модель', 'attr_value': 'GUESS'}])
    result = diagnose_card(raw)
    assert result['pn_candidate'] is None and 'pn_missing' in result['issues']
    raw['good_attrs'] = [{'attr_id': 13914, 'attr_value': 'FIRST'}, {'attr_id': '13914', 'attr_value': 'SECOND'}]
    result = diagnose_card(raw)
    assert result['pn_candidate'] is None and 'pn_ambiguous' in result['issues']
    raw['good_attrs'] = [{'attr_id': 13914, 'attr_value': ' brand '}]
    assert 'pn_equals_brand' in diagnose_card(raw)['issues']


@pytest.mark.parametrize('value', [GTIN[:-1] + '5', 4640638349704, '4640638349704', '０' * 14])
def test_gtin_invalid_not_padded_coerced_or_repaired(value):
    result = diagnose_card(card(identified_by=[{'type': 'gtin', 'level': 'trade-unit', 'value': value, 'multiplier': 1}]))
    assert result['gtin_candidates'] == [] and 'gtin_invalid' in result['issues']


def test_transport_packaging_gtin_not_selected_and_multiple_trade_units_warned():
    assert diagnose_card(card(identified_by=[{'type': 'gtin', 'level': 'box', 'value': GTIN, 'multiplier': 10}]))['gtin_candidates'] == []
    identifiers = card()['identified_by'] + [{'type': 'gtin', 'level': 'trade-unit', 'value': '00000000000017', 'multiplier': True}]
    result = diagnose_card(card(identified_by=identifiers))
    assert len(result['gtin_candidates']) == 2
    assert {'gtin_ambiguous', 'trade_unit_multiplier'} <= set(result['issues'])


def test_full_tnved_conflict_group_mismatch_and_unknown_status_are_not_fixed():
    raw = card(tnved='1234567890', good_status='FUTURE', good_signed='true')
    result = diagnose_card(raw)
    assert result['tnved_candidate'] is None
    assert {'tnved_conflict', 'status_unknown', 'signed_flag_type'} <= set(result['issues'])
    result = diagnose_card(card(tnved='9999'))
    assert 'tnved_group_mismatch' in result['issues']
    assert 'published_without_signed_flag' in diagnose_card(card(good_signed=False))['issues']


def test_duplicate_pn_casefold_is_warning_without_changing_original_values(tmp_path):
    second = card(2, good_attrs=[{'attr_id': 13914, 'attr_value': ' part-1 '},
                                {'attr_id': 13933, 'attr_value': '8536901000'}])
    report = review_pilot(capture(tmp_path, [card(), second]))
    assert report['issue_counts'] == {'duplicate_gtin': 2, 'duplicate_pn': 2}
    assert report['cards'][1]['pn_candidate'] == ' part-1 '


def test_malformed_card_fields_produce_diagnostics_without_crashing():
    result = diagnose_card(card(good_attrs=[None, {'attr_id': 13914, 'attr_value': {'secret': 'value'}}],
                                 identified_by=[None], good_status={}, good_signed=0))
    assert {'attribute_shape', 'attribute_value', 'identifier_shape', 'pn_missing',
            'gtin_missing', 'status_missing_or_invalid', 'signed_flag_type'} <= set(result['issues'])


def test_private_html_escapes_raw_fields_has_no_external_assets_and_json_keeps_value(tmp_path):
    pn = '<script>alert("XSS")</script>'
    raw = card(good_attrs=[{'attr_id': 13914, 'attr_value': pn}])
    report = review_pilot(capture(tmp_path, [raw]))
    output = tmp_path / 'review'
    write_review(report, output)
    html = (output / 'review.html').read_text()
    assert pn not in html and '&lt;script&gt;' in html and "default-src 'none'" in html
    assert '<script' not in html and '<img' not in html and 'href=' not in html
    assert json.loads((output / 'review.json').read_bytes())['cards'][0]['pn_candidate'] == pn
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in output.iterdir())


@pytest.mark.parametrize('with_issues,expected', [(False, 0), (True, 2)])
def test_cli_outputs_counts_only_and_writes_private_report(tmp_path, monkeypatch, capsys, with_issues, expected):
    path = capture(tmp_path, [card(good_attrs=[])] if with_issues else None)
    monkeypatch.setattr(sys, 'argv', ['nk-review', '--capture', str(path), '--output', str(tmp_path / 'review')])
    with pytest.raises(SystemExit) as caught:
        nk_review_cli.main()
    output = capsys.readouterr()
    assert caught.value.code == expected and 'PART-1' not in output.out
    assert 'cards' not in json.loads(output.out) and output.err == ''


def test_cli_integrity_failure_does_not_create_report(tmp_path, monkeypatch, capsys):
    path = capture(tmp_path)
    (path / 'list-0.json').write_bytes(b'tampered')
    monkeypatch.setattr(sys, 'argv', ['nk-review', '--capture', str(path), '--output', str(tmp_path / 'review')])
    with pytest.raises(SystemExit) as caught:
        nk_review_cli.main()
    assert caught.value.code == 1 and capsys.readouterr().err == 'review_body_integrity\n'
    assert not (tmp_path / 'review').exists()


@pytest.mark.browser
def test_html_report_in_chromium_is_readable_and_does_not_execute_remote_fields(tmp_path, browser):
    from playwright.sync_api import expect
    attack = '<img src="https://invalid.example/track" onerror="window.nkInjected=true">'
    report = review_pilot(capture(tmp_path, [card(good_attrs=[{'attr_id': 13914, 'attr_value': attack}]), card(2)]))
    output = tmp_path / 'review'
    write_review(report, output)
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='ru-RU')
    page = context.new_page()
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    try:
        page.goto((output / 'review.html').as_uri())
        expect(page.get_by_role('heading', name='Разбор пилота НК')).to_be_visible()
        expect(page.locator('tbody tr')).to_have_count(2)
        expect(page.locator('tbody tr').first.locator('td').nth(1)).to_have_text(attack)
        assert page.evaluate('window.nkInjected === undefined')
        assert not page.locator('img,script,iframe').count()
        assert not any(url.startswith(('http:', 'https:')) for url in requests)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        screenshots = Path('test-results/ui')
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / 'nk-pilot-review.png'), full_page=True)
    finally:
        context.close()
