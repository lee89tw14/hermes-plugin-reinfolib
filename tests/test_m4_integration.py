"""Exercise real M4 -> M3 request -> RateGate with offline wire responses."""
import json
import pytest
from reinfolib import adapter, m4_adapter, transport
from reinfolib.scheduling import RateGate
from tests.test_m4_layers import KEY, QUERY, RAW


@pytest.fixture
def orchestration(monkeypatch, tmp_path):
    import service
    import client
    now = [transport.time.time()]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(transport.time, 'time', lambda: now[0])
    monkeypatch.setattr(transport.time, 'sleep', sleep)
    gate = RateGate(tmp_path / 'rate', spacing=1, clock=lambda: now[0], sleep=sleep)
    monkeypatch.setattr(adapter, 'production_gate', lambda: gate)
    monkeypatch.setattr(service, '_secret', lambda name: KEY)
    yield now, waits
    assert not (tmp_path / 'snapshots').exists()
    assert not (tmp_path / 'exports').exists()
    assert all(p.name == 'rate' for p in tmp_path.iterdir())


@pytest.mark.parametrize('tool,endpoint,query,payload', [
    ('reinfolib_layer', 'XKT002', QUERY, json.dumps(RAW).encode()),
    ('reinfolib_land_values', 'XPT002', {**QUERY, 'year': 2026}, json.dumps(RAW).encode()),
    ('reinfolib_land_values', 'XCT001', {'year': 2025, 'area': '47', 'division': '09'}, b'[{"unknown":"raw"}]'),
])
def test_m4_dispatch_retries_via_real_profile_gate(orchestration, monkeypatch, tool, endpoint, query, payload):
    import client
    calls = []
    def wire(url, headers, timeout, cap):
        calls.append((orchestration[0][0], url, timeout, cap))
        return (429, {'Retry-After': '2'}, b'') if len(calls) == 1 else (200, {}, payload)
    monkeypatch.setattr(client, 'default_transport', wire)
    result = json.loads(adapter.dispatch(tool, {'endpoint': endpoint, 'query': query}))
    assert result['ok'], result
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 2
    assert all(endpoint + '?' in c[1] and KEY not in c[1] and c[3] == 8_000_000 for c in calls)
    assert [a['status'] for a in result['attempts']] == [429, 200]
    assert result['stop_reason'] == 'completed'
    assert orchestration[1] == [2.0]  # no second legacy spacing wait


@pytest.mark.parametrize('endpoint', sorted(m4_adapter.layers.CONTRACTS))
@pytest.mark.parametrize('response_format', ['geojson', 'pbf'])
def test_every_tile_remote_path_uses_shared_retry(orchestration, monkeypatch, endpoint, response_format):
    import client
    contracts = m4_adapter.layers.CONTRACTS
    query = {'response_format': response_format, 'z': contracts[endpoint]['zoom'][0], 'x': 0, 'y': 0}
    if endpoint == 'XPT001':
        query.update({'from': '20252', 'to': '20252'})
    if endpoint == 'XPT002':
        query['year'] = 2025
    calls = []
    raw = b'\x1a\x00' if response_format == 'pbf' else json.dumps(RAW).encode()
    def wire(*args):
        calls.append(args)
        return (503, {}, b'') if len(calls) == 1 else (200, {}, raw)
    monkeypatch.setattr(client, 'default_transport', wire)
    result = json.loads(adapter.dispatch('reinfolib_layer', {'endpoint': endpoint, 'query': query}))
    assert result['ok'], result
    assert [a['status'] for a in result['attempts']] == [503, 200]
    assert len(calls) == 2
    assert result['property_level_spatial_inference_allowed'] is False
    if response_format == 'pbf':
        import base64
        assert result['decoded'] is False and base64.b64decode(result['raw_base64']) == raw
    else:
        assert result['raw'] == RAW


@pytest.mark.parametrize('status,kind', [(400, 'upstream'), (401, 'auth'), (403, 'auth'), (404, 'not_found')])
def test_m4_nonretryable_status_diagnostics(orchestration, monkeypatch, status, kind):
    import client
    calls = []
    def wire(*args):
        calls.append(args)
        return status, {}, b'not trusted upstream text'
    monkeypatch.setattr(client, 'default_transport', wire)
    result = m4_adapter.dispatch('reinfolib_layer', {'endpoint': 'XKT002', 'query': QUERY})
    assert result['error_type'] == kind and result['status_code'] == status
    assert len(calls) == 1 and result['attempts'] == [{'n': 1, 'status': status}]
    assert 'not trusted' not in json.dumps(result)


def test_m4_absolute_deadline_clips_send_and_stops_retry(orchestration, monkeypatch):
    import client
    calls = []
    def wire(url, headers, timeout, cap):
        calls.append(timeout)
        return 503, {}, b''
    monkeypatch.setattr(client, 'default_transport', wire)
    result = m4_adapter.dispatch('reinfolib_layer', {'endpoint': 'XKT002', 'query': QUERY},
                                 absolute_deadline=orchestration[0][0] + .5)
    assert calls == [.5]
    assert result['status_code'] == 503 and result['stop_reason'] == 'deadline'
    assert result['attempts'] == [{'n': 1, 'status': 503}]


def test_m4_gate_wait_obeys_outer_deadline(orchestration, monkeypatch):
    import client
    calls = []
    monkeypatch.setattr(client, 'default_transport', lambda *args: (calls.append(args) or (200, {}, json.dumps(RAW).encode())))
    args = {'endpoint': 'XKT002', 'query': QUERY}
    assert m4_adapter.dispatch('reinfolib_layer', args)['ok']
    result = m4_adapter.dispatch('reinfolib_layer', args, absolute_deadline=orchestration[0][0] + .5)
    assert result['error_type'] == 'timeout' and result['detail'] == 'rate_gate_deadline'
    assert len(calls) == 1 and result['attempts'] == []
    assert result['stop_reason'] == 'deadline'


@pytest.mark.parametrize('error', [TimeoutError('sensitive upstream exception'), OSError('sensitive upstream exception')])
def test_m4_transport_failure_is_bounded_and_redacted(orchestration, monkeypatch, error):
    import client
    calls = []
    def wire(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(client, 'default_transport', wire)
    result = m4_adapter.dispatch('reinfolib_layer', {'endpoint': 'XKT002', 'query': QUERY})
    assert not result['ok'] and len(calls) == 3
    assert len(result['attempts']) == 3
    assert result['stop_reason'] == 'max_attempts'
    assert 'sensitive upstream' not in json.dumps(result)


def test_m4_rejects_before_secret_gate_or_transport(monkeypatch):
    import service
    def forbidden(*args):
        raise AssertionError('must validate before side effects')
    monkeypatch.setattr(service, '_secret', forbidden)
    monkeypatch.setattr(adapter, 'production_gate', forbidden)
    for args,kind in [({'endpoint': 'XKT002', 'query': {}}, 'invalid_argument'),
                      ({'endpoint': 'XKT002', 'query': QUERY, 'purpose': 'commercial'}, 'license_review_required')]:
        result = m4_adapter.dispatch('reinfolib_layer', args)
        assert result['error_type'] == kind
