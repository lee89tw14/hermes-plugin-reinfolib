"""Immutable raw transaction snapshots (schema version 1)."""
import json
import os
import stat
import secrets
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

SNAPSHOT_CAP = 32_000_000
CURRENT_SCHEMA_VERSION = 1
# Trusted, one-version transforms only. No pre-v1 format has been released.
MIGRATIONS = {}


class CoreError(Exception):
    def __init__(self, error_type: str, detail: str = '', status_code: int | None = None):
        super().__init__(error_type)
        self.error_type = error_type
        self.detail = detail
        self.status_code = status_code


def _period(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'([0-9]{4})年第([1-4])四半期', value)
    if match:
        return int(match[1]), int(match[2])
    match = re.fullmatch(r'(1st|2nd|3rd|4th) quarter ([0-9]{4})', value)
    if match:
        return int(match[2]), int(match[1][0])
    return None


def _warnings(query, records):
    warnings = []
    if any(_period(row.get('Period')) is None for row in records):
        warnings.append('unrecognized_period')
    if query.get('price_classification') == 'both' and query.get('year', 2021) < 2021:
        warnings.append('classification_02_available_from_2021')
    return warnings


class SnapshotStore:
    """Private POSIX store: UUID names, no-follow reads, atomic no-clobber writes.

    The caller supplies its profile data directory. No key, profile lookup,
    mutable index, expiry policy, or network dependency belongs in this store.
    """
    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            self._check_parents()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = self._open_root()
            try:
                os.fchmod(fd, 0o700)
            finally:
                os.close(fd)
        except OSError:
            raise CoreError('storage', 'storage_unavailable') from None

    def _check_parents(self):
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise OSError('symlink_storage_root')

    def _open_root(self):
        self._check_parents()
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def save(self, query, records, fetched_at=None) -> dict:
        """Create a detached immutable snapshot; optional fetched_at must be UTC ISO text."""
        try:
            snapshot = dict(schema_version=CURRENT_SCHEMA_VERSION, snapshot_id=uuid.uuid4().hex,
                            query=query, records=records, upstream_count=len(records),
                            fetched_at=fetched_at if fetched_at is not None else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            _validate_snapshot(snapshot, snapshot['snapshot_id'])
            snapshot['warnings'] = _warnings(query, records)
            encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, CoreError):
            raise CoreError('invalid_response', 'invalid_snapshot') from None
        if len(encoded) > SNAPSHOT_CAP:
            raise CoreError('invalid_response', 'snapshot_too_large')
        root_fd = None
        temporary = '.tmp-' + secrets.token_hex(16)
        published = False
        destination = snapshot['snapshot_id'] + '.json'
        try:
            root_fd = self._open_root()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=root_fd)
            with os.fdopen(fd, 'wb') as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            # Atomic no-clobber publication: unlike replace(), link cannot overwrite
            # an existing immutable snapshot, even on a UUID collision.
            os.link(temporary, destination, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                    follow_symlinks=False)
            published = True
            os.fsync(root_fd)
        except OSError:
            if published:
                try:
                    os.unlink(destination, dir_fd=root_fd)
                except OSError:
                    pass
            raise CoreError('storage', 'snapshot_write_failed') from None
        finally:
            if root_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except OSError:
                    pass
                os.close(root_fd)
        return json.loads(encoded)

    def load(self, snapshot_id) -> dict:
        """Bound reads, migrate registered older formats in memory, validate current format."""
        if not isinstance(snapshot_id, str) or not re.fullmatch(r'[0-9a-f]{32}', snapshot_id):
            raise CoreError('invalid_argument', 'bad_snapshot_id')
        try:
            root_fd = self._open_root()
            try:
                fd = os.open(snapshot_id + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=root_fd)
                with os.fdopen(fd, 'rb') as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise CoreError('invalid_response', 'invalid_snapshot')
                    if os.fstat(file.fileno()).st_size > SNAPSHOT_CAP:
                        raise CoreError('invalid_response', 'snapshot_too_large')
                    payload = file.read(SNAPSHOT_CAP + 1)
                    if len(payload) > SNAPSHOT_CAP:
                        raise CoreError('invalid_response', 'snapshot_too_large')
            finally:
                os.close(root_fd)
        except FileNotFoundError:
            raise CoreError('not_found', 'snapshot_not_found') from None
        except OSError:
            raise CoreError('invalid_response', 'invalid_snapshot') from None
        try:
            snapshot = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise CoreError('invalid_response', 'invalid_snapshot') from None
        if (isinstance(snapshot, dict) and type(snapshot.get('schema_version')) is int
                and snapshot['schema_version'] > CURRENT_SCHEMA_VERSION):
            raise CoreError('invalid_response', 'snapshot_schema_too_new')
        original_version = snapshot.get('schema_version') if isinstance(snapshot, dict) else None
        version = original_version
        while type(version) is int and version < CURRENT_SCHEMA_VERSION:
            migrate = MIGRATIONS.get(version)
            if migrate is None:
                raise CoreError('invalid_response', 'snapshot_schema_unsupported')
            try:
                _validate_json(snapshot)
                snapshot = migrate(snapshot)
                if (not isinstance(snapshot, dict)
                        or type(snapshot.get('schema_version')) is not int
                        or snapshot['schema_version'] != version + 1):
                    raise ValueError('migration_version')
                version = snapshot['schema_version']
            except Exception:
                raise CoreError('invalid_response', 'invalid_snapshot') from None
        try:
            _validate_snapshot(snapshot, snapshot_id)
            if original_version != CURRENT_SCHEMA_VERSION:
                snapshot['migrated_from_schema_version'] = original_version
                snapshot['warnings'] = list(snapshot.get('warnings', [])) + [
                    f'snapshot_migrated_from_schema_{original_version}']
            return snapshot
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, CoreError):
            raise CoreError('invalid_response', 'invalid_snapshot') from None


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate_json_field')
        result[name] = value
    return result


def _validate_json(value, depth=0):
    if depth > 32:
        raise ValueError('json_depth')
    if value is None or type(value) in (str, bool, int):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item, depth + 1)
        return
    raise ValueError('json_shape')


def _validate_snapshot(snapshot, snapshot_id):
    _validate_json(snapshot)
    if not isinstance(snapshot, dict):
        raise ValueError('format')
    if type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != CURRENT_SCHEMA_VERSION:
        raise ValueError('version')
    if snapshot.get('snapshot_id') != snapshot_id:
        raise ValueError('id')
    warnings = snapshot.get('warnings', [])
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError('warnings')
    records = snapshot.get('records')
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError('records')
    if type(snapshot.get('upstream_count')) is not int or snapshot['upstream_count'] != len(records):
        raise ValueError('count')
    when = datetime.fromisoformat(snapshot['fetched_at'])
    if when.utcoffset() is None or when.utcoffset().total_seconds() != 0:
        raise ValueError('timestamp')
    # Local import avoids an initialization cycle; errors and raw format live here.
    from .core import validate_query
    # Durable format validation is clock-independent; fetch validates against its injected clock.
    if validate_query(snapshot.get('query'), today=date.max) != snapshot['query']:
        raise ValueError('query')
