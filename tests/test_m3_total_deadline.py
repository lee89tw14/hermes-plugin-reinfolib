"""Whole history budget includes every quarter, retries and gate waits."""
from datetime import date
from dataclasses import replace

from reinfolib.history import probe
from reinfolib.storage import CoreError, SnapshotStore
from reinfolib.core import fetch_transactions
from reinfolib.scheduling import RateGate
from reinfolib.transport import RetryPolicy


def test_retry_that_cannot_fit_remaining_budget_reports_deadline(tmp_path):
    now = [100.0]
    def clock():
        return now[0]
    def transport(u, h, t, c):
        now[0] += 0.5
        return 503, {}, b''
    def fetch(query, end):
        return fetch_transactions(query, key='synthetic-deadline-key',
            storage=SnapshotStore(tmp_path / 'snapshots'), transport=transport,
            retry=RetryPolicy(), absolute_deadline=end, clock=clock)
    result = probe({'city': '13102'}, fetch=None, bounded_fetch=fetch,
                   clock=clock, deadline_seconds=1, today=date(2026, 11, 8))
    assert result['stop_reason'] == 'deadline'
    assert now[0] == 100.5 and len(result['attempted']) == 1
    assert result['attempted'][0]['status_code'] == 503


def test_gate_refusal_is_history_deadline_not_generic_failure():
    def fetch(query, end):
        error = CoreError('timeout', 'rate_gate_deadline')
        error.attempts = []
        raise error
    result = probe({'city': '13102'}, fetch=None, bounded_fetch=fetch,
                   clock=lambda: 100, deadline_seconds=1, today=date(2026, 11, 8))
    assert result['stop_reason'] == 'deadline'
    assert result['outcome'] == 'failed' and result['live_requests'] == 0
    assert result['attempted_periods'] == 1  # gate refused before any HTTP


def test_transport_uses_absolute_outer_deadline():
    from reinfolib.transport import request
    observed = []
    result = request('https://example.invalid', {}, timeout=30, cap=100,
                     policy=RetryPolicy(), absolute_deadline=102,
                     clock=lambda: 101, transport=lambda u, h, t, c: (observed.append(t) or 404, {}, b''))
    assert observed == [1]
    assert result.status == 404


def test_production_fetch_clips_policy_to_walk_deadline(monkeypatch, tmp_path):
    from reinfolib import research_tools, adapter, core
    import service
    monkeypatch.setattr(service, '_secret', lambda name: 'synthetic-key')
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(adapter, 'production_gate', lambda: None)
    monkeypatch.setattr(research_tools, 'wall_clock', lambda: 100)
    captured = {}
    def capture(query, **kwargs):
        captured.update(kwargs)
        return {}
    monkeypatch.setattr(core, 'fetch_transactions', capture)
    research_tools._fetch({'city': '13102'}, 102)
    assert captured['retry'].deadline == 2
    assert captured['absolute_deadline'] == 102
    assert captured['clock']() == 100



def test_aggregate_deadline_caps_retry_and_gate_wait(tmp_path):
    now = [100.0]
    def clock():
        return now[0]
    def sleep(seconds):
        now[0] += seconds
    calls, timeouts = [], []
    def transport(url, headers, timeout, cap):
        timeouts.append(timeout)
        now[0] += min(1.0, timeout)
        return (404 if len(calls) == 1 else 503), {}, b''
    gate = RateGate(tmp_path / 'gate', clock=clock, sleep=sleep)
    def fetch(query, end):
        calls.append(query['quarter'])
        return fetch_transactions(query, key='synthetic-deadline-key',
            storage=SnapshotStore(tmp_path / 'snapshots'), transport=transport,
            gap=gate, retry=replace(RetryPolicy(), deadline=min(90, end-clock())),
            today=date(2026, 11, 8), clock=clock, sleep=sleep)
    result = probe({'city': '13102'}, fetch=lambda q: (_ for _ in ()).throw(AssertionError()),
                   bounded_fetch=fetch, clock=clock, deadline_seconds=4,
                   today=date(2026, 11, 8), max_quarters=8)
    assert result['outcome'] == 'failed'
    assert result['stop_reason'] == 'deadline'
    assert calls == [4, 3]
    assert timeouts == [4, 2]
    assert now[0] <= 104
    assert result['attempted'][1]['attempts'] == [{'n': 1, 'status': 503, 'wait_s': 1.0}]
