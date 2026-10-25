"""Offline NK read evidence checks. No transport, credentials or ERP persistence.

This is an ERP acceptance policy for one bounded pass, not a complete NK schema
or evidence of an atomic remote snapshot. See docs/audit/NK-READ-CONTRACT.md.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 10000
DETAIL_BATCH = 25


class ContractError(ValueError):
    """Static error codes intentionally never include upstream values/secrets."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('duplicate_json_key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ContractError('invalid_json_number')


def decode_json(body: bytes):
    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
        raise ContractError('body_size')
    try:
        return json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('invalid_json') from None


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _result(body, version):
    value = decode_json(body)
    if (not isinstance(value, dict) or type(value.get('apiversion')) is not int
            or value['apiversion'] != version or 'result' not in value
            or value.get('error') or value.get('errors')):
        raise ContractError('response_envelope')
    return value['result']


@dataclass(frozen=True)
class Window:
    from_date: str
    to_date: str

    def __post_init__(self):
        try:
            dates = [datetime.strptime(v, '%Y-%m-%d %H:%M:%S')
                     for v in (self.from_date, self.to_date)]
            if (any(d.strftime('%Y-%m-%d %H:%M:%S') != v
                    for d, v in zip(dates, (self.from_date, self.to_date)))
                    or dates[0] >= dates[1]):
                raise ValueError
        except (TypeError, ValueError):
            raise ContractError('explicit_time_window') from None


@dataclass(frozen=True)
class PageRequest:
    window: Window
    offset: int = 0
    limit: int = DETAIL_BATCH

    def __post_init__(self):
        if (not isinstance(self.window, Window)
                or not _integer(self.offset, 0, MAX_RECORDS - 1)
                or not _integer(self.limit, 1, DETAIL_BATCH)
                or self.offset + self.limit > MAX_RECORDS):
            raise ContractError('page_request')

    def listing_params(self):
        return {'from_date': self.window.from_date, 'to_date': self.window.to_date,
                'offset': self.offset, 'limit': self.limit}


@dataclass(frozen=True)
class CheckedPage:
    request: PageRequest
    total: int
    good_ids: tuple[int, ...]
    listing_sha256: str
    details_sha256: str | None


def check_listing(request: PageRequest, listing_body: bytes) -> CheckedPage:
    listing = _result(listing_body, 4)
    if not isinstance(listing, dict):
        raise ContractError('listing_shape')
    # NK declares these fields optional. ERP cannot claim coverage without them.
    if not _integer(listing.get('total'), 0, MAX_RECORDS):
        raise ContractError('listing_total')
    if (type(listing.get('offset')) is not int or listing['offset'] != request.offset
            or type(listing.get('limit')) is not int or listing['limit'] != request.limit):
        raise ContractError('listing_page_echo')
    goods = listing.get('goods')
    if not isinstance(goods, list) or len(goods) > request.limit:
        raise ContractError('listing_goods')
    ids = []
    for good in goods:
        if not isinstance(good, dict) or not _integer(good.get('good_id'), 1, 2**63 - 1):
            raise ContractError('good_id')
        ids.append(good['good_id'])
    if len(ids) != len(set(ids)):
        raise ContractError('duplicate_listing_id')
    end = request.offset + len(ids)
    if (end > listing['total'] or request.offset > listing['total']
            or (end < listing['total'] and len(ids) != request.limit)
            or (not ids and (request.offset != 0 or listing['total'] != 0))):
        raise ContractError('listing_coverage')
    return CheckedPage(request, listing['total'], tuple(ids),
                       hashlib.sha256(listing_body).hexdigest(), None)


def check_page(request: PageRequest, listing_body: bytes,
               details_body: bytes | None = None) -> CheckedPage:
    page = check_listing(request, listing_body)
    ids = page.good_ids
    if ids:
        if details_body is None:
            raise ContractError('missing_details')
        details = _result(details_body, 3)
        if not isinstance(details, list):
            raise ContractError('details_shape')
        detail_ids = []
        for good in details:
            if not isinstance(good, dict) or not _integer(good.get('good_id'), 1, 2**63 - 1):
                raise ContractError('good_id')
            detail_ids.append(good['good_id'])
        if len(detail_ids) != len(set(detail_ids)) or set(detail_ids) != set(ids):
            raise ContractError('details_membership')
    elif details_body is not None:
        raise ContractError('unexpected_details')
    return CheckedPage(request, page.total, ids, page.listing_sha256,
                       hashlib.sha256(details_body).hexdigest() if details_body is not None else None)


@dataclass(frozen=True)
class ReadPass:
    window: Window
    page_size: int = DETAIL_BATCH
    pages: tuple[CheckedPage, ...] = ()

    def __post_init__(self):
        PageRequest(self.window, limit=self.page_size)

    @property
    def count(self):
        return sum(len(page.good_ids) for page in self.pages)

    @property
    def finished(self):
        return bool(self.pages) and self.count == self.pages[0].total

    def next_request(self):
        if self.finished:
            raise ContractError('pass_already_finished')
        remaining = self.pages[0].total - self.count if self.pages else MAX_RECORDS
        return PageRequest(self.window, self.count, min(self.page_size, remaining))

    def accept(self, listing_body: bytes, details_body: bytes | None = None):
        self.check_listing(listing_body)
        page = check_page(self.next_request(), listing_body, details_body)
        return ReadPass(self.window, self.page_size, self.pages + (page,))

    def check_listing(self, listing_body: bytes):
        page = check_listing(self.next_request(), listing_body)
        if self.pages and page.total != self.pages[0].total:
            raise ContractError('total_changed_restart_pass')
        seen = {good_id for old in self.pages for good_id in old.good_ids}
        if seen.intersection(page.good_ids):
            raise ContractError('repeated_id_restart_pass')
        return page

    def summary(self):
        return {'coverage': 'bounded_pass' if self.finished else 'incomplete',
                'page_count': len(self.pages), 'card_count': self.count,
                'reported_total': self.pages[0].total if self.pages else None,
                'atomic_snapshot': False, 'authenticity_verified': False,
                'erp_imported': False}
