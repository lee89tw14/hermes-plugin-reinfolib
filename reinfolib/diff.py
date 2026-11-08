"""Compare two immutable snapshots of the same query as multisets of API records.

XIT001 records carry no transaction identifier. DistrictCode is an area code that the
manual says may change on data updates, so it is not used as identity. Records are
compared by their full canonical content:

- added / removed: multiset difference of whole records (identical duplicates count).
- possible_revisions: a removed and an added record that are each other's unique nearest
  match and differ in at most MAX_REVISION_FIELDS fields. This is a heuristic pairing of
  API records, not evidence that both describe the same transaction.

Nothing here says a record is a new transaction or that prices changed.
"""
import json
from collections import Counter

from .storage import CoreError

MAX_REVISION_FIELDS = 2
MAX_PAIRING_WORK = 250_000
NOTES = [
    'These are API record changes between two fetches of the same query, not new transactions '
    'and not price changes. An added record can be a late-published row, a correction or a re-publication.',
    'XIT001 records have no transaction ID. DistrictCode is not a transaction ID and may change on data updates '
    '(XIT001 manual).',
    'possible_revisions is a heuristic: a removed and an added record that are each other\'s unique nearest match '
    f'and differ in at most {MAX_REVISION_FIELDS} fields. It does not prove they describe the same transaction.',
    'added_n and removed_n count every differing record, including those listed in possible_revisions; '
    'the added and removed lists exclude paired records.',
    'Counts are API-covered records, not unique transactions.',
]


def _canonical(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _changed(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k, None) != b.get(k, None) or (k in a) != (k in b))


def _expand(counter):
    return [json.loads(text) for text, n in sorted(counter.items()) for _ in range(n)]


def _pairs(removed, added):
    def nearest(record, pool):
        scored = [(len(_changed(record, other)), index) for index, other in enumerate(pool)]
        scored = [s for s in scored if s[0] <= MAX_REVISION_FIELDS]
        if not scored:
            return None
        best = min(s[0] for s in scored)
        winners = [i for d, i in scored if d == best]
        return winners[0] if len(winners) == 1 else None
    forward = [nearest(r, added) for r in removed]
    backward = [nearest(a, removed) for a in added]
    return [(i, j) for i, j in enumerate(forward) if j is not None and backward[j] == i]


def _summary(snapshot):
    return {'snapshot_id': snapshot['snapshot_id'], 'fetched_at': snapshot['fetched_at'],
            'upstream_count': snapshot['upstream_count']}


def diff(base, target, *, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CoreError('invalid_argument', 'bad_limit')
    if base['query'] != target['query']:
        raise CoreError('invalid_argument', 'query_mismatch')
    before = Counter(_canonical(r) for r in base['records'])
    after = Counter(_canonical(r) for r in target['records'])
    removed, added = _expand(before - after), _expand(after - before)
    warnings = []
    if target['fetched_at'] < base['fetched_at']:
        warnings.append('target_fetched_before_base')
    if len(removed) * len(added) > MAX_PAIRING_WORK:
        pairs = []
        warnings.append('revision_pairing_skipped_too_many_changes')
    else:
        pairs = _pairs(removed, added)
    paired_removed = {i for i, _ in pairs}
    paired_added = {j for _, j in pairs}
    revisions = [{'changed_fields': _changed(removed[i], added[j]), 'before': removed[i], 'after': added[j]}
                 for i, j in pairs]
    only_added = [r for j, r in enumerate(added) if j not in paired_added]
    only_removed = [r for i, r in enumerate(removed) if i not in paired_removed]
    fields_before = set().union(*(r.keys() for r in base['records'])) if base['records'] else set()
    fields_after = set().union(*(r.keys() for r in target['records'])) if target['records'] else set()
    truncated = len(only_added) > limit or len(only_removed) > limit or len(revisions) > limit
    return {'ok': True, 'query': dict(base['query']), 'base': _summary(base), 'target': _summary(target),
            'comparison': 'api_record_multiset',
            'added_n': len(added), 'removed_n': len(removed),
            'unchanged_n': sum((before & after).values()), 'possible_revision_n': len(revisions),
            'no_api_record_changes': not added and not removed,
            'added': only_added[:limit], 'removed': only_removed[:limit],
            'possible_revisions': revisions[:limit], 'limit': limit, 'truncated': truncated,
            'fields_added': sorted(fields_after - fields_before),
            'fields_removed': sorted(fields_before - fields_after),
            'warnings': warnings, 'notes': list(NOTES)}
