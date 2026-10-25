"""Bounded read-only NK transport for an isolated pilot, not a production adapter.

No application wiring, authentication flow, database access or automatic import.
Only a GIS MT bearer token is supported; secrets never appear in request URLs.
"""
from dataclasses import dataclass, field
import math
import random
import re
import threading
import time

import httpx
from pydantic import SecretStr

from .nk_read_contract import MAX_BODY_BYTES, ReadPass

_BASES = {
    'sandbox': 'https://api.nk.sandbox.crptech.ru',
    'production': 'https://апи.национальный-каталог.рф',
}
_PATHS = frozenset(('/v4/product-list', '/v3/feed-product'))
_RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))
_INTERVAL = 2.0
_MAX_AUTO_WAIT = 30
_ATTEMPTS = 3


class ReadError(ValueError):
    def __init__(self, code, *, status=None, retryable=False, retry_after=None):
        super().__init__(code)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class PageEvidence:
    state: ReadPass
    listing_body: bytes = field(repr=False)
    details_body: bytes | None = field(repr=False)


class NkReadClient:
    def __init__(self, *, environment: str, token: SecretStr,
                 transport: httpx.BaseTransport | None = None,
                 clock=time.monotonic, sleep=time.sleep, jitter=random.random):
        if not isinstance(environment, str) or environment not in _BASES:
            raise ReadError('nk_environment')
        if (not isinstance(token, SecretStr) or not 1 <= len(token.get_secret_value()) <= 8192
                or re.fullmatch(r'[A-Za-z0-9._~+/\-]+=*', token.get_secret_value()) is None):
            raise ReadError('nk_bearer_token')
        self._base = _BASES[environment]
        self._token = token
        self._clock, self._sleep, self._jitter = clock, sleep, jitter
        self._next_allowed = 0.0
        self._lock = threading.RLock()
        self._client = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False, verify=True,
            timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._client.close()

    def read_next(self, state: ReadPass, *, observe=None) -> PageEvidence:
        # Serialize the entire pair; a failed detail read never advances state.
        with self._lock:
            request = state.next_request()
            listing = self._get('/v4/product-list', request.listing_params())
            if observe is not None:
                observe('/v4/product-list', listing)
            page = state.check_listing(listing)
            details = self._get('/v3/feed-product', {
                'good_ids': ';'.join(str(good_id) for good_id in page.good_ids),
            }) if page.good_ids else None
            if details is not None and observe is not None:
                observe('/v3/feed-product', details)
            return PageEvidence(state.accept(listing, details), listing, details)

    def _quota(self, response):
        for name in ('API-Usage-Limit', 'API-Method-Usage-Limit'):
            match = re.fullmatch(r'\s*(\d{1,9})/(\d{1,9})\s*', response.headers.get(name, ''))
            if match and int(match[1]) >= int(match[2]) > 0:
                # Local conservative cooldown, NOT a distributed organization quota.
                self._next_allowed = max(self._next_allowed, self._clock() + 300)

    def _once(self, path, params):
        started = self._clock()
        try:
            with self._client.stream('GET', self._base + path,
                    params={**params, 'format': 'json'},
                    headers={'Authorization': 'Bearer ' + self._token.get_secret_value(),
                             'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                self._quota(response)
                if response.status_code != 200:
                    retry_after = None
                    header = response.headers.get('Retry-After', '').strip()
                    if re.fullmatch(r'\d{1,9}', header):
                        retry_after = max(1, int(header))
                    if response.status_code == 429 and retry_after is None:
                        retry_after = 300
                    raise ReadError('nk_http_status', status=response.status_code,
                                    retryable=response.status_code in _RETRY_STATUSES,
                                    retry_after=retry_after)
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ReadError('nk_content_encoding')
                if response.headers.get('Content-Type', '').partition(';')[0].strip().lower() != 'application/json':
                    raise ReadError('nk_content_type')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isascii() or not length.isdecimal()
                                           or len(length) > 12 or int(length) > MAX_BODY_BYTES):
                    raise ReadError('nk_body_size')
                body = bytearray()
                for chunk in response.iter_raw():
                    if self._clock() - started > 30:
                        raise ReadError('nk_attempt_duration', retryable=True)
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise ReadError('nk_body_size')
                    body.extend(chunk)
                if self._clock() - started > 30:
                    raise ReadError('nk_attempt_duration', retryable=True)
                return bytes(body)
        except httpx.TransportError:
            # Raise outside the handler so even __context__ retains no request
            # headers or upstream exception text for an error-reporting service.
            pass
        finally:
            self._next_allowed = max(self._next_allowed, self._clock() + _INTERVAL)
        raise ReadError('nk_connection', retryable=True) from None

    def _get(self, path, params):
        if path not in _PATHS:
            raise ReadError('nk_read_path')
        for attempt in range(_ATTEMPTS):
            wait = max(0, self._next_allowed - self._clock())
            if wait > _MAX_AUTO_WAIT:
                raise ReadError('nk_cooldown', retryable=True, retry_after=math.ceil(wait))
            if wait:
                self._sleep(wait)
            try:
                return self._once(path, params)
            except ReadError as error:
                if not error.retryable:
                    raise
                delay = error.retry_after if error.retry_after is not None else 2**attempt + self._jitter()
                self._next_allowed = max(self._next_allowed, self._clock() + delay)
                if attempt == _ATTEMPTS - 1 or delay > _MAX_AUTO_WAIT:
                    raise
        raise AssertionError('unreachable')
