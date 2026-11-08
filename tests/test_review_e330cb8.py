"""Offline regressions for the e330cb8 independent review."""
import json
import pytest
from reinfolib import adapter, transport
from tests.test_m3_retry import Clock


@pytest.mark.parametrize('tool,args', [
    ('reinfolib_layer', {'endpoint': 'XKT002', 'query': {'response_format': 'geojson', 'z': 13, 'x': 0, 'y': 0}}),
    ('reinfolib_transactions', {'city': '13102', 'year': 2015, 'quarter': 2}),
    ('reinfolib_municipalities', {'area': '13'}),
])
def test_broken_real_gate_is_storage_without_http_or_backoff(tmp_path, monkeypatch, tool, args):
    import client, service
    clock = Clock()
    monkeypatch.setattr(transport.time, 'time', clock)
    monkeypatch.setattr(transport.time, 'sleep', clock.sleep)
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(service, '_secret', lambda name: 'offline-test-key')
    (tmp_path / 'rate-gate').write_text('occupied')
    calls = []
    monkeypatch.setattr(client, 'default_transport', lambda *a: calls.append(a))
    result = json.loads(adapter.dispatch(tool, args))
    assert (result['error_type'], result['detail']) == ('storage', 'rate_gate_unavailable')
    assert len(result['attempts']) <= 1
    assert calls == [] and clock.sleeps == []
    assert 'offline-test-key' not in json.dumps(result)


def test_unexpected_wire_failure_is_internal_without_retry():
    clock = Clock()
    calls = []
    def wire(*args):
        calls.append(args)
        raise RuntimeError('private exception text')
    with pytest.raises(transport.TransportFailure) as caught:
        transport.request('offline', {}, timeout=30, cap=1, transport=wire,
                          clock=clock, sleep=clock.sleep)
    failure = caught.value
    assert (failure.kind, failure.detail) == ('internal', 'transport_failed')
    assert failure.stop_reason == 'nonretryable'
    assert len(calls) == 1 and clock.sleeps == []
    assert 'private' not in str(failure) + repr(failure.attempts)


@pytest.mark.parametrize('error,expected', [
    (OSError('private'), ('storage', 'rate_gate_unavailable')),
    # Review ebebd09: a program error inside the gate is internal, not storage.
    (RuntimeError('private'), ('internal', 'rate_gate_failed')),
])
def test_gate_exception_is_not_wire_exception(error, expected):
    clock = Clock()
    calls = []
    class BrokenGate:
        def run(self, call, *, deadline):
            raise error
    with pytest.raises(transport.TransportFailure) as caught:
        transport.request('offline', {}, timeout=30, cap=1,
                          transport=lambda *a: calls.append(a), gate=BrokenGate(),
                          clock=clock, sleep=clock.sleep)
    failure = caught.value
    assert (failure.kind, failure.detail) == expected
    assert failure.attempts == [] and failure.stop_reason == 'nonretryable'
    assert calls == [] and clock.sleeps == []


def test_history_broken_gate_stops_as_local_failure(tmp_path, monkeypatch):
    import client, service
    from reinfolib import research_tools
    clock = Clock()
    monkeypatch.setattr(transport.time, 'time', clock)
    monkeypatch.setattr(transport.time, 'sleep', clock.sleep)
    monkeypatch.setattr(research_tools, 'wall_clock', clock)
    monkeypatch.setattr(research_tools, '_announcements', lambda end: {'status': 'unavailable'})
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(service, '_secret', lambda name: 'offline-test-key')
    (tmp_path / 'rate-gate').write_text('occupied')
    calls = []
    monkeypatch.setattr(client, 'default_transport', lambda *a: calls.append(a))
    result = json.loads(adapter.dispatch('reinfolib_history', {
        'city': '13102', 'newest_year': 2015, 'newest_quarter': 2,
        'max_quarters': 3, 'use_cache': False}))
    assert result['outcome'] == 'failed' and result['stop_reason'] == 'failure'
    assert len(result['attempted']) == 1
    record = result['attempted'][0]
    assert (record['error_type'], record['detail']) == ('storage', 'rate_gate_unavailable')
    assert record['result'] == 'failed' and record['attempts'] == []
    assert calls == [] and clock.sleeps == []
