"""M3-2: bounded retry over any injected transport (no real network, injected clock)."""
import json
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest

KEY = 'SECRET-KEY-0123456789'
URL = 'https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001?year=2024&city=13104'
HEADERS = {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': KEY}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now, self.sleeps = now, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


class Script:
    """Replays statuses / exceptions; records the timeout of every attempt."""
    def __init__(self, clock, *steps):
        self.clock, self.steps, self.timeouts = clock, list(steps), []

    def __call__(self, url, headers, timeout, cap):
        assert headers[ 'Ocp-Apim-Subscription-Key'] == KEY
        self.timeouts.append(timeout)
        step = self.steps.pop(0)
        self.clock.now += 0.1
        if isinstance(step, BaseException):
            raise step
        status, extra = step if isinstance(step, tuple) else (step, {})
        return status, extra, b'{}'


def call(script, clock, **policy):
    from reinfolib.transport import RetryPolicy, request
    return request(URL, HEADERS, timeout=30, cap=8_000_000, policy=RetryPolicy(**policy),
                   transport=script, clock=clock, sleep=clock.sleep)


def statuses(result):
    return [a.get('status', a.get('error')) for a in result.attempts]


def test_success_first_try_records_one_attempt():
    clock = Clock()
    result = call(Script(clock, 200), clock)
    assert result.status == 200 and statuses(result) == [200] and clock.sleeps == []


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_client_errors_are_never_retried(status):
    clock = Clock()
    script = Script(clock, status, 200)
    result = call(script, clock)
    assert result.status == status and statuses(result) == [status]
    assert len(script.steps) == 1 and clock.sleeps == []


@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_5xx_is_retried_with_bounded_backoff(status):
    clock = Clock()
    result = call(Script(clock, status, status, 200), clock, max_attempts=3, backoff_base=1.0)
    assert statuses(result) == [status, status, 200] and result.status == 200
    assert clock.sleeps == [1.0, 2.0]


def test_attempts_are_bounded_and_last_status_returned():
    clock = Clock()
    script = Script(clock, 503, 503, 503, 200)
    result = call(script, clock, max_attempts=3)
    assert result.status == 503 and statuses(result) == [503, 503, 503]
    assert result.stop_reason == 'max_attempts' and len(script.steps) == 1


def test_backoff_is_capped():
    clock = Clock()
    call(Script(clock, 500, 500, 500, 500, 200), clock, max_attempts=5, backoff_base=2.0,
         backoff_max=5.0, deadline=600)
    assert clock.sleeps == [2.0, 4.0, 5.0, 5.0]


def test_429_honours_retry_after_seconds():
    clock = Clock()
    result = call(Script(clock, (429, {'retry-after': '7'}), 200), clock)
    assert statuses(result) == [429, 200] and clock.sleeps == [7.0]
    assert result.attempts[0]['retry_after_s'] == 7


def test_429_honours_retry_after_http_date():
    clock = Clock()
    when = datetime.fromtimestamp(clock.now + 5, tz=timezone.utc)  # whole seconds
    result = call(Script(clock, (429, {'Retry-After': format_datetime(when, usegmt=True)}), 200), clock)
    assert statuses(result) == [429, 200] and clock.sleeps == [4.9]


def test_429_retry_after_beyond_cap_stops_instead_of_waiting_less():
    clock = Clock()
    script = Script(clock, (429, {'retry-after': '3600'}), 200)
    result = call(script, clock, retry_after_cap=30)
    assert result.status == 429 and statuses(result) == [429]
    assert result.stop_reason == 'retry_after_exceeds_cap' and clock.sleeps == []


def test_429_without_retry_after_uses_backoff_and_bad_header_too():
    clock = Clock()
    result = call(Script(clock, 429, (429, {'retry-after': 'soon'}), 200), clock)
    assert statuses(result) == [429, 429, 200] and clock.sleeps == [1.0, 2.0]


def test_network_errors_are_retried_then_reported_with_attempts():
    from reinfolib.transport import TransportFailure
    clock = Clock()
    leak = OSError('connect failed for ' + KEY)
    with pytest.raises(TransportFailure) as caught:
        call(Script(clock, leak, TimeoutError(), leak), clock, max_attempts=3)
    failure = caught.value
    assert (failure.kind, failure.detail) == ('upstream', 'network')
    assert [a['error'] for a in failure.attempts] == ['network', 'timeout', 'network']
    assert KEY not in json.dumps(failure.attempts) + str(failure) + repr(failure.args)


def test_network_then_success():
    clock = Clock()
    result = call(Script(clock, OSError('x'), 200), clock)
    assert statuses(result) == ['network', 200]


def test_total_deadline_stops_retries_and_caps_attempt_timeout():
    clock = Clock()
    script = Script(clock, 503, 503, 200)
    result = call(script, clock, deadline=2.5, backoff_base=2.0, max_attempts=5)
    # 0.1 s attempt + 2 s wait + 0.1 s attempt, then a 4 s wait would pass the deadline.
    assert statuses(result) == [503, 503] and result.stop_reason == 'deadline'
    assert script.timeouts[0] == 2.5 and script.timeouts[1] == pytest.approx(0.4)


def test_invalid_response_from_transport_is_not_retried():
    import client
    from reinfolib.transport import TransportFailure
    clock = Clock()
    script = Script(clock, client.CallError('invalid_response', 'too_big'), 200)
    with pytest.raises(TransportFailure) as caught:
        call(script, clock)
    assert (caught.value.kind, caught.value.detail) == ('invalid_response', 'too_big')
    assert len(script.steps) == 1


def test_every_attempt_passes_through_the_gate():
    from reinfolib.scheduling import RateGate
    from reinfolib.transport import RetryPolicy, request
    import tempfile, pathlib
    clock = Clock()
    with tempfile.TemporaryDirectory() as folder:
        gate = RateGate(pathlib.Path(folder) / 'g', clock=clock, sleep=clock.sleep)
        result = request(URL, HEADERS, timeout=30, cap=1, policy=RetryPolicy(backoff_base=0.25),
                         transport=Script(clock, 503, 200), gate=gate, clock=clock, sleep=clock.sleep)
    assert statuses(result) == [503, 200]
    # 0.25 s backoff, then the gate tops up to the 1 s spacing.
    assert clock.sleeps == [0.25, 0.75]


def test_attempt_timeout_is_clipped_after_gate_wait(tmp_path):
    from reinfolib.scheduling import RateGate
    from reinfolib.transport import RetryPolicy, request
    clock = Clock()
    gate = RateGate(tmp_path / 'gate', spacing=2, clock=clock, sleep=clock.sleep)
    gate.run(lambda: None)
    script = Script(clock, 200)
    request(URL, HEADERS, timeout=30, cap=1, policy=RetryPolicy(deadline=2.5),
            transport=script, gate=gate, clock=clock, sleep=clock.sleep)
    assert script.timeouts == [pytest.approx(0.5)]


def test_gate_deadline_before_first_attempt_is_timeout():
    from reinfolib.scheduling import GateDeadline
    from reinfolib.transport import TransportFailure
    class Busy:
        def run(self, call, *, deadline=None):
            raise GateDeadline('x')
    clock = Clock()
    from reinfolib.transport import RetryPolicy, request
    with pytest.raises(TransportFailure) as caught:
        request(URL, HEADERS, timeout=30, cap=1, policy=RetryPolicy(), transport=Script(clock, 200),
                gate=Busy(), clock=clock, sleep=clock.sleep)
    assert (caught.value.kind, caught.value.detail) == ('timeout', 'rate_gate_deadline')
    assert caught.value.attempts == []


FIXTURE = (__import__('pathlib').Path(__file__).parent / 'fixtures' / 'xit001_ok.json').read_bytes()
QUERY = {'city': '13102', 'year': 2015, 'quarter': 2}


def body_script(clock, *steps):
    script = Script(clock, *steps)
    inner = script.__call__
    def transport(url, headers, timeout, cap):
        status, extra, _ = inner(url, headers, timeout, cap)
        return status, extra, FIXTURE if status == 200 else b'{}'
    transport.script = script
    return transport


def test_core_retries_5xx_and_reports_attempts_on_snapshot(tmp_path):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    from reinfolib.transport import RetryPolicy
    from datetime import date
    clock = Clock()
    snapshot = fetch_transactions(QUERY, key=KEY, storage=SnapshotStore(tmp_path / 's'),
                                  transport=body_script(clock, 503, 200), retry=RetryPolicy(),
                                  clock=clock, sleep=clock.sleep, today=date(2026, 2, 1))
    assert [a['status'] for a in snapshot['attempts']] == [503, 200]
    assert snapshot['upstream_count'] == 1
    stored = SnapshotStore(tmp_path / 's').load(snapshot['snapshot_id'])
    assert 'attempts' not in stored  # request metadata is not part of the immutable record


@pytest.mark.parametrize('status,kind', [(404, 'not_found'), (401, 'auth'), (403, 'auth'), (400, 'upstream')])
def test_core_does_not_retry_client_errors_and_keeps_attempts(tmp_path, status, kind):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    from reinfolib.transport import RetryPolicy
    from datetime import date
    clock = Clock()
    transport = body_script(clock, status, 200)
    with pytest.raises(CoreError) as caught:
        fetch_transactions(QUERY, key=KEY, storage=SnapshotStore(tmp_path / 's'), transport=transport,
                           retry=RetryPolicy(), clock=clock, sleep=clock.sleep, today=date(2026, 2, 1))
    assert caught.value.error_type == kind
    assert caught.value.attempts == [{'n': 1, 'status': status}]
    assert len(transport.script.steps) == 1


def test_core_rate_limited_after_bounded_429s(tmp_path):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    from reinfolib.transport import RetryPolicy
    from datetime import date
    clock = Clock()
    with pytest.raises(CoreError) as caught:
        fetch_transactions(QUERY, key=KEY, storage=SnapshotStore(tmp_path / 's'),
                           transport=body_script(clock, 429, 429, 429), retry=RetryPolicy(),
                           clock=clock, sleep=clock.sleep, today=date(2026, 2, 1))
    assert caught.value.error_type == 'rate_limited'
    assert [a['status'] for a in caught.value.attempts] == [429, 429, 429]


def test_core_network_failure_is_upstream_with_attempts_and_no_key(tmp_path):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    from reinfolib.transport import RetryPolicy
    from datetime import date
    clock = Clock()
    with pytest.raises(CoreError) as caught:
        fetch_transactions(QUERY, key=KEY, storage=SnapshotStore(tmp_path / 's'),
                           transport=body_script(clock, OSError(KEY), OSError(KEY), OSError(KEY)),
                           retry=RetryPolicy(), clock=clock, sleep=clock.sleep, today=date(2026, 2, 1))
    assert (caught.value.error_type, caught.value.detail) == ('upstream', 'network')
    assert [a['error'] for a in caught.value.attempts] == ['network'] * 3
    assert KEY not in repr(caught.value.attempts) + repr(caught.value.args)


def test_adapter_transactions_result_and_error_include_attempts(tmp_path, monkeypatch):
    import client, service
    from reinfolib import adapter
    from reinfolib.transport import RetryPolicy
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path / 'v2')
    monkeypatch.setattr(adapter, 'RETRY_POLICY', RetryPolicy(backoff_base=0.01))
    monkeypatch.setattr(service, '_secret', lambda name: KEY)
    clock = Clock()
    monkeypatch.setattr(client, 'default_transport', body_script(clock, 503, 200))
    ok = json.loads(adapter.dispatch('reinfolib_transactions', dict(QUERY)))
    assert ok['ok'] is True and [a['status'] for a in ok['attempts']] == [503, 200]
    monkeypatch.setattr(client, 'default_transport', body_script(clock, 404))
    missing = json.loads(adapter.dispatch('reinfolib_transactions', dict(QUERY)))
    assert missing['error_type'] == 'not_found' and missing['attempts'] == [{'n': 1, 'status': 404}]
    assert KEY not in json.dumps(ok) + json.dumps(missing)
