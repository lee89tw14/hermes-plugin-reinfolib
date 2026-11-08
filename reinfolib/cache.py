"""Profile-local query cache for the history probe.

- Negative entries: an XIT001 404 for one exact normalized query, valid for a bounded
  TTL (default 6 h, at most 24 h). Fresh hits are reported as ``cached: true`` with
  ``cached_at``; expired entries are returned with ``stale: true`` and are never served
  as a result, only shown as ``last_known`` next to a failure.
- Latest-snapshot index: the snapshot_id/fetched_at of the last *found* fetch per query,
  so a refresh can name its predecessor. Snapshots themselves stay immutable; a refresh
  always writes a new snapshot through SnapshotStore.

Keys are SHA-256 over endpoint, parser version and the full normalized query; the API
key is never part of a key or entry. Entries are small private files (0600) in a 0700
directory; symlinked directories are refused with CacheUnavailable.
"""
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TTL = 6 * 3600
MAX_TTL = 24 * 3600
DISCARD_AFTER = 30 * 86400
PARSER_VERSION = 1


class CacheUnavailable(Exception):
    pass


def _iso(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class QueryCache:
    def __init__(self, directory, *, ttl=DEFAULT_TTL, clock=None):
        if type(ttl) not in (int, float) or not 0 < ttl <= MAX_TTL:
            raise ValueError('ttl must be within (0, 24h]')
        self.directory = Path(directory)
        self.ttl = ttl
        self._clock = clock or time.time

    @staticmethod
    def key(query, endpoint='XIT001'):
        canonical = json.dumps({'endpoint': endpoint, 'parser': PARSER_VERSION, 'query': query},
                               sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def _dir(self):
        if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
            raise CacheUnavailable('symlink_cache_directory')
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fchmod(fd, 0o700)
            return fd
        except OSError:
            raise CacheUnavailable('cache_unavailable') from None

    def _read(self, name):
        fd = self._dir()
        try:
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError:
                raise CacheUnavailable('cache_unavailable') from None
            try:
                with os.fdopen(file_fd, 'rb') as handle:
                    data = json.loads(handle.read(4096).decode('utf-8'))
            except (ValueError, UnicodeError, OSError):
                return None  # torn/corrupt entry behaves as a miss
            return data if isinstance(data, dict) else None
        finally:
            os.close(fd)

    def _write(self, name, data):
        fd = self._dir()
        temporary = '.tmp-' + secrets.token_hex(8)
        try:
            file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                              dir_fd=fd)
            with os.fdopen(file_fd, 'w') as handle:
                handle.write(json.dumps(data))
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError:
            try:
                os.unlink(temporary, dir_fd=fd)
            except OSError:
                pass
            raise CacheUnavailable('cache_unavailable') from None
        finally:
            os.close(fd)

    def _remove(self, name):
        fd = self._dir()
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError:
            raise CacheUnavailable('cache_unavailable') from None
        finally:
            os.close(fd)

    # -- negative entries -----------------------------------------------------------
    def get_negative(self, query):
        name = self.key(query) + '.neg'
        entry = self._read(name)
        stamp = entry.get('cached_at_s') if entry else None
        if type(stamp) not in (int, float):
            return None
        age = self._clock() - stamp
        if age > DISCARD_AFTER:
            self._remove(name)
            return None
        return {'status_code': 404, 'cached_at': _iso(stamp), 'age_s': max(0, round(age)),
                'stale': not 0 <= age <= self.ttl}

    def put_negative(self, query):
        self._write(self.key(query) + '.neg', {'status_code': 404, 'cached_at_s': self._clock()})

    def clear_negative(self, query):
        self._remove(self.key(query) + '.neg')

    # -- latest snapshot index --------------------------------------------------------
    def latest_snapshot(self, query):
        entry = self._read(self.key(query) + '.latest')
        if not entry or not isinstance(entry.get('snapshot_id'), str) or not isinstance(entry.get('fetched_at'), str):
            return None
        return {'snapshot_id': entry['snapshot_id'], 'fetched_at': entry['fetched_at']}

    def record_snapshot(self, query, snapshot):
        self._write(self.key(query) + '.latest', {'snapshot_id': snapshot['snapshot_id'],
                                                   'fetched_at': snapshot['fetched_at']})
