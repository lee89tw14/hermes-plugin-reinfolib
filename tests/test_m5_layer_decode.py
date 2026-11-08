"""M5 slice d2: reinfolib_layer decode_pbf (opt-in decoded view; raw bytes retained)."""
import base64
import json

import pytest

from reinfolib import adapter, m4_adapter
from reinfolib.scheduling import RateGate
from tests.test_m5_mvt import feature, layer, POINT, SQUARE

KEY = 'k' * 32
QUERY = {'response_format': 'pbf', 'z': 15, 'x': 29099, 'y': 12902}


@pytest.fixture
def env(monkeypatch, tmp_path):
    import client
    import service
    state = {'calls': [], 'payload': b'', 'secret_calls': 0}

    def secret(name):
        state['secret_calls'] += 1
        return KEY

    def wire(url, headers, timeout, cap):
        state['calls'].append(url)
        return 200, {}, state['payload']
    monkeypatch.setattr(service, '_secret', secret)
    monkeypatch.setattr(client, 'default_transport', wire)
    monkeypatch.setattr(adapter, 'production_gate', lambda: RateGate(tmp_path / 'rate', spacing=0))
    return state


def call(args):
    return json.loads(adapter.dispatch('reinfolib_layer', args))


def test_decode_pbf_adds_decoded_layers_and_keeps_raw_bytes(env):
    env['payload'] = layer([feature(1, POINT, tags=(0, 0)), feature(3, SQUARE)])
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'decode_pbf': True})
    assert result['ok'] is True and result['decoded'] is True and result['format'] == 'pbf'
    assert base64.b64decode(result['raw_base64']) == env['payload']
    assert result['decoded_feature_count'] == 2 and 'raw' not in result
    (decoded,) = result['decoded_layers']
    assert [f['geometry']['type'] for f in decoded['features']] == ['Point', 'Polygon']
    assert result['decoded_semantics'] == 'mvt_decoded_simplified_quantised_not_the_geojson_response'
    assert result['property_level_spatial_inference_allowed'] is False


def test_without_flag_pbf_stays_opaque(env):
    env['payload'] = layer([feature(1, POINT)])
    result = call({'endpoint': 'XKT002', 'query': QUERY})
    assert result['decoded'] is False and 'decoded_layers' not in result


def test_empty_pbf_decodes_to_no_features_never_safe(env):
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'decode_pbf': True})
    assert result['decoded'] is True and result['decoded_feature_count'] == 0 and result['status'] == 'no_features'


@pytest.mark.parametrize('args,detail', [
    ({'endpoint': 'XKT002', 'query': {**QUERY, 'response_format': 'geojson'}, 'decode_pbf': True}, 'decode_requires_pbf'),
    ({'endpoint': 'XKT002', 'query': QUERY, 'decode_pbf': 'yes'}, 'bad_decode_flag'),
])
def test_bad_decode_requests_fail_before_secret_or_http(env, args, detail):
    result = call(args)
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'invalid_argument', detail)
    assert env['calls'] == [] and env['secret_calls'] == 0


def test_land_values_tool_does_not_accept_the_flag(env):
    result = json.loads(adapter.dispatch('reinfolib_land_values', {'endpoint': 'XPT002', 'query': {**QUERY, 'year': 2025},
                                                                    'decode_pbf': True}))
    assert (result['error_type'], result['detail']) == ('invalid_argument', 'invalid_parameters')


def test_malformed_pbf_is_fixed_invalid_response_with_attempts(env):
    env['payload'] = b'\x1a\x05\x0a'
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'decode_pbf': True})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'invalid_response', 'bad_pbf')
    assert result['attempts'] == [{'n': 1, 'status': 200}]


def test_decoded_xpt001_keeps_nearest_station_semantics(env):
    env['payload'] = layer([feature(1, POINT)])
    query = {**QUERY, 'from': '20252', 'to': '20252'}
    result = call({'endpoint': 'XPT001', 'query': query, 'decode_pbf': True})
    assert result['geometry_semantics'] == 'nearest_station' and result['property_level_spatial_inference_allowed'] is False
    assert 'prohibited_property_hazard_school_zone_radius_inference' in result['warnings']


def test_schema_exposes_the_optional_flag_only_on_reinfolib_layer():
    assert m4_adapter.DEFINITIONS['reinfolib_layer'][1]['decode_pbf'] == {'type': 'boolean'}
    assert 'decode_pbf' not in m4_adapter.DEFINITIONS['reinfolib_land_values'][1]


def test_processing_note_states_who_decoded(env):
    env['payload'] = layer([feature(1, POINT)])
    decoded = call({'endpoint': 'XKT002', 'query': QUERY, 'decode_pbf': True})
    plain = call({'endpoint': 'XKT002', 'query': QUERY})
    assert 'decoded by this plugin' in decoded['processing'] and 'no spatial analysis or PBF decoding' in plain['processing']
