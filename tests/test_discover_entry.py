"""Register through Hermes discovery, then call the real tool entry."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import client
import schema

ROOT = Path(__file__).resolve().parents[1]
KEY = "unit-secret-key-xyz"


def _copy_plugin(dest: Path) -> None:
    dest.mkdir(parents=True)
    for path in ROOT.iterdir():
        if path.name in {".git", ".pytest_cache", "tests", "__pycache__"} or path.name.startswith("."):
            continue
        if path.is_file():
            shutil.copy2(path, dest / path.name)
        elif path.name in {'skills', 'reinfolib', 'docs'}:
            shutil.copytree(path, dest / path.name)


def _loaded_client(dest: Path):
    root = str(dest.resolve())
    for mod in sys.modules.values():
        file = getattr(mod, "__file__", None) or ""
        if file.endswith("client.py") and root in str(Path(file).resolve()):
            return mod
    raise AssertionError("discovered plugin did not load client.py")


def test_discover_plugins_then_handle_function_call(monkeypatch, tmp_path):
    plugins = pytest.importorskip("hermes_cli.plugins")
    model_tools = pytest.importorskip("model_tools")
    scope = pytest.importorskip("agent.secret_scope")
    constants = pytest.importorskip("hermes_constants")
    home = tmp_path / "hermes"
    dest = home / "plugins" / "jp-reinfolib"
    _copy_plugin(dest)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - jp-reinfolib\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("REINFOLIB_API_KEY", KEY)
    monkeypatch.delenv("REINFOLIB_BASE_URL", raising=False)
    override = constants.set_hermes_home_override(str(home))
    scope.set_multiplex_active(False)
    token = scope.set_secret_scope(None)
    plugins._reset_plugin_managers_for_tests()
    calls = []

    def transport(url, headers, timeout, cap):
        calls.append(url)
        return 200, {"content-type": "application/json"}, _body()

    try:
        assert Path(constants.get_hermes_home()).resolve() == home.resolve()
        plugins.discover_plugins(force=True)
        loaded = _loaded_client(dest)
        from tools.registry import registry
        expected = {'reinfolib_quarter', 'reinfolib_transactions', 'reinfolib_records',
                    'reinfolib_export', 'reinfolib_catalog', 'reinfolib_municipalities',
                    'reinfolib_stations', 'reinfolib_snapshots', 'reinfolib_history',
                    'reinfolib_diff', 'reinfolib_layer', 'reinfolib_land_values',
                    'reinfolib_geocode', 'reinfolib_point_layers'}
        actual_names = set(registry.get_tool_names_for_toolset('jp_reinfolib'))
        assert actual_names == expected
        import re
        manifest_names = set(re.search(r'provides_tools:\n((?:  - .*\n)+)', (dest / 'plugin.yaml').read_text())[1].replace('  - ', '').split())
        assert manifest_names == actual_names
        schemas = [registry.get_schema(name) for name in sorted(actual_names)]
        assert len(schemas) == 14
        print('REGISTERED_SCHEMAS=' + json.dumps({'count': len(schemas), 'names': sorted(actual_names), 'schemas': schemas}))
        loaded.default_transport = transport
        loaded.DEFAULT_GAP = client.Gap(now=lambda: 0.0, sleep=lambda _delay: None)
        refused = json.loads(model_tools.handle_function_call(
            "reinfolib_quarter", {"city": "新宿区", "year": 2025, "quarter": 2},
        ))
        assert refused["detail"] == "bad_city"
        assert "新宿区" not in json.dumps(refused, ensure_ascii=False)
        assert calls == []
        seen = json.loads(model_tools.handle_function_call(
            "reinfolib_quarter", {"city": "13104", "year": 2015, "quarter": 2},
        ))
        assert seen["ok"] is True and seen["count"] == 1 and seen["city"] == "13104"
        assert seen["fetched_at"].endswith("Z")
        assert calls and "city=13104" in calls[0]
        # New tools use the same actual discovered registry, but separate wire contracts.
        query = {'city': '13104', 'year': 2015, 'quarter': 2, 'limit': 1}
        transaction = json.loads(model_tools.handle_function_call('reinfolib_transactions', query))
        assert transaction['ok'] is True and transaction['upstream_count'] == 1
        assert transaction['returned_count'] == 1 and transaction['has_more'] is False
        identifier = transaction['snapshot_id']
        before = len(calls)
        records = json.loads(model_tools.handle_function_call('reinfolib_records', {'snapshot_id': identifier}))
        assert records['records'] == transaction['records'] and len(calls) == before
        exported = json.loads(model_tools.handle_function_call('reinfolib_export', {'snapshot_id': identifier}))
        assert exported['ok'] is True and exported['count'] == 1
        export_path = Path(exported['path'])
        assert export_path.is_relative_to(home)
        assert json.loads(export_path.read_text())['records'] == transaction['records']
        catalog = json.loads(model_tools.handle_function_call('reinfolib_catalog', {}))
        assert catalog['ok'] is True and catalog['endpoint_count'] == 36
        assert len(calls) == before
        snapshots = json.loads(model_tools.handle_function_call('reinfolib_snapshots', {'city': '13104', 'year': 2015, 'quarter': 2}))
        assert snapshots['ok'] and snapshots['snapshot_count'] == 1
        stations = json.loads(model_tools.handle_function_call('reinfolib_stations', {'code': '003696'}))
        assert stations['ok'] and stations['candidate_count'] > 0
        loaded.default_transport = lambda *a: (200, {}, b'{"status":"OK","data":[{"id":"13104","name":"Shinjuku"}]}')
        municipalities = json.loads(model_tools.handle_function_call('reinfolib_municipalities', {'area': '13'}))
        assert municipalities['ok'], municipalities
        research = sys.modules[loaded.__package__ + '.reinfolib.research_tools']
        monkeypatch.setattr(research, '_announcements', lambda end: {'status': 'unavailable', 'detail': 'offline_test'})
        loaded.default_transport = transport
        history = json.loads(model_tools.handle_function_call('reinfolib_history', {
            'city': '13104', 'newest_year': 2015, 'newest_quarter': 2, 'max_quarters': 1, 'max_requests': 1}))
        assert history['ok'] and history['outcome'] == 'found', history
        diff = json.loads(model_tools.handle_function_call('reinfolib_diff', {
            'base_snapshot_id': identifier, 'target_snapshot_id': history['found']['snapshot_id']}))
        assert diff['ok'] and diff['added_n'] == 0 and diff['removed_n'] == 0
        from tests.test_m4_layers import RAW
        loaded.default_transport = lambda *a: (200, {}, json.dumps(RAW).encode())
        tile_query = {'response_format':'geojson','z':14,'x':14551,'y':6450}
        for tool,endpoint,query in [('reinfolib_layer','XKT002',tile_query),
                                    ('reinfolib_land_values','XPT002',{**tile_query,'year':2026})]:
            actual=json.loads(model_tools.handle_function_call(tool,{'endpoint':endpoint,'query':query}))
            assert actual['ok'] is True and actual['raw']==RAW
            assert actual['source_metadata']['source_vintage']
        loaded.default_transport = lambda *a: (200, {}, b'[{"unknown":"retained"}]')
        appraisal=json.loads(model_tools.handle_function_call('reinfolib_land_values',{
            'endpoint':'XCT001','query':{'year':2026,'area':'47','division':'09'}}))
        assert appraisal['ok'] is True and appraisal['raw']==[{'unknown':'retained'}]
        gsi_body = json.dumps([{"geometry": {"coordinates": [139.691772, 35.689629], "type": "Point"},
                                "type": "Feature", "properties": {"addressCode": "", "title": "東京都新宿区西新宿二丁目８番"}}],
                              ensure_ascii=False).encode()
        routed_calls = []
        def routed(url, headers, timeout, cap):
            routed_calls.append(url)
            if url.startswith('https://msearch.gsi.go.jp/address-search/AddressSearch?q='):
                assert KEY not in json.dumps(headers)
                return 200, {}, gsi_body
            return 200, {}, json.dumps(RAW).encode()
        loaded.default_transport = routed
        geo = json.loads(model_tools.handle_function_call('reinfolib_geocode', {'address': '東京都新宿区西新宿2-8-1'}))
        assert geo['ok'] is True and geo['candidates'][0]['matched_level'] == 'block', geo
        overlay = json.loads(model_tools.handle_function_call('reinfolib_point_layers', {
            'address': '東京都新宿区西新宿2-8-1', 'endpoints': ['XKT002'], 'tolerance_m': 5}))
        assert overlay['ok'] is True, overlay
        assert overlay['layers'][0]['relation'] in ('inside', 'within_tolerance', 'outside', 'no_features_in_tile')
        assert overlay['layers'][0]['tiles'] == [[15, 29099, 12902]] and overlay['layers'][0]['safety_inference_allowed'] is False
        refused = json.loads(model_tools.handle_function_call('reinfolib_point_layers', {
            'address': '東京都新宿区西新宿2-8-1', 'endpoints': ['XPT001']}))
        assert refused['detail'] == 'xpt001_not_property_level'
        assert [u.split('?')[0] for u in routed_calls] == ['https://msearch.gsi.go.jp/address-search/AddressSearch',
            'https://msearch.gsi.go.jp/address-search/AddressSearch', 'https://www.reinfolib.mlit.go.jp/ex-api/external/XKT002']
    finally:
        scope.reset_secret_scope(token)
        scope.set_multiplex_active(False)
        constants.reset_hermes_home_override(override)
        plugins._reset_plugin_managers_for_tests()


def _body() -> bytes:
    row = {column["name"]: column["sample"] for column in schema.COLUMNS}
    row["MunicipalityCode"] = "13104"
    return json.dumps({"status": "OK", "data": [row]}, ensure_ascii=False).encode()
