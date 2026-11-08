"""M4 public boundaries must never return private exception text."""
import json
import pytest
from reinfolib import adapter, m4_adapter
from tests.test_m4_layers import QUERY, KEY


@pytest.mark.parametrize('tool,endpoint,query,validator', [
    ('reinfolib_layer', 'XKT002', QUERY, m4_adapter.layers),
    ('reinfolib_land_values', 'XCT001', {'year': 2025, 'area': '47', 'division': '09'}, m4_adapter.land_values),
])
def test_validation_value_error_is_fixed_internal(monkeypatch, tool, endpoint, query, validator):
    def broken(*args):
        raise ValueError('private exception text')
    monkeypatch.setattr(validator, 'validate', broken)
    result = m4_adapter.dispatch(tool, {'endpoint': endpoint, 'query': query})
    assert (result['error_type'], result['detail']) == ('internal', 'local_view_failed')
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('bad', [float('nan'), object()])
def test_m4_encoding_failure_is_fixed_internal(monkeypatch, bad):
    monkeypatch.setattr(m4_adapter, 'dispatch', lambda *a: {'ok': True, 'bad': bad})
    result = json.loads(adapter.dispatch('reinfolib_layer', {}))
    assert result['ok'] is False
    assert (result['error_type'], result['detail']) == ('internal', 'local_view_failed')


@pytest.mark.parametrize('error,kind,detail', [
    (KeyError('private'), 'internal', 'local_view_failed'),
    (RuntimeError('private'), 'internal', 'local_view_failed'),
    (OSError('private'), 'storage', 'storage_unavailable'),
])
@pytest.mark.parametrize('stage', ['validation', 'metadata', 'provenance', 'encoding'])
def test_m4_all_local_boundaries_are_redacted(monkeypatch, tmp_path, error, kind, detail, stage):
    import service, client
    from reinfolib.scheduling import RateGate
    from tests.test_m4_layers import RAW
    payload = json.dumps(RAW).encode()
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(service, '_secret', lambda name: KEY)
    monkeypatch.setattr(client, 'default_transport', lambda *a: (200, {}, payload))
    monkeypatch.setattr(adapter, 'production_gate', lambda: RateGate(tmp_path / 'gate', spacing=0))
    if stage == 'validation':
        monkeypatch.setattr(m4_adapter.layers, 'validate', broken)
    elif stage == 'metadata':
        class BrokenMetadata(dict):
            def __getitem__(self, key):
                raise error
        monkeypatch.setattr(m4_adapter, 'METADATA', BrokenMetadata())
    elif stage == 'provenance':
        monkeypatch.setattr(m4_adapter.layers, 'provenance', broken)
        monkeypatch.setattr(m4_adapter, 'provenance', broken)
    else:
        monkeypatch.setattr(m4_adapter, 'dispatch', lambda *a: {'ok': True})
        monkeypatch.setattr(adapter.json, 'dumps', broken)
    result = json.loads(adapter.dispatch('reinfolib_layer', {'endpoint': 'XKT002', 'query': QUERY}))
    assert (result['error_type'], result['detail']) == (kind, detail)
    assert not result['ok']
    assert 'private' not in repr(result) and KEY not in repr(result)


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit()])
def test_process_control_exceptions_are_not_swallowed(monkeypatch, error):
    def broken(*args):
        raise error
    monkeypatch.setattr(m4_adapter.layers, 'validate', broken)
    with pytest.raises(type(error)):
        adapter.dispatch('reinfolib_layer', {'endpoint': 'XKT002', 'query': QUERY})
