"""M3 research tools: bounded history probing (and later diffs), wired into adapter.py.

Kept separate from adapter.py so the adapter only merges DEFINITIONS/HANDLERS.
Imports of adapter happen lazily inside handlers to avoid an import cycle.
"""
from datetime import date
from dataclasses import replace
from time import time as wall_clock

from .cache import QueryCache
from .storage import CoreError

SCOPE_PROPERTIES = {
    'area': {'type': 'string', 'pattern': '^[0-9]{2}$'},
    'city': {'type': 'string', 'pattern': '^[0-9]{5}$'},
    'station': {'type': 'string', 'pattern': '^[0-9]{6}$'},
    'price_classification': {'type': 'string', 'enum': ['01', '02', 'both']},
    'language': {'type': 'string', 'enum': ['ja', 'en']},
}
HISTORY_PROPERTIES = {
    **SCOPE_PROPERTIES,
    'max_quarters': {'type': 'integer', 'minimum': 1, 'maximum': 20},
    'max_requests': {'type': 'integer', 'minimum': 1, 'maximum': 20},
    'newest_year': {'type': 'integer', 'minimum': 2005},
    'newest_quarter': {'type': 'integer', 'minimum': 1, 'maximum': 4},
    'use_cache': {'type': 'boolean'},
    'deadline_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300},
}

DEFINITIONS = {
    'reinfolib_history': (
        'Find the newest published Japan property sales quarter. '
        'Scope is one station, city or prefecture. '
        'Probes XIT001 quarter by quarter backward from the latest started quarter (or newest_year/newest_quarter) '
        'Checks official homepage announcements first; unavailable announcements do not imply no data. '
        'Probes until rows are found, max_quarters (default 8, at most 20), max_requests, or the total '
        'deadline_seconds (default 90, 1-300; includes announcements, retries and rate waits). '
        'Returns every attempted quarter with result no_data_404, failed or found, HTTP attempts, and the saved '
        'snapshot_id of the found quarter. 404 is not zero sales; failed is not no data. Requests are spaced 1 s. '
        'Recent 404s (6 h) are reused and marked cached with cached_at; use_cache false re-requests them. '
        'A found quarter always creates a new snapshot and names the previous one for reinfolib_diff.',
        HISTORY_PROPERTIES, []),
    'reinfolib_diff': (
        'Compare two saved Japan property sales snapshots. '
        'Both snapshot_ids must come from the same query (scope, period, classification, language). '
        'Reports API-record additions, removals and heuristic possible_revisions with changed fields; '
        'these are API record changes, not new transactions or price changes. Records have no transaction ID '
        'and DistrictCode is not one. Local files only, no API request.',
        {'base_snapshot_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
         'target_snapshot_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
        ['base_snapshot_id', 'target_snapshot_id']),
}


def today():
    return date.today()


def _failure(error):
    from .adapter import AdapterFailure
    return AdapterFailure(error.error_type, error.detail, error.status_code,
                          getattr(error, 'attempts', None))


def _fetch(query, end=None):
    """One quarter through the production gate + retry; raises CoreError."""
    from . import adapter
    from .core import fetch_transactions
    from .storage import SnapshotStore
    if __package__ and '.' in __package__:
        from ..service import _secret
    else:
        from service import _secret
    key = _secret('REINFOLIB_API_KEY')
    policy = adapter.RETRY_POLICY or adapter._retry_policy()
    if end is not None:
        policy = replace(policy, deadline=max(0, min(policy.deadline, end - wall_clock())))
    return fetch_transactions(query, key=key, storage=SnapshotStore(adapter.data_root() / 'snapshots'),
                              gap=adapter.production_gate(), retry=policy,
                              today=today(), absolute_deadline=end, clock=wall_clock)


def _announcements(end):
    from . import adapter
    from .publication import lookup
    return lookup(end=end, clock=wall_clock, gate=adapter.production_gate())


def _cache():
    from . import adapter
    try:
        return QueryCache(adapter.data_root() / 'cache')
    except Exception:  # noqa: BLE001 - probe() reports cache_unavailable on use
        return None


def history(args):
    from .core import validate_query
    from .history import probe
    from .views import provenance
    scope = {k: args[k] for k in ('area', 'city', 'station') if k in args}
    classification = args.get('price_classification', '01')
    language = args.get('language', 'ja')
    clock = today()
    use_cache = args.get('use_cache', True)
    if type(use_cache) is not bool:
        from .adapter import AdapterFailure
        raise AdapterFailure('invalid_argument', 'bad_use_cache')
    try:
        # Shape check of the scope before any request (year is only a placeholder).
        validate_query({**scope, 'year': clock.year, 'price_classification': classification,
                        'language': language}, today=clock)
        result = probe(scope, fetch=_fetch, today=clock, max_quarters=args.get('max_quarters', 8),
                       max_requests=args.get('max_requests'), newest_year=args.get('newest_year'),
                       newest_quarter=args.get('newest_quarter'), price_classification=classification,
                       language=language, cache=_cache(), use_cache=use_cache,
                       deadline_seconds=args.get("deadline_seconds", 90), clock=wall_clock, bounded_fetch=_fetch,
                       announcement_lookup=_announcements)
    except CoreError as error:
        raise _failure(error) from None
    return {**result, **provenance()}


def snapshot_diff(args):
    import re
    from .adapter import load_snapshot
    from .diff import diff
    from .views import ViewError, provenance
    ids = [args.get('base_snapshot_id'), args.get('target_snapshot_id')]
    if any(not isinstance(i, str) or not re.fullmatch('[0-9a-f]{32}', i) for i in ids):
        raise ViewError('invalid_argument', 'invalid_snapshot_id')
    base, target = (load_snapshot(i) for i in ids)
    try:
        result = diff(base, target, limit=args.get('limit', 20))
    except CoreError as error:
        raise _failure(error) from None
    return {**result, **provenance()}


HANDLERS = {'reinfolib_history': history, 'reinfolib_diff': snapshot_diff}
