"""M5 PBF export: opt-in permanent raw tile artifact via reinfolib_layer save_artifact."""
import hashlib
import json
import stat

import pytest

from reinfolib import adapter
from reinfolib.scheduling import RateGate
from tests.test_m5_mvt import feature, layer, POINT, SQUARE

KEY = 'k' * 32
QUERY = {'response_format': 'pbf', 'z': 15, 'x': 29099, 'y': 12902}


@pytest.fixture
def env(monkeypatch, tmp_path):
    import client
    import service
    state = {'calls': [], 'payload': b'', 'secret_calls': 0, 'root': tmp_path / 'v2'}

    def secret(name):
        state['secret_calls'] += 1
        return KEY

    def wire(url, headers, timeout, cap):
        state['calls'].append(url)
        return 200, {}, state['payload']
    monkeypatch.setattr(service, '_secret', secret)
    monkeypatch.setattr(client, 'default_transport', wire)
    monkeypatch.setattr(adapter, 'production_gate', lambda: RateGate(tmp_path / 'rate', spacing=0))
    monkeypatch.setattr(adapter, 'data_root', lambda: state['root'])
    return state


def call(args, name='reinfolib_layer'):
    return json.loads(adapter.dispatch(name, args))


def test_save_artifact_writes_exact_raw_pbf_bytes_and_metadata(env):
    env['payload'] = layer([feature(1, POINT, tags=(0, 0)), feature(3, SQUARE)])
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert result['ok'] is True and result['format'] == 'pbf'
    artifact = result['artifact']
    digest = hashlib.sha256(env['payload']).hexdigest()
    assert artifact['sha256'] == digest and artifact['byte_count'] == len(env['payload'])
    assert artifact['format'] == 'pbf'
    assert artifact['semantics'] == 'raw_mapbox_vector_tile_bytes_not_geojson'
    exports = env['root'] / 'exports'
    pbf = exports / (artifact['artifact_id'] + '.pbf')
    meta_path = exports / (artifact['artifact_id'] + '.metadata.json')
    assert artifact['path'] == str(pbf.resolve()) and artifact['manifest_path'] == str(meta_path.resolve())
    assert pbf.read_bytes() == env['payload']
    assert sorted(p.name for p in exports.iterdir()) == sorted([pbf.name, meta_path.name])
    meta = json.loads(meta_path.read_text())
    assert meta['artifact_id'] == artifact['artifact_id'] and meta['endpoint'] == 'XKT002'
    assert meta['query'] == QUERY and meta['sha256'] == digest and meta['byte_count'] == len(env['payload'])
    assert meta['format'] == 'pbf' and meta['encoding'] == 'raw_bytes' and meta['decoded'] is False
    assert meta['fetched_at'] == result['fetched_at']
    assert meta['source_metadata'] == result['source_metadata']
    assert meta['source_metadata']['source_vintage'] and meta['source_metadata']['terms_url']
    assert 'not GeoJSON' in meta['processing'] and 'raw Mapbox Vector Tile bytes' in meta['processing']
    assert meta['attribution'] and meta['credit'] and meta['retention_days'] == 30
    assert meta['property_level_spatial_inference_allowed'] is False
    assert KEY not in meta_path.read_text() and KEY not in json.dumps(result)
    # Never claims GeoJSON: only the explicit negative semantics label mentions it.
    assert 'geojson' not in json.dumps({k: v for k, v in artifact.items() if k != 'semantics'}).lower()
    assert 'raw' not in result and 'geojson' not in meta['format'] + meta['encoding']
    for path in (pbf, meta_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(exports.stat().st_mode) == 0o700


@pytest.mark.parametrize('args,error', [
    ({'endpoint': 'XKT002', 'query': {**QUERY, 'response_format': 'geojson'}, 'save_artifact': True},
     ('invalid_argument', 'save_artifact_requires_pbf')),
    ({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': 'yes'}, ('invalid_argument', 'bad_save_artifact_flag')),
    ({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': 1}, ('invalid_argument', 'bad_save_artifact_flag')),
    ({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True, 'purpose': 'commercial'},
     ('license_review_required', 'regional_terms_not_cleared')),
    ({'endpoint': 'XKT002', 'query': {**QUERY, 'z': 3}, 'save_artifact': True}, ('invalid_argument', None)),
])
def test_bad_or_commercial_save_fails_before_secret_http_or_files(env, args, error):
    result = call(args)
    assert result['ok'] is False and result['error_type'] == error[0]
    if error[1]:
        assert result['detail'] == error[1]
    assert env['calls'] == [] and env['secret_calls'] == 0
    assert not env['root'].exists()


def test_land_values_tool_does_not_accept_save_artifact(env):
    result = call({'endpoint': 'XPT002', 'query': {**QUERY, 'year': 2025}, 'save_artifact': True},
                  name='reinfolib_land_values')
    assert (result['error_type'], result['detail']) == ('invalid_argument', 'invalid_parameters')
    assert env['calls'] == [] and not env['root'].exists()


@pytest.mark.parametrize('status,kind', [(404, 'not_found'), (401, 'auth'), (400, 'upstream')])
def test_failed_fetch_writes_nothing(env, monkeypatch, status, kind):
    import client
    monkeypatch.setattr(client, 'default_transport', lambda url, headers, timeout, cap: (status, {}, b''))
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert result['ok'] is False and result['error_type'] == kind and 'artifact' not in result
    assert not (env['root'] / 'exports').exists()


def test_without_flag_nothing_is_written(env):
    env['payload'] = layer([feature(1, POINT)])
    result = call({'endpoint': 'XKT002', 'query': QUERY})
    assert result['ok'] is True and 'artifact' not in result and not env['root'].exists()


def test_symlinked_exports_root_is_refused_after_fetch_without_writing(env, tmp_path):
    env['payload'] = layer([feature(1, POINT)])
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    env['root'].mkdir()
    (env['root'] / 'exports').symlink_to(elsewhere, target_is_directory=True)
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'storage', 'symlink_export_root')
    assert list(elsewhere.iterdir()) == [] and len(result['attempts']) == 1


def test_metadata_write_failure_removes_pbf_and_temporaries(env, monkeypatch):
    from reinfolib import tile_export
    env['payload'] = layer([feature(1, POINT)])
    real = tile_export._publish

    def flaky(root, name, content):
        if name.endswith('.metadata.json'):
            raise OSError('disk full')
        return real(root, name, content)
    monkeypatch.setattr(tile_export, '_publish', flaky)
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'storage', 'storage_unavailable')
    assert list((env['root'] / 'exports').iterdir()) == []


def test_empty_200_tile_is_saved_as_zero_byte_artifact_marked_no_features(env):
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert result['ok'] is True and result['status'] == 'no_features'
    artifact = result['artifact']
    assert artifact['byte_count'] == 0 and artifact['sha256'] == hashlib.sha256(b'').hexdigest()
    assert open(artifact['path'], 'rb').read() == b''
    assert json.load(open(artifact['manifest_path']))['status'] == 'no_features'


def test_save_combined_with_decode_keeps_raw_bytes_on_disk(env):
    env['payload'] = layer([feature(3, SQUARE)])
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True, 'decode_pbf': True})
    assert result['decoded'] is True and result['decoded_feature_count'] == 1
    assert open(result['artifact']['path'], 'rb').read() == env['payload']
    assert json.load(open(result['artifact']['manifest_path']))['decoded'] is False


def test_xpt001_artifact_metadata_keeps_nearest_station_semantics(env):
    env['payload'] = layer([feature(1, POINT)])
    query = {**QUERY, 'z': 14, 'x': 14549, 'y': 6451, 'from': '20231', 'to': '20234'}
    result = call({'endpoint': 'XPT001', 'query': query, 'save_artifact': True})
    assert result['ok'] is True, result
    meta = json.load(open(result['artifact']['manifest_path']))
    assert meta['geometry_semantics'] == 'nearest_station'
    assert meta['property_level_spatial_inference_allowed'] is False
    assert 'prohibited_property_hazard_school_zone_radius_inference' in meta['warnings']


# -- retention and deletion integration --------------------------------------------------

import argparse
import os
from datetime import datetime, timedelta, timezone

from reinfolib import cli_commands, maintenance

NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=30, seconds=1)).timestamp()
FRESH = (NOW - timedelta(days=29, hours=23)).timestamp()


def _make(directory, name, when, content=b'\x1a\x00'):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    os.utime(path, (when, when))
    return path


def test_sweep_deletes_expired_pbf_pair_and_keeps_fresh(tmp_path):
    ex = tmp_path / 'exports'
    old = [_make(ex, 'a' * 32 + s, OLD) for s in ('.pbf', '.metadata.json')]
    fresh = [_make(ex, 'b' * 32 + s, FRESH) for s in ('.pbf', '.metadata.json')]
    result = maintenance.sweep_expired(tmp_path, now=NOW)
    assert not any(p.exists() for p in old) and all(p.exists() for p in fresh)
    assert result['deleted'] == {'snapshot': 0, 'export': 1} and result['skipped'] == 0


def test_sweep_keeps_pbf_whose_metadata_is_fresh_and_vice_versa(tmp_path):
    ex = tmp_path / 'exports'
    first = [_make(ex, 'c' * 32 + '.pbf', OLD), _make(ex, 'c' * 32 + '.metadata.json', FRESH)]
    second = [_make(ex, 'd' * 32 + '.pbf', FRESH), _make(ex, 'd' * 32 + '.metadata.json', OLD)]
    result = maintenance.sweep_expired(tmp_path, now=NOW)
    assert all(p.exists() for p in first + second)
    assert result['deleted'] == {'snapshot': 0, 'export': 0}


def test_sweep_never_follows_symlinked_pbf_member(tmp_path):
    ex = tmp_path / 'exports'
    outside = _make(tmp_path / 'outside', 'target.pbf', OLD)
    meta = _make(ex, 'e' * 32 + '.metadata.json', OLD)
    (ex / ('e' * 32 + '.pbf')).symlink_to(outside)
    result = maintenance.sweep_expired(tmp_path, now=NOW)
    assert outside.exists() and meta.exists() and (ex / ('e' * 32 + '.pbf')).is_symlink()
    assert result['skipped'] == 1 and result['deleted']['export'] == 0


def test_delete_export_removes_pbf_and_metadata(tmp_path):
    ex = tmp_path / 'exports'
    paths = [_make(ex, 'f' * 32 + s, FRESH) for s in ('.pbf', '.metadata.json')]
    result = maintenance.delete_artifact(tmp_path, kind='export', artifact_id='f' * 32, confirmed=True)
    assert result['deleted_files'] == 2 and not any(p.exists() for p in paths)


def test_delete_export_refuses_symlinked_pbf(tmp_path):
    ex = tmp_path / 'exports'
    outside = _make(tmp_path / 'outside', 'x.pbf', FRESH)
    meta = _make(ex, '1' * 32 + '.metadata.json', FRESH)
    (ex / ('1' * 32 + '.pbf')).symlink_to(outside)
    with pytest.raises(maintenance.CoreError) as caught:
        maintenance.delete_artifact(tmp_path, kind='export', artifact_id='1' * 32, confirmed=True)
    assert (caught.value.error_type, caught.value.detail) == ('storage', 'unsafe_delete_target')
    assert outside.exists() and meta.exists()


def test_cli_delete_export_removes_a_tool_written_pbf_artifact(env, capsys):
    env['payload'] = layer([feature(1, POINT)])
    artifact = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})['artifact']
    parser = argparse.ArgumentParser()
    cli_commands.setup(parser)
    args = parser.parse_args(['delete', 'export', artifact['artifact_id'], '--yes'])
    assert cli_commands.handle(args, data_root=lambda: env['root']) == 0
    assert json.loads(capsys.readouterr().out)['deleted_files'] == 2
    assert list((env['root'] / 'exports').iterdir()) == []


def test_sweep_removes_a_tool_written_artifact_after_thirty_days(env):
    env['payload'] = layer([feature(1, POINT)])
    artifact = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})['artifact']
    for name in ('path', 'manifest_path'):
        os.utime(artifact[name], (OLD, OLD))
    result = maintenance.sweep_expired(env['root'], now=NOW)
    assert result['deleted']['export'] == 1 and list((env['root'] / 'exports').iterdir()) == []


def test_saving_runs_the_retention_sweep_like_transaction_export(env):
    ex = env['root'] / 'exports'
    expired = [_make(ex, '2' * 32 + s, (datetime.now(timezone.utc) - timedelta(days=31)).timestamp())
               for s in ('.pbf', '.metadata.json')]
    env['payload'] = layer([feature(1, POINT)])
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert result['ok'] is True and 'retention_cleanup_failed' not in result.get('warnings', [])
    assert not any(p.exists() for p in expired)


def test_sweep_failure_is_a_fixed_warning_and_save_still_happens(env, monkeypatch):
    monkeypatch.setattr(maintenance, 'sweep_expired', lambda root: {'ok': False})
    env['payload'] = layer([feature(1, POINT)])
    result = call({'endpoint': 'XKT002', 'query': QUERY, 'save_artifact': True})
    assert result['ok'] is True and result['warnings'] == ['retention_cleanup_failed']
    assert os.path.exists(result['artifact']['path'])
    assert json.load(open(result['artifact']['manifest_path']))['warnings'] == []


def test_no_sweep_without_save_flag(env, monkeypatch):
    monkeypatch.setattr(maintenance, 'sweep_expired', lambda root: pytest.fail('sweep without save'))
    env['payload'] = layer([feature(1, POINT)])
    assert call({'endpoint': 'XKT002', 'query': QUERY})['ok'] is True


# -- disclosure surfaces -------------------------------------------------------------------

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tool_description_discloses_save_artifact_within_listing_limits():
    from reinfolib import m4_adapter
    description, properties, _ = m4_adapter.DEFINITIONS['reinfolib_layer']
    assert properties['save_artifact'] == {'type': 'boolean'}
    assert 'save_artifact' in description and '30 days' in description and len(description) <= 1024
    assert 'save_artifact' not in m4_adapter.DEFINITIONS['reinfolib_land_values'][1]


def test_manifest_skill_and_cli_disclose_persistent_pbf_artifacts():
    manifest = (ROOT / 'plugin.yaml').read_text()
    skill = (ROOT / 'skills/property-transactions/SKILL.md').read_text()
    assert 'save_artifact' in manifest and 'They do not persist\n  snapshots or exports.' not in manifest
    assert 'save_artifact' in skill and 'M4 responses do not create permanent snapshots or exports' not in skill
    assert 'PBF' in cli_commands.DESCRIPTION
