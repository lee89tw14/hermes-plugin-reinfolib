"""M2 slice 5: snapshot schema_version handling (migrate older in memory, fail closed on newer)."""
import json

import pytest

ID = 'a' * 32


def snapshot(version=1, **extra):
    return {'schema_version': version, 'snapshot_id': ID,
            'query': {'city': '13104', 'year': 2025, 'price_classification': '01', 'language': 'ja'},
            'fetched_at': '2026-11-08T00:00:00Z', 'upstream_count': 1,
            'records': [{'TradePrice': '100', 'Period': '2025年第1四半期'}], 'warnings': [], **extra}


def write(tmp_path, data):
    from reinfolib.storage import SnapshotStore
    store = SnapshotStore(tmp_path)
    path = tmp_path / (ID + '.json')
    path.write_text(json.dumps(data, ensure_ascii=False))
    path.chmod(0o600)
    return store, path


def test_current_version_constant_and_save_writes_it(tmp_path):
    from reinfolib import storage
    assert storage.CURRENT_SCHEMA_VERSION == 1
    saved = storage.SnapshotStore(tmp_path).save(snapshot()['query'], snapshot()['records'])
    assert saved['schema_version'] == storage.CURRENT_SCHEMA_VERSION


@pytest.mark.parametrize('version', [2, 99])
def test_future_version_fails_closed_with_fixed_error_and_file_untouched(tmp_path, version):
    from reinfolib.storage import CoreError
    store, path = write(tmp_path, snapshot(version))
    before = path.read_bytes()
    with pytest.raises(CoreError) as caught:
        store.load(ID)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', 'snapshot_schema_too_new')
    assert path.read_bytes() == before


@pytest.mark.parametrize('version', [0, -1])
def test_older_version_without_migration_is_unsupported(tmp_path, version):
    from reinfolib.storage import CoreError
    store, _ = write(tmp_path, snapshot(version))
    with pytest.raises(CoreError) as caught:
        store.load(ID)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', 'snapshot_schema_unsupported')


@pytest.mark.parametrize('version', [True, '1', 1.0, None])
def test_non_integer_version_is_invalid_snapshot(tmp_path, version):
    from reinfolib.storage import CoreError
    store, _ = write(tmp_path, snapshot(version))
    with pytest.raises(CoreError) as caught:
        store.load(ID)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', 'invalid_snapshot')


def test_registered_migration_runs_in_memory_and_records_note(tmp_path, monkeypatch):
    from reinfolib import storage

    def from_zero(old):
        new = dict(old)
        new['schema_version'] = 1
        new['records'] = old.pop('rows')
        new.pop('rows')
        return new
    monkeypatch.setitem(storage.MIGRATIONS, 0, from_zero)
    legacy = snapshot(0)
    legacy['rows'] = legacy.pop('records')
    store, path = write(tmp_path, legacy)
    before = path.read_bytes()
    loaded = store.load(ID)
    assert loaded['schema_version'] == 1 and loaded['records'] == snapshot()['records']
    assert loaded['migrated_from_schema_version'] == 0
    assert 'snapshot_migrated_from_schema_0' in loaded['warnings']
    assert path.read_bytes() == before  # never rewritten on disk
    from reinfolib.views import page
    assert 'snapshot_migrated_from_schema_0' in page(loaded)['warnings']


def test_migration_producing_invalid_snapshot_is_invalid(tmp_path, monkeypatch):
    from reinfolib import storage
    monkeypatch.setitem(storage.MIGRATIONS, 0, lambda old: {**old, 'schema_version': 1, 'upstream_count': 5})
    store, _ = write(tmp_path, snapshot(0))
    with pytest.raises(storage.CoreError) as caught:
        store.load(ID)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', 'invalid_snapshot')


def test_migration_must_advance_exactly_one_version(tmp_path, monkeypatch):
    from reinfolib import storage
    monkeypatch.setitem(storage.MIGRATIONS, 0, lambda old: {**old, 'schema_version': 0})
    store, _ = write(tmp_path, snapshot(0))
    with pytest.raises(storage.CoreError) as caught:
        store.load(ID)
    assert caught.value.detail == 'invalid_snapshot'


def test_records_tool_reports_future_schema_as_fixed_error(tmp_path, monkeypatch):
    from reinfolib import adapter
    (tmp_path / 'snapshots').mkdir(mode=0o700)
    write(tmp_path / 'snapshots', snapshot(2))
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    result = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': ID}))
    assert (result['ok'], result['error_type'], result['detail']) == (False, 'invalid_response', 'snapshot_schema_too_new')
