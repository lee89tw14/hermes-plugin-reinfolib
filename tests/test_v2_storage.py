"""Filesystem snapshot contract (no network and no personal data)."""
import json
import os
import re
import stat

import pytest

from reinfolib.storage import CoreError, SnapshotStore

QUERY = {'year': 2015, 'city': '13102', 'quarter': 2, 'language': 'ja', 'price_classification': '01'}
ROWS = [{'Period': '2015年第2四半期', 'Unknown': None}]
WHEN = '2026-11-08T01:02:03Z'


def test_private_unique_detached_snapshots(tmp_path):
    root = tmp_path / 'snapshots'
    root.mkdir(mode=0o755)
    store = SnapshotStore(root)
    rows = [dict(ROWS[0])]
    query = dict(QUERY)
    first = store.save(query, rows, fetched_at=WHEN)
    second = store.save(query, rows, fetched_at=WHEN)
    assert first['snapshot_id'] != second['snapshot_id']
    assert re.fullmatch('[0-9a-f]{32}', first['snapshot_id'])
    assert first['fetched_at'] == WHEN
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for file in root.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    rows[0]['Unknown'] = 'mutated'
    query['city'] = '13103'
    assert first['records'] == ROWS
    assert first['query'] == QUERY
    first['records'].clear()
    assert store.load(first['snapshot_id']) == second | {'snapshot_id': first['snapshot_id']}


def test_core_error_interface():
    error = CoreError('kind')
    assert (error.error_type, error.detail, error.status_code) == ('kind', '', None)


@pytest.mark.parametrize('identifier', ['../outside', '/etc/passwd', 'A'*32, '0'*31, '0'*33, None, 1, '0'*32+'.json'])
def test_invalid_id_is_safe(tmp_path, identifier):
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError) as caught:
        store.load(identifier)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_argument', 'bad_snapshot_id')


def test_missing_and_corrupt_snapshots(tmp_path):
    store = SnapshotStore(tmp_path)
    identifier = '0'*32
    with pytest.raises(CoreError) as caught:
        store.load(identifier)
    assert (caught.value.error_type, caught.value.detail) == ('not_found', 'snapshot_not_found')
    (tmp_path / (identifier+'.json')).write_text('not json')
    with pytest.raises(CoreError) as caught:
        store.load(identifier)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', 'invalid_snapshot')


@pytest.mark.parametrize('field,value', [('schema_version', 2), ('schema_version', True),
    ('snapshot_id', 'f'*32), ('upstream_count', 999), ('upstream_count', True),
    ('records', [None]), ('records', {}), ('query', []), ('query', {'key':'secret'}),
    ('fetched_at', 'not-a-date'), ('fetched_at', '2026-11-08T01:02:03+09:00')])
def test_corrupt_snapshot_format(tmp_path, field, value):
    store = SnapshotStore(tmp_path)
    result = store.save(QUERY, ROWS, fetched_at=WHEN)
    result[field] = value
    (tmp_path / (result['snapshot_id']+'.json') if field != 'snapshot_id' else
     next(tmp_path.iterdir())).write_text(json.dumps(result))
    identifier = next(tmp_path.iterdir()).stem
    with pytest.raises(CoreError) as caught:
        store.load(identifier)
    expected = 'snapshot_schema_too_new' if field == 'schema_version' and type(value) is int and value == 2 else 'invalid_snapshot'
    assert caught.value.detail == expected


def test_snapshot_symlink_refused(tmp_path):
    store = SnapshotStore(tmp_path / 'snapshots')
    target = tmp_path / 'outside.json'
    target.write_text('{}')
    (store.root / ('0'*32+'.json')).symlink_to(target)
    with pytest.raises(CoreError) as caught:
        store.load('0'*32)
    assert caught.value.detail == 'invalid_snapshot'
    assert target.read_text() == '{}'


def test_root_symlink_refused(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CoreError):
        SnapshotStore(link)


def test_failed_write_never_publishes_partial_snapshot(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    def fail_fsync(fd):
        assert not list(tmp_path.glob('*.json'))
        raise OSError('sensitive filesystem details')
    monkeypatch.setattr(os, 'fsync', fail_fsync)
    with pytest.raises(CoreError) as caught:
        store.save(QUERY, ROWS)
    assert caught.value.detail == 'snapshot_write_failed'
    assert not list(tmp_path.iterdir())


def test_collision_never_overwrites(tmp_path, monkeypatch):
    from reinfolib import storage
    store = SnapshotStore(tmp_path)
    first = store.save(QUERY, ROWS, fetched_at=WHEN)
    class Fixed:
        hex = first['snapshot_id']
    monkeypatch.setattr(storage.uuid, 'uuid4', lambda: Fixed())
    with pytest.raises(CoreError):
        store.save(QUERY, [{'different': 'data'}], fetched_at=WHEN)
    assert store.load(first['snapshot_id']) == first


def test_root_replaced_with_symlink_before_save_refused(tmp_path):
    root = tmp_path / 'root'
    store = SnapshotStore(root)
    root.rename(tmp_path / 'old')
    outside = tmp_path / 'outside'
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CoreError):
        store.save(QUERY, ROWS)
    assert list(outside.iterdir()) == []


def test_bounded_load_never_truncates(tmp_path, monkeypatch):
    from reinfolib import storage
    monkeypatch.setattr(storage, 'SNAPSHOT_CAP', 256, raising=False)
    store = SnapshotStore(tmp_path)
    identifier = '0'*32
    file = tmp_path / (identifier+'.json')
    with file.open('wb') as stream:
        stream.truncate(257)
    with pytest.raises(CoreError) as caught:
        store.load(identifier)
    assert caught.value.detail == 'snapshot_too_large'


def test_bounded_save_never_truncates(tmp_path, monkeypatch):
    from reinfolib import storage
    monkeypatch.setattr(storage, 'SNAPSHOT_CAP', 256, raising=False)
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError) as caught:
        store.save(QUERY, [{'large': 'x'*300}])
    assert caught.value.detail == 'snapshot_too_large'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('rows', [[None], {'not': 'list'}, [{'bad': float('nan')}], [{'bad': object()}]])
def test_invalid_save_does_not_write(tmp_path, rows):
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError) as caught:
        store.save(QUERY, rows)
    assert caught.value.detail == 'invalid_snapshot'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '1e9999'])
def test_nonfinite_snapshot_rejected(tmp_path, number):
    store = SnapshotStore(tmp_path)
    valid = store.save(QUERY, ROWS, fetched_at=WHEN)
    file = tmp_path / (valid['snapshot_id']+'.json')
    content = json.dumps(valid).replace('null', number)
    file.write_text(content)
    with pytest.raises(CoreError) as caught:
        store.load(file.stem)
    assert caught.value.detail == 'invalid_snapshot'


@pytest.mark.parametrize('query,rows,when', [({}, ROWS, WHEN), (QUERY, ROWS, ''),
    (QUERY, [{1:'non-string-key'}], WHEN), (QUERY, [{'tuple': (1,2)}], WHEN)])
def test_save_rejects_noncanonical_data(tmp_path, query, rows, when):
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError):
        store.save(query, rows, fetched_at=when)
    assert not list(tmp_path.iterdir())


def test_duplicate_snapshot_field_refused(tmp_path):
    store = SnapshotStore(tmp_path)
    result = store.save(QUERY, ROWS)
    path = tmp_path / (result['snapshot_id']+'.json')
    path.write_text(json.dumps(result).replace('"schema_version": 1', '"schema_version": 99, "schema_version": 1'))
    with pytest.raises(CoreError):
        store.load(result['snapshot_id'])





