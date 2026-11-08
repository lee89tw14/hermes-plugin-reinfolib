"""Offline integration checks for the union of M2 and M3."""
import json

from reinfolib import adapter, locations
from reinfolib.transport import RetryPolicy


def test_municipality_adapter_uses_production_gate_and_retry(monkeypatch):
    gate, policy = object(), RetryPolicy(backoff_base=0)
    seen = {}
    monkeypatch.setattr(adapter, 'production_gate', lambda: gate)
    monkeypatch.setattr(adapter, 'RETRY_POLICY', policy)
    monkeypatch.setattr(adapter, '_scoped_key', lambda: 'offline-placeholder')
    def fetch(args, **kwargs):
        seen.update(kwargs)
        return {'ok': True}
    monkeypatch.setattr(locations, 'fetch_municipalities', fetch)
    assert json.loads(adapter.dispatch('reinfolib_municipalities', {'area': '13'}))['ok']
    assert seen.get('gap') is gate
    assert seen.get('retry') is policy


def test_municipality_retry_attempts_use_gate_and_keep_validation():
    statuses = iter([503, 200])
    calls = []
    class Gate:
        def run(self, fn, **kwargs):
            calls.append(kwargs)
            return fn()
    def transport(url, headers, timeout, cap):
        return next(statuses), {}, b'{"status":"OK","data":[{"id":"13102","name":"Chuo"}]}'
    result = locations.fetch_municipalities(
        {'area': '13'}, key='offline-placeholder', transport=transport, gap=Gate(),
        retry=RetryPolicy(backoff_base=0))
    assert result['municipalities'][0]['code'] == '13102'
    assert result['attempts'] == [{'n': 1, 'status': 503, 'wait_s': 0}, {'n': 2, 'status': 200}]
    assert len(calls) == 2
    assert all('deadline' in call for call in calls)
