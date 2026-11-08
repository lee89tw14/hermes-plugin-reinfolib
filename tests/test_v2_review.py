"""Independent-review regressions. Injected data, no real key or network."""
import json
import pytest
import client
import service
from reinfolib import adapter
from reinfolib.core import CoreError, fetch_transactions
from reinfolib.storage import SnapshotStore

QUERY = {'city': '13104', 'year': 2025, 'quarter': 1}
KEY = 'review-synthetic-key-1234567890'


def test_deep_json_rejected_before_persistence(tmp_path, monkeypatch):
    body = b'{"status":"OK","data":[{"unknown":' + b'[' * 500 + b'0' + b']' * 500 + b'}]}'
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(service, '_secret', lambda _: KEY)
    monkeypatch.setattr(client, 'default_transport', lambda *args: (200, {}, body))
    monkeypatch.setattr(client, 'DEFAULT_GAP', client.Gap(now=lambda: 0, sleep=lambda _: None))
    result = json.loads(adapter.dispatch('reinfolib_transactions', QUERY))
    assert result['ok'] is False and result['error_type'] == 'invalid_response'
    assert not list(tmp_path.rglob('*.json'))


@pytest.mark.parametrize('token', ['9007199254740993.0', '0.1', '1e100'])
def test_noninteger_json_numbers_rejected_instead_of_rounded(tmp_path, token):
    body = ('{"status":"OK","data":[{"UnknownFutureNumber":' + token + '}]}').encode()
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError) as caught:
        fetch_transactions(QUERY, key=KEY, storage=store,
                           transport=lambda *args: (200, {}, body),
                           gap=client.Gap(now=lambda: 0, sleep=lambda _: None))
    assert caught.value.error_type == 'invalid_response'
    assert not list(tmp_path.glob('*.json'))


def test_snapshot_rejects_fractional_numbers_at_storage_boundary(tmp_path):
    store = SnapshotStore(tmp_path)
    with pytest.raises(CoreError):
        store.save({**QUERY, 'price_classification': '01', 'language': 'ja'}, [{'value': 0.1}])
    assert not list(tmp_path.glob('*.json'))


def test_oversized_content_length_is_upstream_error_not_user_error(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(service, '_secret', lambda _: KEY)
    monkeypatch.setattr(client, 'default_transport', lambda *args: (200, {'Content-Length': '9' * 5000}, b'{}'))
    monkeypatch.setattr(client, 'DEFAULT_GAP', client.Gap(now=lambda: 0, sleep=lambda _: None))
    result = json.loads(adapter.dispatch('reinfolib_transactions', QUERY))
    assert result['error_type'] == 'invalid_response'
    assert result['detail'] == 'bad_headers'
    assert not list(tmp_path.rglob('*.json'))
