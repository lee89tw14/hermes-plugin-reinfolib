"""M3-5: snapshot diff reports API-record changes only, never transaction identity."""
import json

import pytest

QUERY = {'city': '13102', 'year': 2015, 'quarter': 2, 'price_classification': '01', 'language': 'ja'}


def row(**fields):
    base = {'Type': '宅地(土地)', 'DistrictName': '銀座', 'DistrictCode': '131020010',
            'TradePrice': '100000000', 'Area': '50', 'Period': '2015年第2四半期', 'PriceCategory': '不動産取引価格情報'}
    return {**base, **fields}


def snap(identifier, records, fetched_at='2026-11-01T00:00:00Z', query=None):
    return {'schema_version': 1, 'snapshot_id': identifier * 32, 'query': query or dict(QUERY),
            'records': records, 'upstream_count': len(records), 'fetched_at': fetched_at, 'warnings': []}


def run(base, target, **kwargs):
    from reinfolib.diff import diff
    return diff(base, target, **kwargs)


def test_identical_snapshots_have_no_api_record_changes():
    records = [row(), row(TradePrice='1')]
    result = run(snap('a', records), snap('b', list(records), '2026-11-08T00:00:00Z'))
    assert (result['added_n'], result['removed_n'], result['unchanged_n']) == (0, 0, 2)
    assert result['no_api_record_changes'] is True
    assert any('not new transactions' in note for note in result['notes'])


def test_additions_and_removals_are_multisets_of_full_records():
    dup = row(TradePrice='5')
    base = snap('a', [row(), dup])
    target = snap('b', [row(), dup, dup, row(Type='中古マンション等', Area='20', TradePrice='9')],
                  '2026-11-08T00:00:00Z')
    result = run(base, target)
    assert result['added_n'] == 2 and result['removed_n'] == 0 and result['unchanged_n'] == 2
    assert dup in result['added']  # a second identical record is an added API record


def test_single_field_change_is_a_possible_revision_not_identity():
    base = snap('a', [row(), row(DistrictName='築地', DistrictCode='131020020')])
    target = snap('b', [row(TradePrice='110000000'), row(DistrictName='築地', DistrictCode='131020099')],
                  '2026-11-08T00:00:00Z')
    result = run(base, target)
    assert result['possible_revision_n'] == 2 and result['added'] == [] and result['removed'] == []
    changed = sorted(tuple(r['changed_fields']) for r in result['possible_revisions'])
    assert changed == [('DistrictCode',), ('TradePrice',)]
    assert result['added_n'] == 2 and result['removed_n'] == 2
    assert any('DistrictCode' in note and 'not a transaction ID' in note for note in result['notes'])
    assert any('heuristic' in note for note in result['notes'])


def test_ambiguous_pairing_is_not_reported_as_revision():
    base = snap('a', [row(TradePrice='1')])
    target = snap('b', [row(TradePrice='2'), row(TradePrice='3')], '2026-11-08T00:00:00Z')
    result = run(base, target)
    assert result['possible_revision_n'] == 0
    assert result['added_n'] == 2 and result['removed_n'] == 1 and len(result['added']) == 2


def test_many_field_change_is_add_and_remove():
    base = snap('a', [row()])
    target = snap('b', [row(TradePrice='1', Area='2', Type='林地')], '2026-11-08T00:00:00Z')
    result = run(base, target)
    assert result['possible_revision_n'] == 0 and len(result['added']) == 1 and len(result['removed']) == 1


def test_different_queries_are_refused():
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        run(snap('a', [row()]), snap('b', [row()], query={**QUERY, 'language': 'en'}))
    assert caught.value.detail == 'query_mismatch'


def test_listing_is_limited_with_counts_kept():
    base = snap('a', [])
    target = snap('b', [row(TradePrice=str(n), Area=str(n), Type=str(n)) for n in range(30)], '2026-11-08T00:00:00Z')
    result = run(base, target, limit=10)
    assert result['added_n'] == 30 and len(result['added']) == 10 and result['truncated'] is True


def test_column_changes_and_order_warning():
    base = snap('a', [row()], '2026-11-08T00:00:00Z')
    target = snap('b', [{**row(), 'NewColumn': 'x'}], '2026-11-01T00:00:00Z')
    result = run(base, target)
    assert result['fields_added'] == ['NewColumn'] and result['fields_removed'] == []
    assert 'target_fetched_before_base' in result['warnings']


def test_diff_tool_dispatch_reads_local_snapshots_only(tmp_path, monkeypatch):
    from reinfolib import adapter
    from reinfolib.storage import SnapshotStore
    import client
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path / 'v2')
    def no_network(*args, **kwargs):
        raise AssertionError('diff must not call the API')
    monkeypatch.setattr(client, 'default_transport', no_network)
    store = SnapshotStore(tmp_path / 'v2' / 'snapshots')
    first = store.save(dict(QUERY), [row()])
    second = store.save(dict(QUERY), [row(TradePrice='2')])
    result = json.loads(adapter.dispatch('reinfolib_diff', {'base_snapshot_id': first['snapshot_id'],
                                                            'target_snapshot_id': second['snapshot_id']}))
    assert result['ok'] is True and result['possible_revision_n'] == 1
    assert result['attribution'] and result['credit']
    bad = json.loads(adapter.dispatch('reinfolib_diff', {'base_snapshot_id': '../x', 'target_snapshot_id': second['snapshot_id']}))
    assert bad['ok'] is False and bad['error_type'] == 'invalid_argument'
    missing = json.loads(adapter.dispatch('reinfolib_diff', {'base_snapshot_id': 'f' * 32,
                                                             'target_snapshot_id': second['snapshot_id']}))
    assert missing['error_type'] == 'not_found'
