"""P2-2 acceptance: tools observe deletion as not_found."""
import json
from reinfolib import adapter
from reinfolib.maintenance import delete_artifact
from reinfolib.storage import SnapshotStore


def test_records_and_export_return_not_found_after_snapshot_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    q = {'year': 2025, 'city': '13104', 'language': 'ja', 'price_classification': '01'}
    snapshot = SnapshotStore(tmp_path / 'snapshots').save(q, [{'Area': '30'}])
    identifier = snapshot['snapshot_id']
    assert json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': identifier}))['ok'] is True
    delete_artifact(tmp_path, kind='snapshot', artifact_id=identifier, confirmed=True)
    for name in ('reinfolib_records', 'reinfolib_export'):
        result = json.loads(adapter.dispatch(name, {'snapshot_id': identifier}))
        assert (result['ok'], result['error_type'], result['detail']) == (False, 'not_found', 'snapshot_not_found')
    assert not list((tmp_path / 'exports').glob('*')) if (tmp_path / 'exports').exists() else True
