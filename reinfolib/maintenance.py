"""Profile-local UUID artifact deletion: explicit deletion and a 30-day retention sweep."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import stat
from .storage import CoreError

RETENTION_DAYS = 30
_NAME = re.compile(r'([0-9a-f]{32})(\.json|\.csv|\.pbf|\.metadata\.json)')
# Export sets: transaction JSON/CSV or a raw PBF tile artifact, each with its metadata.
_SUFFIXES = {'snapshot': ('.json',), 'export': ('.json', '.csv', '.pbf', '.metadata.json')}
# Names written by storage.save (snapshots) and views._publish (exports, tempfile.mkstemp)
# before an atomic rename; a killed process leaves them behind.
_TEMPORARY = {'snapshots': re.compile(r'\.tmp-[0-9a-f]{32}'),
              'exports': re.compile(r'\.pending-[a-z0-9_]{1,32}')}


def _sweep_temporaries(directory, pattern, cutoff):
    """Unlink expired crash-left temporaries: regular files only, never via symlinks."""
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise CoreError('storage', 'unsafe_delete_target')
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    deleted = 0
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries if pattern.fullmatch(entry.name)]
        for name in names:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                    os.unlink(name, dir_fd=fd)
                    deleted += 1
            except FileNotFoundError:
                continue
    finally:
        os.close(fd)
    return deleted


def _groups(directory, kind):
    """UUID -> (all members regular, newest mtime); never follows symlinks."""
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise CoreError('storage', 'unsafe_delete_target')
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    groups = {}
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                match = _NAME.fullmatch(entry.name)
                if not match or match.group(2) not in _SUFFIXES[kind]:
                    continue
                info = entry.stat(follow_symlinks=False)
                regular, newest = groups.get(match.group(1), (True, 0.0))
                groups[match.group(1)] = (regular and stat.S_ISREG(info.st_mode),
                                          max(newest, info.st_mtime))
    finally:
        os.close(fd)
    return groups


def sweep_expired(root, *, now=None, retention_days=RETENTION_DAYS):
    """Delete snapshots and export sets whose newest file is older than the retention window.

    Age is the file write time (artifacts are written once and never rewritten). Export
    files sharing a UUID are deleted together only when every member is expired. Errors
    return a fixed detail instead of raising, so callers can continue their main work."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    deleted, skipped, temporary = {'snapshot': 0, 'export': 0}, 0, 0
    try:
        for folder, pattern in _TEMPORARY.items():
            temporary += _sweep_temporaries(Path(root) / folder, pattern, cutoff)
        for kind, folder in (('snapshot', 'snapshots'), ('export', 'exports')):
            for artifact_id, (regular, newest) in sorted(_groups(Path(root) / folder, kind).items()):
                if not regular:
                    skipped += 1
                    continue
                if newest >= cutoff:
                    continue
                try:
                    delete_artifact(root, kind=kind, artifact_id=artifact_id, confirmed=True)
                    deleted[kind] += 1
                except CoreError as error:
                    if error.error_type != 'not_found':
                        skipped += 1
    except (CoreError, OSError):
        return {'ok': False, 'error_type': 'storage', 'detail': 'retention_cleanup_failed',
                'deleted': deleted, 'skipped': skipped, 'temporary_deleted': temporary}
    return {'ok': True, 'deleted': deleted, 'skipped': skipped, 'temporary_deleted': temporary}


def validate_artifact_ref(kind, artifact_id):
    if kind not in ('snapshot', 'export'):
        raise CoreError('invalid_argument', 'bad_artifact_kind')
    if not isinstance(artifact_id, str) or not re.fullmatch('[0-9a-f]{32}', artifact_id):
        raise CoreError('invalid_argument', 'bad_artifact_id')


def delete_artifact(root, *, kind, artifact_id, confirmed):
    """Delete exactly one UUID snapshot or export set; never cascade between kinds."""
    validate_artifact_ref(kind, artifact_id)
    if confirmed is not True:
        raise CoreError('invalid_argument', 'confirmation_required')
    directory = Path(root) / ('snapshots' if kind == 'snapshot' else 'exports')
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise CoreError('storage', 'unsafe_delete_target')
    names = [artifact_id + suffix for suffix in _SUFFIXES[kind]]
    fd = None
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        existing = []
        for name in names:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise CoreError('storage', 'unsafe_delete_target')
            existing.append(name)
        if not existing:
            raise CoreError('not_found', 'artifact_not_found')
        for name in existing:
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    except FileNotFoundError:
        raise CoreError('not_found', 'artifact_not_found') from None
    except OSError:
        raise CoreError('storage', 'artifact_delete_failed') from None
    finally:
        if fd is not None:
            os.close(fd)
    return {'ok': True, 'kind': kind, 'artifact_id': artifact_id,
            'deleted_files': len(existing), 'cascade': False}
