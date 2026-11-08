"""Review 9c94843 R-3: crash-left temporary files are swept after the retention window."""
import os
from datetime import datetime, timedelta, timezone

from reinfolib.maintenance import sweep_expired

NOW = datetime(2026, 12, 31, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=30, seconds=1)).timestamp()
FRESH = (NOW - timedelta(days=29)).timestamp()
SNAP_TMP = '.tmp-' + 'a' * 32
EXPORT_TMP = '.pending-ab12_xyz'


def make(directory, name, when):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b'partial')
    os.utime(path, (when, when))
    return path


def test_expired_crash_temporaries_are_deleted_and_counted(tmp_path):
    snap = make(tmp_path / 'snapshots', SNAP_TMP, OLD)
    export = make(tmp_path / 'exports', EXPORT_TMP, OLD)
    result = sweep_expired(tmp_path, now=NOW)
    assert not snap.exists() and not export.exists()
    assert result['ok'] is True and result['temporary_deleted'] == 2


def test_fresh_temporaries_may_belong_to_a_running_write(tmp_path):
    snap = make(tmp_path / 'snapshots', SNAP_TMP, FRESH)
    export = make(tmp_path / 'exports', EXPORT_TMP, FRESH)
    result = sweep_expired(tmp_path, now=NOW)
    assert snap.exists() and export.exists() and result['temporary_deleted'] == 0


def test_only_each_stores_own_prefix_and_never_symlinks(tmp_path):
    outside = make(tmp_path / 'outside', 'victim', OLD)
    (tmp_path / 'snapshots').mkdir()
    (tmp_path / 'snapshots' / SNAP_TMP).symlink_to(outside)
    wrong_place = make(tmp_path / 'snapshots', EXPORT_TMP, OLD)   # export prefix in snapshots
    wrong_name = make(tmp_path / 'exports', '.tmp-notes', OLD)
    other = make(tmp_path / 'exports', 'notes.pending-x', OLD)
    result = sweep_expired(tmp_path, now=NOW)
    assert outside.exists() and (tmp_path / 'snapshots' / SNAP_TMP).is_symlink()
    assert wrong_place.exists() and wrong_name.exists() and other.exists()
    assert result['temporary_deleted'] == 0


def test_temporary_directory_is_not_removed(tmp_path):
    folder = tmp_path / 'snapshots' / SNAP_TMP
    folder.mkdir(parents=True)
    os.utime(folder, (OLD, OLD))
    assert sweep_expired(tmp_path, now=NOW)['temporary_deleted'] == 0 and folder.is_dir()
