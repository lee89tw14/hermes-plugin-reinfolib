"""M2 slice 4: list saved snapshots for one effective query; refresh never overwrites."""
import json
import os

import pytest

KEY = 'synthetic-test-key-123456789'


class NoWait:
    def run(self, call):
        return call()


def row(i=0):
    return {'Period': '2015年第2四半期', 'MunicipalityCode': '13102', 'TradePrice': str(1000 + i),
            'PriceCategory': '不動産取引価格情報'}


def fetch(store, args, rows):
    from reinfolib.core import fetch_transactions
    payload = json.dumps({'status': 'OK', 'data': rows}, ensure_ascii=False).encode()
    return fetch_transactions(args, key=KEY, storage=store, gap=NoWait(),
                              transport=lambda *a: (200, {}, payload))


QUERY = {'city': '13102', 'year': 2015, 'quarter': 2}


def test_refresh_creates_new_immutable_snapshot_and_listing_is_newest_first(tmp_path):
    from reinfolib.storage import SnapshotStore
    from reinfolib.snapshots import list_snapshots
    store = SnapshotStore(tmp_path)
    first = store.save({**QUERY, 'price_classification': '01', 'language': 'ja'}, [row()],
                       fetched_at='2026-11-01T00:00:00Z')
    before = (tmp_path / (first['snapshot_id'] + '.json')).read_bytes()
    second = fetch(store, QUERY, [row(), row(1)])
    assert second['snapshot_id'] != first['snapshot_id']
    assert (tmp_path / (first['snapshot_id'] + '.json')).read_bytes() == before
    listed = list_snapshots(store, QUERY)
    assert listed['ok'] is True and listed['snapshot_count'] == 2
    assert [s['snapshot_id'] for s in listed['snapshots']] == [second['snapshot_id'], first['snapshot_id']]
    assert [s['upstream_count'] for s in listed['snapshots']] == [2, 1]
    assert listed['snapshots'][1]['fetched_at'] == '2026-11-01T00:00:00Z'
    assert 'records' not in listed and all('records' not in s for s in listed['snapshots'])
    assert 'new immutable snapshot' in listed['refresh_policy']


def test_listing_matches_effective_query_including_defaults(tmp_path):
    from reinfolib.storage import SnapshotStore
    from reinfolib.snapshots import list_snapshots
    store = SnapshotStore(tmp_path)
    base = fetch(store, QUERY, [row()])
    fetch(store, {**QUERY, 'language': 'en'}, [{**row(), 'Period': '2nd quarter 2015'}])
    fetch(store, {**QUERY, 'price_classification': 'both'}, [row()])
    fetch(store, {'city': '13102', 'year': 2015}, [row()])
    explicit = list_snapshots(store, {**QUERY, 'price_classification': '01', 'language': 'ja'})
    assert [s['snapshot_id'] for s in explicit['snapshots']] == [base['snapshot_id']]
    assert list_snapshots(store, QUERY)['snapshot_count'] == 1
    assert list_snapshots(store, {'city': '13103', 'year': 2015})['snapshot_count'] == 0
    assert list_snapshots(store, QUERY)['scanned_n'] == 4


def test_listing_skips_unreadable_and_non_uuid_files_and_never_follows_symlinks(tmp_path):
    from reinfolib.storage import SnapshotStore
    from reinfolib.snapshots import list_snapshots
    store = SnapshotStore(tmp_path / 'snapshots')
    good = fetch(store, QUERY, [row()])
    (tmp_path / 'snapshots' / ('b' * 32 + '.json')).write_text('{broken')
    (tmp_path / 'snapshots' / 'notes.json').write_text('{}')
    outside = tmp_path / 'outside.json'
    outside.write_text((tmp_path / 'snapshots' / (good['snapshot_id'] + '.json')).read_text())
    os.symlink(outside, tmp_path / 'snapshots' / ('c' * 32 + '.json'))
    listed = list_snapshots(store, QUERY)
    assert [s['snapshot_id'] for s in listed['snapshots']] == [good['snapshot_id']]
    assert listed['unreadable_n'] == 2  # the broken file and the symlink


def test_limit_and_argument_validation(tmp_path):
    from reinfolib.storage import CoreError, SnapshotStore
    from reinfolib.snapshots import list_snapshots
    store = SnapshotStore(tmp_path)
    for i in range(3):
        fetch(store, QUERY, [row(i)])
    limited = list_snapshots(store, QUERY, limit=2)
    assert limited['snapshot_count'] == 3 and len(limited['snapshots']) == 2 and limited['has_more'] is True
    for bad in [{}, {'city': '13102'}, {**QUERY, 'snapshot_id': 'a' * 32}]:
        with pytest.raises(CoreError) as caught:
            list_snapshots(store, bad)
        assert caught.value.error_type == 'invalid_argument'
    with pytest.raises(CoreError):
        list_snapshots(store, QUERY, limit=0)


def test_snapshots_tool_dispatch_is_local(tmp_path, monkeypatch):
    from reinfolib import adapter
    from reinfolib.storage import SnapshotStore
    store = SnapshotStore(tmp_path / 'snapshots')
    saved = fetch(store, QUERY, [row()])
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(adapter, 'fetch_snapshot', lambda *a: (_ for _ in ()).throw(AssertionError('no API')))
    result = json.loads(adapter.dispatch('reinfolib_snapshots', QUERY))
    assert result['ok'] is True and result['snapshots'][0]['snapshot_id'] == saved['snapshot_id']
    assert result['credit']
    description, properties, required = adapter.DEFINITIONS['reinfolib_snapshots']
    assert required == ['year'] and 'limit' in properties and 'offset' not in properties
