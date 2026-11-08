"""M2 adapter wiring for new lookup tools (offline; no network, synthetic key)."""
import json

import pytest

from reinfolib import adapter

KEY = 'synthetic-test-key-123456789'


def test_municipalities_tool_is_defined_with_strict_schema():
    description, properties, required = adapter.DEFINITIONS['reinfolib_municipalities']
    assert required == ['area']
    assert set(properties) == {'area', 'language', 'name'}
    assert 'XIT002' in description or 'municipal' in description.lower()


def test_municipalities_dispatch_uses_scoped_key_and_injected_fetch(monkeypatch):
    seen = {}

    def fake_fetch(args, *, key, transport=None, gap=None, retry=None):
        seen.update(args=args, key=key)
        return {'ok': True, 'endpoint': 'XIT002', 'area': args['area'], 'count': 1,
                'municipalities': [{'code': '13101', 'name': '千代田区', 'kind': 'special_ward'}]}
    monkeypatch.setattr(adapter, '_scoped_key', lambda: KEY)
    import reinfolib.locations as locations
    monkeypatch.setattr(locations, 'fetch_municipalities', fake_fetch)
    monkeypatch.setattr(adapter, 'data_root', lambda: (_ for _ in ()).throw(AssertionError('no snapshot storage')))
    monkeypatch.setattr(adapter, 'production_gate', lambda: object())
    result = json.loads(adapter.dispatch('reinfolib_municipalities', {'area': '13'}))
    assert result['ok'] is True and result['municipalities'][0]['code'] == '13101'
    assert seen == {'args': {'area': '13', 'language': 'ja'}, 'key': KEY}
    assert result['credit'] and result['attribution']
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize('args', [{}, {'area': '13', 'extra': 1}, {'area': '99'}])
def test_municipalities_bad_arguments_are_fixed_errors(monkeypatch, args):
    monkeypatch.setattr(adapter, '_scoped_key', lambda: KEY)
    result = json.loads(adapter.dispatch('reinfolib_municipalities', args))
    assert result['ok'] is False and result['error_type'] == 'invalid_argument'


def test_municipalities_404_is_not_found(monkeypatch):
    from reinfolib.storage import CoreError
    import reinfolib.locations as locations
    monkeypatch.setattr(adapter, '_scoped_key', lambda: KEY)

    def missing(*args, **kwargs):
        raise CoreError('not_found', status_code=404)
    monkeypatch.setattr(locations, 'fetch_municipalities', missing)
    monkeypatch.setattr(adapter, 'production_gate', lambda: object())
    result = json.loads(adapter.dispatch('reinfolib_municipalities', {'area': '13'}))
    assert (result['ok'], result['error_type'], result['status_code']) == (False, 'not_found', 404)
    assert 'municipalities' not in result


def test_stations_tool_is_local_and_lists_candidates(monkeypatch):
    description, properties, required = adapter.DEFINITIONS['reinfolib_stations']
    assert required == [] and set(properties) == {'name', 'code', 'limit'}
    monkeypatch.setattr(adapter, '_scoped_key', lambda: (_ for _ in ()).throw(AssertionError('no key')))
    monkeypatch.setattr(adapter, 'data_root', lambda: (_ for _ in ()).throw(AssertionError('no storage')))
    monkeypatch.setattr(adapter, 'production_gate', lambda: (_ for _ in ()).throw(AssertionError('no gate')))
    result = json.loads(adapter.dispatch('reinfolib_stations', {'name': '府中'}))
    assert result['ok'] is True and result['match_status'] == 'ambiguous'
    assert result['dataset'] == 'N02-25' and '加工して作成' in result['station_attribution']
    assert result['credit']
    unique = json.loads(adapter.dispatch('reinfolib_stations', {'name': '西新宿五丁目'}))
    assert unique['candidates'][0]['group_code'] == '003696'


@pytest.mark.parametrize('args', [{}, {'name': '新宿', 'code': '003700'}, {'code': 'abc'}, {'name': 'x', 'limit': 0}])
def test_stations_argument_errors_are_fixed(args):
    result = json.loads(adapter.dispatch('reinfolib_stations', args))
    assert result['ok'] is False and result['error_type'] == 'invalid_argument'


def _tool_transactions(monkeypatch, tmp_path, status, headers, payload, args):
    """Drive reinfolib_transactions end to end with an injected HTTP transport."""
    import client
    import service
    calls = []

    def transport(url, sent, timeout, cap):
        calls.append(url)
        return status, headers, payload
    monkeypatch.setattr(client, 'default_transport', transport)
    monkeypatch.setattr(client, 'DEFAULT_GAP', client.Gap(now=lambda: 0.0, sleep=lambda _d: None))
    monkeypatch.setattr(service, '_secret', lambda name: KEY)
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    return json.loads(adapter.dispatch('reinfolib_transactions', args)), calls


@pytest.mark.parametrize('headers,payload', [
    ({'Content-Length': '8000001'}, b'{}'),
    ({'Content-Encoding': 'gzip'}, __import__('gzip').compress(b'{"status":"OK","data":[' + b'{"a":"' + b'x' * 8_000_000 + b'"}]}')),
])
def test_oversized_upstream_is_response_too_large_without_snapshot(monkeypatch, tmp_path, headers, payload):
    result, calls = _tool_transactions(monkeypatch, tmp_path, 200, headers, payload,
                                       {'area': '13', 'year': 2024, 'price_classification': 'both'})
    assert len(calls) == 1
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'response_too_large', 'too_big')
    assert 'records' not in result and 'snapshot_id' not in result
    assert not (tmp_path / 'snapshots').exists() or list((tmp_path / 'snapshots').iterdir()) == []


def test_station_400_is_fallback_required_without_automatic_fallback(monkeypatch, tmp_path):
    result, calls = _tool_transactions(monkeypatch, tmp_path, 400, {}, b'',
                                       {'station': '010188', 'year': 2024, 'quarter': 1})
    assert len(calls) == 1 and 'station=010188' in calls[0]
    assert (result['ok'], result['error_type'], result['status_code']) == (False, 'fallback_required', 400)
    assert 'No automatic fallback' in result['fallback']
