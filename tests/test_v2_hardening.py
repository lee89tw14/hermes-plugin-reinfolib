"""Regression tests found during integration review."""
import json
import pytest
from reinfolib.storage import SnapshotStore, CoreError
from reinfolib import adapter

QUERY = {'city': '13104', 'year': 2025, 'quarter': 1, 'price_classification': '01', 'language': 'ja'}
ROWS = [{'Period': '2025年第1四半期', 'TradePrice': '100'}]


def test_snapshot_directory_rejects_symlink_ancestor(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(CoreError):
        SnapshotStore(link / 'snapshots')
    assert not list(real.iterdir())


@pytest.mark.parametrize('warnings', [None, 'text', [1], {'field': 'value'}])
def test_corrupt_snapshot_warning_shape_is_safe(tmp_path, warnings):
    store = SnapshotStore(tmp_path)
    s = store.save(QUERY, ROWS)
    s['warnings'] = warnings
    (tmp_path / (s['snapshot_id'] + '.json')).write_text(json.dumps(s))
    with pytest.raises(CoreError) as caught:
        store.load(s['snapshot_id'])
    assert caught.value.error_type == 'invalid_response'


def test_full_adapter_query_paging_export_and_error_mapping(tmp_path, monkeypatch):
    import client
    import service
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(service, '_secret', lambda name: 'integration-test-secret-1234567890')
    calls = []
    def transport(url, headers, timeout, cap):
        calls.append(url)
        return 200, {}, json.dumps({'status': 'OK', 'data': ROWS * 3}).encode()
    monkeypatch.setattr(client, 'default_transport', transport)
    monkeypatch.setattr(client, 'DEFAULT_GAP', client.Gap(now=lambda: 0, sleep=lambda _: None))
    result = json.loads(adapter.dispatch('reinfolib_transactions', {**QUERY, 'limit': 2}))
    assert result['ok'] is True and result['upstream_count'] == 3 and result['returned_count'] == 2
    identifier = result['snapshot_id']
    second = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': identifier, 'offset': 2, 'limit': 2}))
    assert second['returned_count'] == 1 and second['has_more'] is False
    exported = json.loads(adapter.dispatch('reinfolib_export', {'snapshot_id': identifier, 'format': 'json'}))
    assert exported['ok'] is True and exported['count'] == 3 and len(calls) == 1
    bad = json.loads(adapter.dispatch('reinfolib_transactions', {'city': 'not-a-code', 'year': 2025}))
    assert bad['error_type'] == 'invalid_argument' and len(calls) == 1
    missing = json.loads(adapter.dispatch('reinfolib_records', {'snapshot_id': 'f' * 32}))
    assert missing['error_type'] == 'not_found'
