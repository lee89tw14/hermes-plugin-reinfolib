"""M3-4: negative cache (bounded TTL), stale marking, snapshot index without overwrite."""
import json
from datetime import date

import pytest

from tests.test_m3_history import Fetch, TODAY, tool_env  # noqa: F401  (fixture reuse)

Q = {'city': '13104', 'year': 2026, 'quarter': 4, 'price_classification': '01', 'language': 'ja'}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def cache(tmp_path, clock=None, **kwargs):
    from reinfolib.cache import QueryCache
    return QueryCache(tmp_path / 'cache', clock=clock or Clock(), **kwargs)


def test_negative_entry_is_fresh_then_stale_after_ttl(tmp_path):
    clock = Clock()
    c = cache(tmp_path, clock, ttl=3600)
    assert c.get_negative(Q) is None
    c.put_negative(Q)
    hit = c.get_negative(Q)
    assert hit['stale'] is False and hit['cached_at'] == '2027-01-15T08:00:00Z'
    clock.now += 3601
    assert c.get_negative(Q)['stale'] is True


def test_ttl_is_bounded():
    from reinfolib.cache import QueryCache
    with pytest.raises(ValueError):
        QueryCache('/nonexistent', ttl=7 * 86400)


def test_cache_key_covers_whole_query_and_never_holds_secret(tmp_path):
    c = cache(tmp_path)
    c.put_negative(Q)
    for change in ({'language': 'en'}, {'price_classification': 'both'}, {'quarter': 3}, {'city': '13102'}):
        assert c.get_negative({**Q, **change}) is None
    names = [p.name for p in (tmp_path / 'cache').rglob('*') if p.is_file()]
    assert names and all(len(n.split('.')[0]) == 64 for n in names)
    assert not list((tmp_path / 'cache').rglob('*.json'))


def test_probe_serves_fresh_negatives_from_cache_and_marks_them(tmp_path):
    from reinfolib.history import probe
    clock = Clock()
    c = cache(tmp_path, clock)
    first = Fetch({(2026, 2): 200})
    probe({'city': '13104'}, fetch=first, today=TODAY, cache=c)
    second = Fetch({(2026, 2): 200})
    result = probe({'city': '13104'}, fetch=second, today=TODAY, cache=c)
    assert [(q['year'], q['quarter']) for q in second.queries] == [(2026, 2)]
    assert [a.get('cached') for a in result['attempted']] == [True, True, False]
    assert result['attempted'][0]['cached_at'] and result['attempted'][0]['stale'] is False
    assert 'attempts' not in result['attempted'][0]  # no HTTP happened for a cache hit
    assert result['live_requests'] == 1


def test_expired_negative_is_requested_again(tmp_path):
    from reinfolib.history import probe
    clock = Clock()
    c = cache(tmp_path, clock, ttl=60)
    probe({'city': '13104'}, fetch=Fetch({(2026, 4): 404, (2026, 3): 200}), today=TODAY, cache=c)
    clock.now += 61
    fetch = Fetch({(2026, 4): 200})
    result = probe({'city': '13104'}, fetch=fetch, today=TODAY, cache=c)
    assert [(q['year'], q['quarter']) for q in fetch.queries] == [(2026, 4)]
    assert result['attempted'][0]['cached'] is False and result['outcome'] == 'found'


def test_failure_after_expired_negative_shows_stale_last_known(tmp_path):
    from reinfolib.history import probe
    clock = Clock()
    c = cache(tmp_path, clock, ttl=60)
    probe({'city': '13104'}, fetch=Fetch({(2026, 3): 200}), today=TODAY, cache=c)
    clock.now += 61
    result = probe({'city': '13104'}, fetch=Fetch({(2026, 4): ('upstream', 'network')}), today=TODAY, cache=c)
    first = result['attempted'][0]
    assert first['result'] == 'failed' and first['cached'] is False
    assert first['last_known'] == {'result': 'no_data_404', 'cached_at': '2027-01-15T08:00:00Z', 'stale': True}


def test_found_reports_previous_snapshot_and_clears_negative(tmp_path):
    from reinfolib.history import probe
    clock = Clock()
    c = cache(tmp_path, clock)
    probe({'city': '13104'}, fetch=Fetch({(2026, 3): 200}), today=TODAY, cache=c)
    class Second(Fetch):
        def __call__(self, query):
            snapshot = super().__call__(query)
            return {**snapshot, 'snapshot_id': 'b' * 32}
    first = probe({'city': '13104'}, fetch=Second({(2026, 3): 200}), today=TODAY, cache=c)
    assert first['found']['snapshot_id'] == 'b' * 32
    previous = first['found']['previous_snapshot']
    assert previous['snapshot_id'] == 'a' * 32 and previous['fetched_at'] == '2026-11-08T00:00:00Z'
    assert previous['note'].startswith('earlier fetch')
    c.put_negative({**Q, 'quarter': 3})
    probe({'city': '13104'}, fetch=Fetch({(2026, 3): 200}), today=TODAY, cache=c, use_cache=False)
    assert c.get_negative({**Q, 'quarter': 3}) is None


def test_use_cache_false_bypasses_reads(tmp_path):
    from reinfolib.history import probe
    c = cache(tmp_path)
    probe({'city': '13104'}, fetch=Fetch({(2026, 3): 200}), today=TODAY, cache=c)
    fetch = Fetch({(2026, 3): 200})
    probe({'city': '13104'}, fetch=fetch, today=TODAY, cache=c, use_cache=False)
    assert len(fetch.queries) == 2


def test_symlinked_cache_dir_is_refused(tmp_path):
    from reinfolib.cache import CacheUnavailable, QueryCache
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(CacheUnavailable):
        QueryCache(tmp_path / 'link').put_negative(Q)


def test_tool_refresh_of_old_quarter_creates_new_snapshot_without_overwriting(tool_env, tmp_path):
    from reinfolib import adapter
    calls, published = tool_env
    published[(2026, 3)] = True
    first = json.loads(adapter.dispatch('reinfolib_history', {'city': '13102'}))
    first_id = first['found']['snapshot_id']
    path = tmp_path / 'v2' / 'snapshots' / (first_id + '.json')
    before = path.read_bytes()
    second = json.loads(adapter.dispatch('reinfolib_history', {'city': '13102'}))
    assert calls == [(2026, 4), (2026, 3), (2026, 3)]  # 2026Q4 404 served from cache
    assert second['attempted'][0]['cached'] is True
    assert second['found']['snapshot_id'] != first_id
    assert second['found']['previous_snapshot']['snapshot_id'] == first_id
    assert path.read_bytes() == before
    third = json.loads(adapter.dispatch('reinfolib_history', {'city': '13102', 'use_cache': False}))
    assert calls[-2:] == [(2026, 4), (2026, 3)] and third['attempted'][0]['cached'] is False


def test_tool_survives_unusable_cache_with_warning(tool_env, tmp_path, monkeypatch):
    from reinfolib import research_tools
    from reinfolib.cache import CacheUnavailable
    calls, published = tool_env
    published[(2026, 4)] = True
    class Broken:
        def __init__(self, *a, **k):
            pass
        def __getattr__(self, name):
            def fail(*a, **k):
                raise CacheUnavailable('x')
            return fail
    monkeypatch.setattr(research_tools, 'QueryCache', Broken)
    from reinfolib import adapter
    result = json.loads(adapter.dispatch('reinfolib_history', {'city': '13102'}))
    assert result['outcome'] == 'found' and 'cache_unavailable' in result['warnings']
