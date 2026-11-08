"""Bounded historical probing of XIT001 quarters (publication check).

Publication timing evidence (official API manual):
- terms.md (利用規約): 不動産取引価格情報等は、四半期ごとにデータの追加を行います。ただし、
  不動産取引価格情報においては、掲載以降順次追加される可能性がある.
- index.md (API操作説明 4.): データの更新時期は、トップ画面の「お知らせ」に掲載.
- index.md Q.8: XIT001 returns HTTP 404 when there is no data.
No fixed publication lag is documented in the saved manuals, so the probe starts at the
latest *started* quarter (local date) and walks backward instead of guessing a lag.

A 404 only says "no published rows for this scope and quarter" (not yet published, or
none exist). It is never reported as zero transactions, unchanged prices, or "no data
exists"; a failure is reported separately and stops the walk without a conclusion.
"""
from datetime import date
import time

from .storage import CoreError

EARLIEST = {'01': (2005, 3), 'both': (2005, 3), '02': (2021, 1)}
MAX_QUARTERS = 20
DEFAULT_QUARTERS = 8
SOURCES = ['https://www.reinfolib.mlit.go.jp/help/termsOfUse/',
           'https://www.reinfolib.mlit.go.jp/help/apiManual/']
NOTES = [
    'HTTP 404 (no_data_404) means no published rows for that scope and quarter: not yet published or none exist. '
    'It is not zero transactions and not unchanged prices.',
    'failed means the probe could not be completed; it is not no data. The walk stops there without a conclusion.',
    'MLIT adds transaction data quarterly and may add rows to already published quarters later (terms of use); '
    'the saved manuals document no fixed publication lag. Update dates are announced on the Reinfolib top page.',
    'Counts are API-covered records, not unique transactions.',
]


def latest_started_quarter(today):
    return today.year, (today.month - 1) // 3 + 1


def quarters_back(start, count, *, earliest):
    year, quarter = start
    result = []
    while len(result) < count and (year, quarter) >= earliest:
        result.append((year, quarter))
        year, quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    return result


def _bounded(args, name, low, high, default):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise CoreError('invalid_argument', 'bad_' + name)
    return value


def probe(scope, *, fetch, today=None, max_quarters=DEFAULT_QUARTERS, max_requests=None,
          newest_year=None, newest_quarter=None, price_classification='01', language='ja',
          cache=None, use_cache=True, deadline_seconds=90, clock=None, bounded_fetch=None,
          announcement_lookup=None):
    """Walk quarters newest -> older until one is found or range/budget ends.

    ``fetch(query) -> snapshot`` raises CoreError (attempts attached when available).
    Production supplies ``bounded_fetch(query, absolute_end)`` to cap the inner retry/gate;
    legacy injected fetch callbacks must honor their own timeout. ``announcement_lookup(end)``
    runs before quarter probes, consuming the same end. All clocks use wall-clock seconds.
    ``cache`` (optional, cache.QueryCache) serves fresh 404s and indexes found snapshots;
    ``use_cache=False`` skips reads (results are still recorded). An unusable cache adds the
    warning ``cache_unavailable`` and the walk continues uncached.
    """
    clock = clock or time.time
    deadline_seconds = _bounded({"deadline_seconds": deadline_seconds}, "deadline_seconds", 1, 300, 90)
    end = clock() + deadline_seconds
    warnings = []

    def cached_call(method, *args):
        nonlocal cache
        if cache is None:
            return None
        try:
            return getattr(cache, method)(*args)
        except Exception:  # noqa: BLE001 - the cache is an optimisation, never a blocker
            cache = None
            warnings.append('cache_unavailable')
            return None

    today = today or date.today()
    options = {'max_quarters': max_quarters}
    max_quarters = _bounded(options, 'max_quarters', 1, MAX_QUARTERS, DEFAULT_QUARTERS)
    budget = _bounded({'max_requests': max_quarters if max_requests is None else max_requests},
                      'max_requests', 1, MAX_QUARTERS, max_quarters)
    if (newest_year is None) != (newest_quarter is None):
        raise CoreError('invalid_argument', 'newest_year_and_quarter_together')
    latest = latest_started_quarter(today)
    if newest_year is None:
        start = latest
    else:
        if type(newest_year) is not int or type(newest_quarter) is not int or not 1 <= newest_quarter <= 4:
            raise CoreError('invalid_argument', 'bad_newest_quarter')
        start = (newest_year, newest_quarter)
        if start > latest:
            raise CoreError('invalid_argument', 'future_quarter')
    if price_classification not in EARLIEST:
        raise CoreError('invalid_argument', 'bad_price_classification')
    earliest = EARLIEST[price_classification]
    if start < earliest:
        raise CoreError('invalid_argument', 'before_earliest_available')
    planned = quarters_back(start, max_quarters, earliest=earliest)
    publication = announcement_lookup(end) if announcement_lookup is not None else {"status": "not_checked", "announcements": []}
    attempted, live, sent, found, stop = [], 0, 0, None, None

    def _sent(attempts):
        # HTTP requests actually sent; a fetch without attempt metadata is one GET.
        return len(attempts) if isinstance(attempts, list) else 1
    for year, quarter in planned:
        if clock() >= end:
            stop = "deadline"
            break
        query = {**scope, 'year': year, 'quarter': quarter,
                 'price_classification': price_classification, 'language': language}
        record = {'year': year, 'quarter': quarter, 'period': f'{year}Q{quarter}'}
        cached = cached_call('get_negative', query) if use_cache else None
        if cached is not None and not cached.get('stale'):
            record.update(result='no_data_404', status_code=404, cached=True,
                          cached_at=cached['cached_at'], stale=False)
            attempted.append(record)
            continue
        if live >= budget:
            stop = 'request_budget_exhausted'
            break
        live += 1
        try:
            snapshot = bounded_fetch(query, end) if bounded_fetch is not None else fetch(query)
        except CoreError as error:
            sent += _sent(getattr(error, 'attempts', None))
            record['attempts'] = getattr(error, 'attempts', None) or []
            record['cached'] = False
            if error.error_type == 'not_found':
                record.update(result='no_data_404', status_code=404)
                cached_call('put_negative', query)
                attempted.append(record)
                continue
            record.update(result='failed', error_type=error.error_type, detail=error.detail,
                          status_code=error.status_code)
            if cached is not None:  # expired negative entry: shown as stale, never as fresh
                record['last_known'] = {'result': 'no_data_404', 'cached_at': cached['cached_at'],
                                        'stale': True}
            attempted.append(record)
            stop = 'deadline' if (clock() >= end or error.detail == 'rate_gate_deadline' or
                                  getattr(error, 'stop_reason', None) == 'deadline') else 'failure'
            break
        sent += _sent(snapshot.get('attempts'))
        record.update(result='found', cached=False, attempts=snapshot.get('attempts', []),
                      snapshot_id=snapshot['snapshot_id'], upstream_count=snapshot['upstream_count'])
        attempted.append(record)
        found = {'year': year, 'quarter': quarter, 'period': f'{year}Q{quarter}',
                 'snapshot_id': snapshot['snapshot_id'], 'upstream_count': snapshot['upstream_count'],
                 'fetched_at': snapshot['fetched_at'], 'warnings': list(snapshot.get('warnings', []))}
        previous = cached_call('latest_snapshot', query)
        if previous is not None and previous['snapshot_id'] != snapshot['snapshot_id']:
            found['previous_snapshot'] = {**previous, 'note': 'earlier fetch of the same query; '
                                          'compare with reinfolib_diff. It may have expired after 30 days.'}
        cached_call('record_snapshot', query, snapshot)
        cached_call('clear_negative', query)
        stop = 'found'
        break
    if stop is None:
        stop = ('reached_earliest_available' if len(planned) < max_quarters or
                planned and planned[-1] == earliest else 'range_exhausted')
    outcome = {'found': 'found', 'failure': 'failed',
               'deadline': 'failed', 'request_budget_exhausted': 'budget_exhausted'}.get(stop, 'none_found_in_range')
    return {'ok': True, 'outcome': outcome, 'stop_reason': stop, 'scope': dict(scope),
            'price_classification': price_classification, 'language': language,
            'range': {'newest': f'{start[0]}Q{start[1]}', 'max_quarters': max_quarters,
                      'max_requests': budget, 'deadline_seconds': deadline_seconds, 'earliest_available': f'{earliest[0]}Q{earliest[1]}'},
            'live_requests': sent, 'attempted_periods': live, 'attempted': attempted, 'found': found,
            'publication': publication, 'notes': list(NOTES), 'publication_sources': list(SOURCES), 'warnings': warnings}
