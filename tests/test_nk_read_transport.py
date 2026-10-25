"""Isolated wire simulations, not a productive NK contract pilot."""
import logging

import httpx
from pydantic import SecretStr
import pytest

from marking_erp.nk_read_contract import ContractError, MAX_BODY_BYTES, ReadPass
from marking_erp.nk_read_transport import NkReadClient, ReadError
from test_nk_read_contract import WINDOW, details, listing

TOKEN = 'synthetic-PRIVATE-token'


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, clock=None, advance=0):
        self.chunks, self.clock, self.advance = chunks, clock, advance
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if self.clock:
                self.clock.now += self.advance
            yield chunk

    def close(self):
        self.closed = True


def response(body=b'', status=200, headers=None, stream=None):
    return httpx.Response(status, headers={'Content-Type': 'application/json', **(headers or {})},
                          stream=stream if stream is not None else Stream([body]))


def client(handler, clock=None, **kwargs):
    clock = clock or Clock()
    return NkReadClient(environment='sandbox', token=SecretStr(TOKEN),
                        transport=httpx.MockTransport(handler), clock=clock,
                        sleep=clock.sleep, jitter=lambda: 0, **kwargs)


def test_exact_read_pair_auth_out_of_url_pacing_and_raw_evidence(caplog):
    requests = []
    clock = Clock()
    first, second = listing([1, 2]), details([2, 1])

    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.nk.sandbox.crptech.ru'
        assert request.headers['Authorization'] == 'Bearer ' + TOKEN
        assert request.headers['Accept-Encoding'] == 'identity'
        assert request.url.params['format'] == 'json'
        assert TOKEN not in str(request.url) and 'apikey' not in request.url.params
        return response(first if len(requests) == 1 else second)

    caplog.set_level(logging.DEBUG)
    with client(handler, clock) as reader:
        evidence = reader.read_next(ReadPass(WINDOW))
        assert TOKEN not in repr(reader) and TOKEN not in repr(evidence)
    assert evidence.state.finished
    assert evidence.listing_body == first and evidence.details_body == second
    assert [r.url.path for r in requests] == ['/v4/product-list', '/v3/feed-product']
    assert requests[1].url.params['good_ids'] == '1;2'
    assert clock.waits == [2]
    assert TOKEN not in caplog.text


def test_empty_listing_never_sends_details_and_finished_pass_never_sends_again():
    requests = []
    with client(lambda r: requests.append(r) or response(listing([]))) as reader:
        evidence = reader.read_next(ReadPass(WINDOW))
        assert evidence.state.finished and evidence.details_body is None
        with pytest.raises(ContractError, match='pass_already_finished'):
            reader.read_next(evidence.state)
    assert len(requests) == 1


def test_bad_or_changed_listing_stops_before_details_and_preserves_old_state():
    old = ReadPass(WINDOW, 1).accept(listing([1], total=2, limit=1), details([1]))
    for body in [listing([2], total=3, offset=1, limit=1),
                 listing([1], total=2, offset=1, limit=1), b'PRIVATE invalid-json']:
        requests = []
        with client(lambda r: requests.append(r) or response(body)) as reader:
            with pytest.raises(ContractError):
                reader.read_next(old)
        assert len(requests) == 1 and old.count == 1


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 301, 302, 307, 308])
def test_terminal_status_and_redirect_never_retry_or_forward_bearer(status):
    requests = []
    stream = Stream([b'upstream echoes ' + TOKEN.encode()])
    with client(lambda r: requests.append(r) or response(status=status, stream=stream,
                headers={'Location': 'https://attacker.invalid/?apikey=' + TOKEN})) as reader:
        with pytest.raises(ReadError) as caught:
            reader.read_next(ReadPass(WINDOW))
    assert caught.value.status == status and not caught.value.retryable
    assert TOKEN not in str(caught.value) and len(requests) == 1 and stream.closed


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_retryable_status_has_three_attempt_budget_and_server_delay(status):
    requests = []
    clock = Clock()
    with client(lambda r: requests.append(r) or response(status=status,
                headers={'Retry-After': '7'}), clock) as reader:
        with pytest.raises(ReadError) as caught:
            reader.read_next(ReadPass(WINDOW))
    assert caught.value.retryable and caught.value.retry_after == 7
    assert len(requests) == 3 and clock.waits == [7, 7]
    assert requests[0].url == requests[1].url == requests[2].url


def test_transient_failures_retry_same_request_then_validate_normally():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout(TOKEN, request=request)
        if len(requests) == 2:
            return response(status=503)
        return response(listing([]))

    clock = Clock()
    with client(handler, clock) as reader:
        assert reader.read_next(ReadPass(WINDOW)).state.finished
    assert len(requests) == 3 and clock.waits == [2, 2]


def test_network_exception_is_sanitized_and_bounded():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError('URL contains ' + TOKEN, request=request)

    with client(handler) as reader:
        with pytest.raises(ReadError) as caught:
            reader.read_next(ReadPass(WINDOW))
    assert str(caught.value) == 'nk_connection' and caught.value.__suppress_context__
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert len(requests) == 3


@pytest.mark.parametrize('retry_header', [None, 'bad', '-1', '3600'])
def test_long_or_missing_429_delay_does_not_sleep_or_retry_early(retry_header):
    requests = []
    clock = Clock()
    headers = {'Retry-After': retry_header} if retry_header is not None else {}
    with client(lambda r: requests.append(r) or response(status=429, headers=headers), clock) as reader:
        with pytest.raises(ReadError) as first:
            reader.read_next(ReadPass(WINDOW))
        with pytest.raises(ReadError, match='nk_cooldown') as second:
            reader.read_next(ReadPass(WINDOW))
    expected = 3600 if retry_header == '3600' else 300
    assert first.value.retry_after == second.value.retry_after == expected
    assert len(requests) == 1 and clock.waits == []


@pytest.mark.parametrize('name', ['API-Usage-Limit', 'API-Method-Usage-Limit'])
def test_exhausted_quota_pauses_even_after_success(name):
    requests = []
    with client(lambda r: requests.append(r) or response(listing([1]), headers={name: '500/500'})) as reader:
        with pytest.raises(ReadError, match='nk_cooldown') as caught:
            reader.read_next(ReadPass(WINDOW))
    assert len(requests) == 1 and caught.value.retry_after == 300


@pytest.mark.parametrize('headers,body', [
    ({'Content-Length': str(MAX_BODY_BYTES + 1)}, b''),
    ({'Content-Length': '-1'}, b''),
    ({'Content-Encoding': 'gzip'}, b'compressed'),
    ({'Content-Type': 'text/html'}, b'<error>'),
    ({}, b'x' * (MAX_BODY_BYTES + 1)),
])
def test_unacceptable_body_stops_without_retry_and_closes_stream(headers, body):
    requests = []
    stream = Stream([body])
    with client(lambda r: requests.append(r) or response(headers=headers, stream=stream)) as reader:
        with pytest.raises(ReadError) as caught:
            reader.read_next(ReadPass(WINDOW))
    assert not caught.value.retryable and len(requests) == 1 and stream.closed


def test_drip_stream_duration_checked_on_each_chunk_and_attempt_bounded():
    clock = Clock()
    streams = []

    def handler(request):
        stream = Stream([b' ', b' ', b' ', b' '], clock, advance=9)
        streams.append(stream)
        return response(stream=stream)

    with client(handler, clock) as reader:
        with pytest.raises(ReadError, match='nk_attempt_duration'):
            reader.read_next(ReadPass(WINDOW))
    assert len(streams) == 3 and all(s.closed for s in streams)


def test_detail_contract_failure_does_not_retry_or_advance_state():
    requests = []
    old = ReadPass(WINDOW)
    with client(lambda r: requests.append(r) or response(
            listing([1, 2]) if len(requests) == 1 else details([1, 1, 2]))) as reader:
        with pytest.raises(ContractError, match='details_membership'):
            reader.read_next(old)
    assert old.pages == () and len(requests) == 2


def test_mutating_get_and_arbitrary_origins_rejected_before_io():
    requests = []
    with client(lambda r: requests.append(r) or response()) as reader:
        for path in ['/v3/generate-gtins', '/v3/feed-moderation', '/v3/feed',
                     'https://attacker.invalid/v4/product-list', '//attacker.invalid']:
            with pytest.raises(ReadError, match='nk_read_path'):
                reader._get(path, {})
    assert requests == []
    for environment in ['http://localhost', 'custom', None]:
        with pytest.raises(ReadError, match='nk_environment'):
            NkReadClient(environment=environment, token=SecretStr(TOKEN))
    for token in ['', 'bad\r\nInjected: yes', 'токен', 'a' * 8193]:
        with pytest.raises(ReadError, match='nk_bearer_token'):
            NkReadClient(environment='sandbox', token=SecretStr(token))
