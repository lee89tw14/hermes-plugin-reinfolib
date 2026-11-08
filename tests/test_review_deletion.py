"""P2-2: explicit UUID-only deletion, without automatic retention policies."""
from pathlib import Path
import pytest
from reinfolib.storage import CoreError, SnapshotStore


def delete(root, kind, identifier, confirmed=True):
    module = Path(__file__).resolve().parents[1] / 'reinfolib' / 'maintenance.py'
    assert module.exists(), 'explicit artifact deletion is not implemented'
    from reinfolib.maintenance import delete_artifact
    return delete_artifact(root, kind=kind, artifact_id=identifier, confirmed=confirmed)


def test_delete_snapshot_leaves_other_snapshot_and_exports(tmp_path):
    store = SnapshotStore(tmp_path / 'snapshots')
    q = {'year': 2025, 'city': '13104', 'language': 'ja', 'price_classification': '01'}
    one = store.save(q, [{'Area': '30'}])
    two = store.save(q, [{'Area': '40'}])
    exports = tmp_path / 'exports'
    exports.mkdir()
    existing = exports / (one['snapshot_id'] + '.json')
    existing.write_text('separate export')
    result = delete(tmp_path, 'snapshot', one['snapshot_id'])
    assert result['ok'] is True and result['deleted_files'] == 1
    with pytest.raises(CoreError) as caught:
        store.load(one['snapshot_id'])
    assert caught.value.error_type == 'not_found'
    assert store.load(two['snapshot_id']) == two and existing.exists()


def test_delete_export_pair_does_not_delete_source_snapshot(tmp_path):
    identifier = 'a' * 32
    root = tmp_path / 'exports'
    root.mkdir()
    for suffix in ('.csv', '.metadata.json'):
        (root / (identifier + suffix)).write_text('data')
    snapshot = tmp_path / 'snapshots' / (identifier + '.json')
    snapshot.parent.mkdir()
    snapshot.write_text('source')
    result = delete(tmp_path, 'export', identifier)
    assert result['deleted_files'] == 2
    assert not list(root.iterdir()) and snapshot.exists()


@pytest.mark.parametrize('identifier', ['../outside', '/absolute', 'A' * 32, '0' * 31, None])
def test_delete_rejects_bad_ids_before_io(tmp_path, identifier):
    with pytest.raises(CoreError) as caught:
        delete(tmp_path, 'snapshot', identifier)
    assert caught.value.error_type == 'invalid_argument'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('confirmed', [False, None, 1, 'true'])
def test_delete_requires_explicit_boolean_confirmation(tmp_path, confirmed):
    with pytest.raises(CoreError) as caught:
        delete(tmp_path, 'snapshot', 'a' * 32, confirmed)
    assert caught.value.detail == 'confirmation_required'
    assert not list(tmp_path.iterdir())


def test_delete_missing_id_returns_not_found(tmp_path):
    for state in ('absent_directory', 'absent_id'):
        if state == 'absent_id':
            (tmp_path / 'snapshots').mkdir()
        with pytest.raises(CoreError) as caught:
            delete(tmp_path, 'snapshot', 'a' * 32)
        assert caught.value.error_type == 'not_found'


@pytest.mark.parametrize('kind', ['snapshot', 'export'])
def test_delete_rejects_symlink_without_deleting_external_target(tmp_path, kind):
    parent = tmp_path / ('snapshots' if kind == 'snapshot' else 'exports')
    parent.mkdir()
    identifier = 'a' * 32
    outside = tmp_path / 'outside.txt'
    outside.write_text('must stay')
    target = parent / (identifier + '.json')
    target.symlink_to(outside)
    with pytest.raises(CoreError) as caught:
        delete(tmp_path, kind, identifier)
    assert caught.value.error_type == 'storage'
    assert outside.read_text() == 'must stay' and target.is_symlink()


def test_delete_rejects_symlink_directory(tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'snapshots').symlink_to(external, target_is_directory=True)
    with pytest.raises(CoreError):
        delete(root, 'snapshot', 'a' * 32)
    assert not list(external.iterdir())
