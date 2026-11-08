"""Hermes v2 adapter registration and safe local dispatch tests."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def adapter():
    path = ROOT / 'reinfolib' / 'adapter.py'
    assert path.exists(), 'v2 Hermes adapter is not implemented'
    from reinfolib import adapter
    return adapter


class Context:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs


def test_register_new_tools_without_network_or_storage(tmp_path, monkeypatch):
    a = adapter()
    def forbidden():
        raise AssertionError('registration must not create storage')
    monkeypatch.setattr(a, 'data_root', forbidden)
    ctx = Context()
    a.register(ctx)
    assert set(ctx.tools) == {'reinfolib_transactions', 'reinfolib_records', 'reinfolib_export', 'reinfolib_catalog', 'reinfolib_municipalities', 'reinfolib_stations', 'reinfolib_snapshots', 'reinfolib_history', 'reinfolib_diff', 'reinfolib_layer', 'reinfolib_land_values', 'reinfolib_geocode', 'reinfolib_point_layers'}
    for name, tool in ctx.tools.items():
        assert tool['schema']['name'] == name
        assert tool['schema']['parameters']['additionalProperties'] is False
        assert tool['toolset'] == 'jp_reinfolib'
    result = json.loads(ctx.tools['reinfolib_catalog']['handler']({}))
    assert result['ok'] is True and result['endpoint_count'] == 36
    assert sum(e['id'] == 'XIT001' for e in result['endpoints']) == 1
    assert next(e for e in result['endpoints'] if e['id'] == 'XIT001')['live_verified'] is True
    assert next(e for e in result['endpoints'] if e['id'] == 'XIT002')['live_verified'] is True
    assert all(e['live_verified'] is True for e in result['endpoints'] if e['id'] not in ('XIT001','XIT002'))


def test_plugin_entry_registers_v2_and_legacy_together():
    spec = importlib.util.spec_from_file_location('reinfolib_entry_v2_test', ROOT / '__init__.py')
    assert spec is not None and spec.loader is not None
    spec.submodule_search_locations = None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class FullContext(Context):
        def register_command(self, *args, **kwargs):
            pass
        def register_skill(self, *args, **kwargs):
            pass
    ctx = FullContext()
    module.register(ctx)
    assert set(ctx.tools) == {'reinfolib_transactions', 'reinfolib_records', 'reinfolib_export', 'reinfolib_catalog', 'reinfolib_municipalities', 'reinfolib_stations', 'reinfolib_snapshots', 'reinfolib_history', 'reinfolib_diff', 'reinfolib_layer', 'reinfolib_land_values', 'reinfolib_geocode', 'reinfolib_point_layers', 'reinfolib_quarter'}
    manifest = (ROOT / 'plugin.yaml').read_text()
    for name in ctx.tools:
        assert f'  - {name}\n' in manifest


def test_records_and_export_dispatch_without_upstream(tmp_path, monkeypatch):
    a = adapter()
    s = {'schema_version': 1, 'snapshot_id': 'a' * 32, 'query': {'city': '13104'},
         'fetched_at': '2026-11-08T00:00:00Z', 'upstream_count': 3,
         'records': [{'TradePrice': str(i), 'Type': 'condo', 'PriceCategory': 'survey', 'Period': '2025Q1'} for i in range(3)]}
    monkeypatch.setattr(a, 'load_snapshot', lambda _id: s, raising=False)
    monkeypatch.setattr(a, 'data_root', lambda: tmp_path)
    page = json.loads(a.dispatch('reinfolib_records', {'snapshot_id': s['snapshot_id'], 'limit': 2}))
    assert page['ok'] is True and page['matched_count'] == 3 and page['returned_count'] == 2
    result = json.loads(a.dispatch('reinfolib_export', {'snapshot_id': s['snapshot_id'], 'format': 'json'}))
    assert result['ok'] is True and result['count'] == 3
    assert json.loads(Path(result['path']).read_text())['records'] == s['records']


@pytest.mark.parametrize('name,args', [
    ('reinfolib_transactions', {'year': 2025, 'city': '13104', 'limit': 0}),
    ('reinfolib_transactions', {'year': 2025, 'city': '13104', 'filters': {'area_min': 'NaN'}}),
    ('reinfolib_records', {'snapshot_id': 'a' * 32, 'unknown': 1}),
    ('reinfolib_export', {'snapshot_id': 'a' * 32, 'format': '../../bad'}),
    ('reinfolib_catalog', {'api_key': 'not-to-echo'}),
    ('reinfolib_records', []), ('reinfolib_records', {}),
])
def test_invalid_arguments_before_io(name, args, monkeypatch):
    a = adapter()
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid arguments reached IO')
    monkeypatch.setattr(a, 'fetch_snapshot', forbidden, raising=False)
    monkeypatch.setattr(a, 'load_snapshot', forbidden, raising=False)
    raw = a.dispatch(name, args)
    payload = json.loads(raw)
    assert payload['ok'] is False and payload['error_type'] == 'invalid_argument'
    assert 'not-to-echo' not in raw
