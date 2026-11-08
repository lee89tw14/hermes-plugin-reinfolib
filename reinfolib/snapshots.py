"""List saved snapshots for one effective XIT001 query; local only, no API request.

Refresh semantics: every reinfolib_transactions call performs one GET and writes a new
immutable UUID snapshot. Older snapshots are never modified or overwritten; they remain
until the 30-day retention sweep or an explicit terminal deletion.
"""
import os
import re
import stat
from datetime import date

from .storage import CoreError

REFRESH_POLICY = ('Each reinfolib_transactions call fetches again and writes a new immutable snapshot; '
                  'older snapshots are never overwritten and expire 30 days after they were written. '
                  'Compare snapshots by snapshot_id; a difference in API-covered records is not a new sale.')
SCAN_CAP = 1000
_NAME = re.compile(r'([0-9a-f]{32})\.json')


def list_snapshots(store, query, *, limit=20):
    """Newest first by fetched_at (then write time). Unreadable files are counted, not shown."""
    from .core import validate_query
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CoreError('invalid_argument', 'invalid_limit')
    wanted = validate_query(query, today=date.max)
    try:
        fd = store._open_root()
    except OSError:
        raise CoreError('storage', 'storage_unavailable') from None
    entries = []
    try:
        with os.scandir(fd) as found:
            for entry in found:
                match = _NAME.fullmatch(entry.name)
                if match:
                    info = entry.stat(follow_symlinks=False)
                    entries.append((match[1], info.st_mtime_ns, stat.S_ISREG(info.st_mode)))
    finally:
        os.close(fd)
    entries.sort(key=lambda item: item[1], reverse=True)
    scanned, unreadable, matched = 0, 0, []
    for snapshot_id, mtime_ns, regular in entries[:SCAN_CAP]:
        scanned += 1
        if not regular:
            unreadable += 1
            continue
        try:
            snapshot = store.load(snapshot_id)
        except CoreError as error:
            if error.error_type != 'not_found':
                unreadable += 1
            continue
        if snapshot['query'] == wanted:
            matched.append((snapshot['fetched_at'], mtime_ns, {
                'snapshot_id': snapshot_id, 'fetched_at': snapshot['fetched_at'],
                'upstream_count': snapshot['upstream_count'],
                'warnings': list(snapshot.get('warnings', []))}))
    matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {'ok': True, 'query': wanted, 'snapshot_count': len(matched),
            'snapshots': [item[2] for item in matched[:limit]], 'has_more': len(matched) > limit,
            'scanned_n': scanned, 'unreadable_n': unreadable,
            'scan_truncated': len(entries) > SCAN_CAP, 'refresh_policy': REFRESH_POLICY}
