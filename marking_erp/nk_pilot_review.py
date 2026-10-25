"""Offline evidence integrity and conservative card diagnostics; never an import."""
from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import re
from uuid import UUID

from .gs1 import valid_gtin
from .nk_pilot import MAX_BUDGET, PrivateCapture
from .nk_read_cli import _body, _read, check_capture
from .nk_read_contract import ContractError, MAX_BODY_BYTES, decode_json

_LABELS = {
    'attributes_shape': 'Некорректный список атрибутов', 'attribute_shape': 'Некорректный атрибут',
    'attribute_value': 'Пустое, слишком длинное или нетекстовое значение атрибута',
    'pn_missing': 'PN не найден', 'pn_ambiguous': 'Несколько вариантов PN',
    'pn_equals_brand': 'PN совпадает с брендом', 'identifiers_shape': 'Некорректный список идентификаторов',
    'identifier_shape': 'Некорректный идентификатор', 'gtin_invalid': 'Неверный формат или контрольная цифра GTIN',
    'gtin_missing': 'GTIN потребительской единицы не найден', 'gtin_ambiguous': 'Несколько GTIN потребительской единицы',
    'trade_unit_multiplier': 'Количество в потребительской единице не равно 1',
    'tnved_attribute_invalid': 'Атрибут ТН ВЭД не содержит 10 цифр', 'tnved_full_missing': 'Полный ТН ВЭД не найден',
    'tnved_conflict': 'Противоречащие значения ТН ВЭД', 'tnved_group_mismatch': 'Группа ТН ВЭД не совпадает с полным кодом',
    'status_missing_or_invalid': 'Статус отсутствует или некорректен', 'status_unknown': 'Неизвестный статус',
    'signed_flag_type': 'Некорректный признак подписания', 'published_without_signed_flag': 'Публикация заявлена без признака подписи',
    'duplicate_pn': 'PN повторяется в других карточках', 'duplicate_gtin': 'GTIN повторяется в других карточках',
    'incomplete_coverage': 'Неполный обход выборки', 'run_not_complete': 'Запуск не завершён',
    'checkpoint_mismatch': 'Счётчики запуска и манифеста расходятся',
    'uncommitted_bodies': 'Есть ответы вне проверенных страниц', 'untracked_files': 'Есть файлы без записей о получении',
}


def _timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError
    except ValueError:
        raise ContractError('review_timestamp') from None


def _receipts(run):
    if (not isinstance(run, dict) or type(run.get('format')) is not int or run['format'] != 1
            or run.get('environment') not in ('sandbox', 'production')
            or run.get('state') not in ('in_progress', 'complete', 'page_limit', 'failed', 'interrupted')):
        raise ContractError('review_run')
    try:
        if not isinstance(run.get('run_id'), str) or str(UUID(run['run_id'])) != run['run_id']:
            raise ValueError
    except (ValueError, AttributeError):
        raise ContractError('review_run_id') from None
    _timestamp(run.get('started_at'))
    _timestamp(run.get('updated_at'))
    records = run.get('received')
    if not isinstance(records, list) or len(records) > 800:
        raise ContractError('review_receipts')
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ContractError('review_receipt')
        name, digest, size = record.get('file'), record.get('sha256'), record.get('size')
        if (not isinstance(name, str) or re.fullmatch(r'(list|details)-(0|[1-9][0-9]{0,3})\.json', name) is None
                or name in result or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None
                or type(size) is not int or not 0 <= size <= MAX_BODY_BYTES):
            raise ContractError('review_receipt')
        _timestamp(record.get('received_at'))
        result[name] = record
    total = sum(record['size'] for record in result.values())
    if (type(run.get('saved_body_bytes')) is not int or run['saved_body_bytes'] != total
            or total > MAX_BUDGET):
        raise ContractError('review_saved_bytes')
    return result


def _text(value, issues, code):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        issues.add(code)
        return None
    return value


def diagnose_card(raw):
    issues = set()
    attrs = raw.get('good_attrs')
    if not isinstance(attrs, list):
        issues.add('attributes_shape')
        attrs = []
    pn_values, tnved_values = set(), set()
    for attr in attrs:
        if not isinstance(attr, dict):
            issues.add('attribute_shape')
            continue
        attribute_id = attr.get('attr_id')
        # No name/brand aliases or guesses: these IDs are source-audit candidates.
        if type(attribute_id) is int or isinstance(attribute_id, str):
            if str(attribute_id) in ('13914', '13933'):
                value = _text(attr.get('attr_value'), issues, 'attribute_value')
                if value is not None:
                    (pn_values if str(attribute_id) == '13914' else tnved_values).add(value)
    if not pn_values:
        issues.add('pn_missing')
    elif len(pn_values) > 1:
        issues.add('pn_ambiguous')
    pn = next(iter(pn_values)) if len(pn_values) == 1 else None
    if pn is not None and isinstance(raw.get('brand_name'), str) and pn.strip().casefold() == raw['brand_name'].strip().casefold():
        issues.add('pn_equals_brand')
    identifiers = raw.get('identified_by')
    if not isinstance(identifiers, list):
        issues.add('identifiers_shape')
        identifiers = []
    gtins = set()
    for identifier in identifiers:
        if not isinstance(identifier, dict):
            issues.add('identifier_shape')
            continue
        if identifier.get('type') == 'gtin' and identifier.get('level') == 'trade-unit':
            value = identifier.get('value')
            if not isinstance(value, str) or re.fullmatch(r'[0-9]{14}', value) is None or not valid_gtin(value):
                issues.add('gtin_invalid')
            else:
                gtins.add(value)
            if type(identifier.get('multiplier')) is not int or identifier['multiplier'] != 1:
                issues.add('trade_unit_multiplier')
    if not gtins:
        issues.add('gtin_missing')
    elif len(gtins) > 1:
        issues.add('gtin_ambiguous')
    top_tnved = raw.get('tnved')
    if isinstance(top_tnved, str) and re.fullmatch(r'[0-9]{10}', top_tnved):
        tnved_values.add(top_tnved)
    valid_tnved = {v for v in tnved_values if re.fullmatch(r'[0-9]{10}', v)}
    if valid_tnved != tnved_values:
        issues.add('tnved_attribute_invalid')
    if not valid_tnved:
        issues.add('tnved_full_missing')
    elif len(valid_tnved) > 1:
        issues.add('tnved_conflict')
    tnved = next(iter(valid_tnved)) if len(valid_tnved) == 1 else None
    if (tnved is not None and isinstance(top_tnved, str) and re.fullmatch(r'[0-9]{4,9}', top_tnved)
            and not tnved.startswith(top_tnved)):
        issues.add('tnved_group_mismatch')
    status = _text(raw.get('good_status'), issues, 'status_missing_or_invalid')
    if status is not None and status not in ('draft', 'archived', 'published'):
        issues.add('status_unknown')
    signed = raw.get('good_signed')
    if type(signed) is not bool:
        issues.add('signed_flag_type')
        signed = None
    if status == 'published' and signed is not True:
        issues.add('published_without_signed_flag')
    return {'good_id': raw['good_id'], 'pn_candidate': pn, 'gtin_candidates': sorted(gtins),
            'tnved_candidate': tnved, 'reported_status': status, 'reported_signed': signed,
            'issues': sorted(issues)}


def review_pilot(directory: Path):
    directory = Path(directory)
    run_body = _read(directory / 'run.json')
    run = decode_json(run_body)
    receipts = _receipts(run)
    verified = set()

    def verify(name, body):
        receipt = receipts.get(name)
        if receipt is None or len(body) != receipt['size'] or sha256(body).hexdigest() != receipt['sha256']:
            raise ContractError('review_body_integrity')
        verified.add(name)

    # Include pending HTTP200 receipts, even if they failed card/JSON validation.
    for name in receipts:
        verify(name, _body(directory, name))
    cards, referenced = [], set()

    def observe(page, listing, details):
        verify(page['listing'], listing)
        referenced.add(page['listing'])
        if details is not None:
            verify(page['details'], details)
            referenced.add(page['details'])
            cards.extend(diagnose_card(card) for card in decode_json(details)['result'])

    coverage = check_capture(directory / 'manifest.json', observe=observe)
    pn_groups, gtin_groups = defaultdict(list), defaultdict(list)
    for card in cards:
        if card['pn_candidate'] is not None:
            pn_groups[card['pn_candidate'].strip().casefold()].append(card)
        for gtin in card['gtin_candidates']:
            gtin_groups[gtin].append(card)
    for groups, code in ((pn_groups, 'duplicate_pn'), (gtin_groups, 'duplicate_gtin')):
        for group in groups.values():
            if len(group) > 1:
                for card in group:
                    card['issues'] = sorted(set(card['issues']) | {code})
    warnings = []
    if coverage['coverage'] != 'bounded_pass':
        warnings.append('incomplete_coverage')
    if run['state'] != 'complete':
        warnings.append('run_not_complete')
    if any(type(run.get(key)) is not type(coverage[key]) or run.get(key) != coverage[key]
           for key in ('coverage', 'card_count', 'page_count', 'reported_total')):
        warnings.append('checkpoint_mismatch')
    pending = len(set(receipts) - referenced)
    if pending:
        warnings.append('uncommitted_bodies')
    expected = set(receipts) | {'manifest.json', 'run.json'}
    try:
        untracked = sum(1 for path in directory.iterdir() if path.name not in expected)
    except OSError:
        raise ContractError('review_inventory') from None
    if untracked:
        warnings.append('untracked_files')
    issue_counts = dict(sorted(Counter(code for card in cards for code in card['issues']).items()))
    return {'format': 1, 'run_id': run['run_id'], 'environment': run['environment'],
            'run_state': run['state'], 'run_sha256': sha256(run_body).hexdigest(),
            **{key: value for key, value in coverage.items() if key != 'evidence'},
            'receipt_hashes_match': True, 'receipt_count': len(verified),
            'uncommitted_body_count': pending, 'untracked_file_count': untracked,
            'cards_with_issues': sum(bool(card['issues']) for card in cards),
            'issue_counts': issue_counts, 'warnings': warnings, 'cards': cards,
            'contract_verified': False, 'import_allowed': False}


def write_review(report, output: Path):
    capture = PrivateCapture(output)
    try:
        capture.document('review.json', report)
        rows = []
        for card in report['cards']:
            values = [card['good_id'], card['pn_candidate'] or '—',
                      ', '.join(card['gtin_candidates']) or '—', card['tnved_candidate'] or '—',
                      card['reported_status'] or '—', '; '.join(_LABELS[code] for code in card['issues']) or '—']
            rows.append('<tr>' + ''.join('<td>' + escape(str(value)) + '</td>' for value in values) + '</tr>')
        html = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Разбор пилота НК</title><style>body{font:15px system-ui;margin:32px;color:#182338;background:#f4f6fa}
table{border-collapse:collapse;background:white;width:100%}td,th{padding:10px;border:1px solid #dce1ea;text-align:left;overflow-wrap:anywhere}
th{background:#e9eef8}td:nth-child(3){overflow-wrap:normal;font-family:ui-monospace,monospace;font-size:14px}
p{max-width:1100px}code{font-size:13px}</style><h1>Разбор пилота НК</h1>
<p>Диагностика сохранённых данных. Контракт, подлинность ответа и импорт в ERP не подтверждены.</p>'''
        coverage = 'проверенный обход выборки' if report['coverage'] == 'bounded_pass' else 'неполный обход'
        html += '<p>' + escape(f"Карточек: {report['card_count']}; с замечаниями: {report['cards_with_issues']}; охват: {coverage}.") + '</p>'
        html += '<p>' + escape('Замечания к запуску: ' + ('; '.join(_LABELS[code] for code in report['warnings']) or 'нет')) + '</p>'
        html += '<table><thead><tr><th>ID НК</th><th>PN: кандидат</th><th>GTIN: кандидаты</th><th>ТН ВЭД: кандидат</th><th>Заявленный статус</th><th>Замечания</th></tr></thead><tbody>'
        html += ''.join(rows) + '</tbody></table></html>'
        capture.write('review.html', html.encode())
    finally:
        capture.close()
