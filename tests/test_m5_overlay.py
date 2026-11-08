"""M5 slice c: reinfolib_geocode and reinfolib_point_layers through the real adapter,
M3 request/RateGate and injected offline wires (no live requests)."""
import json
import math

import pytest

from reinfolib import adapter, m5_adapter, spatial, transport
from reinfolib.scheduling import RateGate
from reinfolib.transport import RetryPolicy

KEY = 'k' * 32
LON0, LAT0 = 139.691772, 35.689629
M_LAT = 1 / 111195.08
M_LON = M_LAT / math.cos(math.radians(LAT0))
GSI_ONE = [{"geometry": {"coordinates": [LON0, LAT0], "type": "Point"}, "type": "Feature",
            "properties": {"addressCode": "", "title": "東京都新宿区西新宿二丁目８番"}}]


def gsi(lon, lat, title='東京都新宿区西新宿二丁目８番'):
    return [{"geometry": {"coordinates": [lon, lat], "type": "Point"}, "type": "Feature",
             "properties": {"addressCode": "", "title": title}}]


def square(half, cx=0.0, cy=0.0, lon=LON0, lat=LAT0, **props):
    ring = [[lon + (cx + dx) * M_LON, lat + (cy + dy) * M_LAT] for dx, dy in
            [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]]
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]}, 'properties': props}


def fc(*features):
    return json.dumps({'type': 'FeatureCollection', 'features': list(features)}).encode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    import client
    import service
    state = {'calls': [], 'routes': {}, 'secret_calls': 0, 'key': KEY}

    def secret(name):
        state['secret_calls'] += 1
        return state['key']

    def wire(url, headers, timeout, cap):
        state['calls'].append((url, dict(headers)))
        for prefix, answer in state['routes'].items():
            if prefix in url:
                return answer(url) if callable(answer) else answer
        return 200, {}, fc()
    monkeypatch.setattr(service, '_secret', secret)
    monkeypatch.setattr(client, 'default_transport', wire)
    monkeypatch.setattr(adapter, 'production_gate', lambda: RateGate(tmp_path / 'rate', spacing=0))
    monkeypatch.setattr(m5_adapter, 'gsi_gate', lambda: RateGate(tmp_path / 'gsi', spacing=0))
    monkeypatch.setattr(adapter, 'RETRY_POLICY', RetryPolicy(max_attempts=3, backoff_base=0, backoff_max=0, deadline=60))
    state['routes']['msearch.gsi.go.jp'] = (200, {}, json.dumps(GSI_ONE, ensure_ascii=False).encode())
    return state


def call(name, args):
    return json.loads(adapter.dispatch(name, args))


def reinfolib_calls(env):
    return [c for c in env['calls'] if 'reinfolib.mlit.go.jp' in c[0]]


def test_geocode_tool_lists_candidates_without_key_or_reinfolib(env):
    result = call('reinfolib_geocode', {'address': '東京都新宿区西新宿2-8-1'})
    assert result['ok'] is True and result['candidate_count'] == 1
    assert 'all_candidates' not in result
    assert result['candidates'][0]['matched_level'] == 'block'
    assert result['coordinate_semantics'] == 'geocoded_address_representative_point_not_parcel_or_building'
    assert env['secret_calls'] == 0 and reinfolib_calls(env) == []
    (url, headers), = env['calls']
    assert url.startswith('https://msearch.gsi.go.jp/address-search/AddressSearch?q=') and KEY not in json.dumps(headers)


def test_point_layers_reports_each_layer_separately(env):
    env['routes']['XKT002?'] = (200, {}, fc(square(40, _id='a1', use_area_ja='商業地域')))
    env['routes']['XKT026?'] = (200, {}, b'')
    env['routes']['XKT015?'] = (404, {}, b'')
    result = call('reinfolib_point_layers', {'address': '東京都新宿区西新宿2-8-1',
                                             'endpoints': ['XKT002', 'XKT026', 'XKT015'], 'tolerance_m': 5})
    assert result['ok'] is True, result
    assert [layer['endpoint'] for layer in result['layers']] == ['XKT002', 'XKT026', 'XKT015']
    zoning, flood, passengers = result['layers']
    assert zoning['relation'] == 'inside' and zoning['layer_kind'] == 'legal_designation'
    assert zoning['matched_groups'][0]['properties']['use_area_ja'] == '商業地域'
    assert zoning['source_vintage'].startswith('都市計画決定GISデータ') and zoning['commercial_listing'] == '〇'
    assert flood['relation'] == 'no_features_in_tile' and flood['layer_kind'] == 'physical_hazard_estimate'
    assert 'no_features_is_not_safe' in flood['warnings']
    assert passengers['relation'] == 'not_covered' and passengers['status_code'] == 404
    for layer in result['layers']:
        assert layer['safety_inference_allowed'] is False
        assert layer['tiles'] == [[15, 29099, 12902]]
        assert layer['license_review'] and layer['coverage_note'] and layer['terms_url']
        assert 'safe' not in layer['relation']
    location = result['location']
    assert location['matched_level'] == 'block' and location['lon'] == LON0
    assert location['coordinate_semantics'] == 'geocoded_address_representative_point_not_parcel_or_building'
    assert result['tolerance_m'] == 5 and result['credit'] and result['geocoder_attribution']
    assert 'no_property_level_conclusions_from_transaction_data' in result['limitations']
    assert all(KEY not in url for url, _ in env['calls'])
    assert all('Ocp-Apim-Subscription-Key' not in h for url, h in env['calls'] if 'gsi.go.jp' in url)


@pytest.mark.parametrize('endpoints,detail', [
    (['XPT001'], 'xpt001_not_property_level'), (['XKT002', 'XPT001'], 'xpt001_not_property_level'),
    (['XPT002'], 'endpoint_requires_filters'), (['XIT001'], 'unsupported_endpoint'),
    (['XKT002', 'XKT002'], 'bad_endpoints'), ([], 'bad_endpoints'), ('XKT002', 'bad_endpoints'),
    (['XKT001', 'XKT002', 'XKT003', 'XKT014', 'XKT023', 'XKT024', 'XKT026'], 'bad_endpoints'),
])
def test_endpoint_refusals_happen_before_secret_or_http(env, endpoints, detail):
    result = call('reinfolib_point_layers', {'address': '西新宿2-8-1', 'endpoints': endpoints})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'invalid_argument', detail)
    assert env['calls'] == [] and env['secret_calls'] == 0


@pytest.mark.parametrize('tolerance', [-1, 101, 'x', True, float('nan')])
def test_bad_tolerance_is_refused(env, tolerance):
    result = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT002'], 'tolerance_m': tolerance})
    assert (result['error_type'], result['detail']) == ('invalid_argument', 'bad_tolerance') and env['calls'] == []


def test_commercial_purpose_fails_closed_before_any_request(env):
    result = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT002'], 'purpose': 'commercial'})
    assert (result['error_type'], result['detail']) == ('license_review_required', 'regional_terms_not_cleared')
    assert env['calls'] == [] and env['secret_calls'] == 0


def test_missing_key_stops_before_geocoding(env):
    env['key'] = ''
    result = call('reinfolib_point_layers', {'address': '西新宿2-8-1', 'endpoints': ['XKT002']})
    assert (result['error_type'], result['detail']) == ('config', 'missing_key') and env['calls'] == []


def test_ambiguous_address_lists_candidates_and_never_picks(env):
    two = gsi(LON0, LAT0) + gsi(135.5, 34.7, '大阪府大阪市北区梅田一丁目１番')
    env['routes']['msearch.gsi.go.jp'] = (200, {}, json.dumps(two, ensure_ascii=False).encode())
    refused = call('reinfolib_point_layers', {'address': '一丁目1番', 'endpoints': ['XKT002']})
    assert (refused['ok'], refused['error_type'], refused['detail']) == (False, 'ambiguous_location', 'multiple_candidates')
    assert len(refused['candidates']) == 2 and reinfolib_calls(env) == []
    chosen = refused['candidates'][0]['candidate_id']
    result = call('reinfolib_point_layers', {'address': '一丁目1番', 'endpoints': ['XKT002'], 'candidate_id': chosen})
    assert result['ok'] and result['location']['candidate_id'] == chosen and result['location']['selection'] == 'candidate_id'


def test_coarse_geocode_refuses_object_level_overlay_with_reason(env):
    env['routes']['msearch.gsi.go.jp'] = (200, {}, json.dumps(gsi(LON0, LAT0, '東京都新宿区西新宿二丁目'), ensure_ascii=False).encode())
    result = call('reinfolib_point_layers', {'address': '西新宿二丁目', 'endpoints': ['XKT002']})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'location_not_reliable', 'matched_level_chome')
    assert 'block' in result['reason'] and reinfolib_calls(env) == []
    assert result['candidates'][0]['matched_level'] == 'chome'


def test_no_geocode_candidates_is_not_found(env):
    env['routes']['msearch.gsi.go.jp'] = (200, {}, b'[]')
    result = call('reinfolib_point_layers', {'address': 'zzz', 'endpoints': ['XKT002']})
    assert (result['error_type'], result['detail']) == ('not_found', 'no_geocode_candidates')


def test_one_failing_layer_does_not_hide_or_fake_the_others(env):
    env['routes']['XKT002?'] = (503, {}, b'')
    env['routes']['XKT014?'] = (200, {}, fc(square(30, _id='f1')))
    result = call('reinfolib_point_layers', {'address': '西新宿2-8-1', 'endpoints': ['XKT002', 'XKT014']})
    failed, fire = result['layers']
    assert failed['relation'] == 'failed' and failed['error_type'] == 'upstream'
    assert [a['status'] for a in failed['attempts']] == [503, 503, 503]
    assert 'matched_groups' not in failed
    assert fire['relation'] == 'inside' and result['failed_layer_n'] == 1


def test_partial_tile_failure_marks_the_layer_failed(env):
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    lon, lat = east - 2 * M_LON, north - 2 * M_LAT                    # near NE corner: 4 tiles
    env['routes']['msearch.gsi.go.jp'] = (200, {}, json.dumps(gsi(lon, lat), ensure_ascii=False).encode())
    env['routes']['x=29100&y=12901'] = (404, {}, b'')
    result = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT002'], 'tolerance_m': 10})
    (layer,) = result['layers']
    assert len(layer['tiles']) == 4 and layer['relation'] == 'not_covered'
    assert layer['tiles_fetched_n'] < 4


def test_tile_budget_is_checked_before_reinfolib_requests(env):
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    lon, lat = east - 2 * M_LON, north - 2 * M_LAT
    env['routes']['msearch.gsi.go.jp'] = (200, {}, json.dumps(gsi(lon, lat), ensure_ascii=False).encode())
    six = ['XKT001', 'XKT002', 'XKT003', 'XKT014', 'XKT023', 'XKT024']
    result = call('reinfolib_point_layers', {'address': 'a', 'endpoints': six, 'tolerance_m': 10})
    assert (result['error_type'], result['detail']) == ('invalid_argument', 'too_many_tiles')
    assert reinfolib_calls(env) == []


def test_line_layer_uses_distance_with_tolerance(env):
    line = {'type': 'Feature', 'properties': {'_id': 'r1'}, 'geometry': {'type': 'LineString', 'coordinates': [
        [LON0 - 100 * M_LON, LAT0 + 8 * M_LAT], [LON0 + 100 * M_LON, LAT0 + 8 * M_LAT]]}}
    env['routes']['XKT030?'] = (200, {}, fc(line))
    near = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT030'], 'tolerance_m': 10})['layers'][0]
    far = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT030'], 'tolerance_m': 5})['layers'][0]
    assert near['relation'] == 'within_tolerance' and far['relation'] == 'outside'
    assert near['layer_kind'] == 'legal_designation' and abs(near['matched_groups'][0]['distance_m'] - 8) < 0.5


def test_every_overlay_endpoint_has_a_layer_kind():
    from reinfolib import overlay
    eligible = set(m5_adapter.layers.CONTRACTS) - {'XPT001', 'XPT002'}
    assert set(overlay.LAYER_KINDS) == eligible
    assert set(overlay.LAYER_KINDS.values()) <= set(overlay.KIND_NOTES)


def test_local_failures_return_fixed_errors(env, monkeypatch):
    from reinfolib import overlay
    monkeypatch.setattr(overlay, 'layer_result', lambda *a, **k: (_ for _ in ()).throw(KeyError('secret text')))
    result = call('reinfolib_point_layers', {'address': 'a', 'endpoints': ['XKT002']})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'internal', 'local_view_failed')
    assert 'secret text' not in json.dumps(result)
