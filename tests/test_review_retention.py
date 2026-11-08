"""R-2.1: 30-day retention sweep for snapshots and exports (injected clock)."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from reinfolib import adapter, maintenance
from reinfolib.maintenance import RETENTION_DAYS, sweep_expired

NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=30, seconds=1)).timestamp()
FRESH = (NOW - timedelta(days=29, hours=23)).timestamp()


def make(directory, name, when, text='{}'):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text)
    os.utime(path, (when, when))
    return path


def test_retention_is_thirty_days():
    assert RETENTION_DAYS == 30


def test_expired_snapshot_removed_fresh_kept(tmp_path):
    old = make(tmp_path / 'snapshots', 'a' * 32 + '.json', OLD)
    new = make(tmp_path / 'snapshots', 'b' * 32 + '.json', FRESH)
    result = sweep_expired(tmp_path, now=NOW)
    assert not old.exists() and new.exists()
    assert result == {'ok': True, 'deleted': {'snapshot': 1, 'export': 0}, 'skipped': 0, 'temporary_deleted': 0}


def test_export_set_deleted_as_pair_only_when_all_members_expired(tmp_path):
    ex = tmp_path / 'exports'
    pair = [make(ex, 'c' * 32 + suffix, OLD) for suffix in ('.csv', '.metadata.json')]
    mixed = [make(ex, 'd' * 32 + '.json', OLD), make(ex, 'd' * 32 + '.metadata.json', FRESH)]
    result = sweep_expired(tmp_path, now=NOW)
    assert not any(p.exists() for p in pair)
    assert all(p.exists() for p in mixed)
    assert result['deleted'] == {'snapshot': 0, 'export': 1}


def test_ignores_non_uuid_and_never_follows_symlinks(tmp_path):
    snaps = tmp_path / 'snapshots'
    other = make(snaps, 'notes.json', OLD)
    upper = make(snaps, 'A' * 32 + '.json', OLD)
    outside = make(tmp_path / 'outside', 'e' * 32 + '.json', OLD)
    (snaps / ('e' * 32 + '.json')).symlink_to(outside)
    result = sweep_expired(tmp_path, now=NOW)
    assert other.exists() and upper.exists() and outside.exists()
    assert (snaps / ('e' * 32 + '.json')).is_symlink()
    assert result['deleted'] == {'snapshot': 0, 'export': 0} and result['skipped'] == 1


def test_symlinked_store_directory_is_not_swept(tmp_path):
    real = tmp_path / 'real'
    victim = make(real, 'f' * 32 + '.json', OLD)
    (tmp_path / 'snapshots').symlink_to(real, target_is_directory=True)
    result = sweep_expired(tmp_path, now=NOW)
    assert victim.exists()
    assert result['ok'] is False and result['detail'] == 'retention_cleanup_failed'


def test_missing_store_is_a_clean_no_op(tmp_path):
    assert sweep_expired(tmp_path, now=NOW) == {'ok': True, 'deleted': {'snapshot': 0, 'export': 0}, 'skipped': 0, 'temporary_deleted': 0}


def test_records_reports_not_found_after_expiry(tmp_path, monkeypatch):
    from reinfolib.storage import SnapshotStore
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    q = {'year': 2025, 'city': '13104', 'language': 'ja', 'price_classification': '01'}
    identifier = SnapshotStore(tmp_path / 'snapshots').save(q, [{'Area': '30'}])['snapshot_id']
    path = tmp_path / 'snapshots' / (identifier + '.json')
    os.utime(path, (OLD, OLD))
    sweep_expired(tmp_path, now=NOW)
    result = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': identifier}))
    assert (result['error_type'], result['detail']) == ('not_found', 'snapshot_not_found')


def _fresh_snapshot(tmp_path):
    from reinfolib.storage import SnapshotStore
    q = {'year': 2025, 'city': '13104', 'language': 'ja', 'price_classification': '01'}
    return SnapshotStore(tmp_path / 'snapshots').save(q, [{'Area': '30'}])['snapshot_id']


def test_dispatch_sweeps_expired_artifacts_before_local_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    keep = _fresh_snapshot(tmp_path)
    when = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    stale = make(tmp_path / 'exports', '9' * 32 + '.json', when)
    result = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': keep}))
    assert result['ok'] is True and not stale.exists()
    assert 'retention_cleanup_failed' not in result['warnings']


def test_catalog_does_not_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    when = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    stale = make(tmp_path / 'snapshots', '8' * 32 + '.json', when)
    assert json.loads(adapter.dispatch('reinfolib_catalog', {}))['ok'] is True
    assert stale.exists()


@pytest.mark.parametrize('failure', ['raises', 'reports'])
@pytest.mark.parametrize('tool', ['reinfolib_records', 'reinfolib_export'])
def test_cleanup_failure_never_breaks_main_flow(tmp_path, monkeypatch, failure, tool):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    keep = _fresh_snapshot(tmp_path)

    def broken(root, **kwargs):
        if failure == 'raises':
            raise RuntimeError('synthetic-private-detail /etc/secret')
        return {'ok': False, 'error_type': 'storage', 'detail': 'retention_cleanup_failed'}

    monkeypatch.setattr(maintenance, 'sweep_expired', broken)
    text = adapter.dispatch(tool, {'snapshot_id': keep})
    result = json.loads(text)
    assert result['ok'] is True
    assert 'retention_cleanup_failed' in result['warnings']
    assert 'synthetic-private-detail' not in text and '/etc/secret' not in text
