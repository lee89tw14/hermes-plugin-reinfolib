"""P2-1: fixed safe diagnostics for local view/adapter failures (offline)."""
import json

import pytest

from reinfolib import adapter, views


SNAPSHOT_ID = 'a' * 32


def snapshot(rows=()):
    return {'schema_version': 1, 'snapshot_id': SNAPSHOT_ID,
            'query': {'city': '13104', 'year': 2025,
                      'price_classification': '01', 'language': 'ja'},
            'fetched_at': '2026-11-08T00:00:00Z',
            'upstream_count': len(rows), 'records': list(rows)}


def local_dispatch(monkeypatch, tmp_path, data):
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(adapter, 'load_snapshot', lambda _id: data)


def assert_failure(payload, kind, detail):
    assert payload['ok'] is False
    assert payload['error_type'] == kind
    assert payload['detail'] == detail


def test_export_size_cap_is_response_too_large(tmp_path, monkeypatch):
    # Exercise the real serializer/cap, not an exception injected into export.
    local_dispatch(monkeypatch, tmp_path, snapshot([{'FutureField': 'x' * 32_000_000}]))
    payload = json.loads(adapter.dispatch('reinfolib_export', {'snapshot_id': SNAPSHOT_ID}))
    assert_failure(payload, 'response_too_large', 'export_too_large')
    assert list((tmp_path / 'exports').iterdir()) == []


def test_symlink_export_root_is_storage_failure(tmp_path, monkeypatch):
    local_dispatch(monkeypatch, tmp_path, snapshot())
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'exports').symlink_to(real, target_is_directory=True)
    payload = json.loads(adapter.dispatch('reinfolib_export', {'snapshot_id': SNAPSHOT_ID}))
    assert_failure(payload, 'storage', 'symlink_export_root')
    assert list(real.iterdir()) == []
    with pytest.raises(ValueError) as caught:
        views.export(snapshot(), tmp_path / 'exports')
    assert caught.value.error_type == 'storage'


@pytest.mark.parametrize('name,args,detail', [
    ('unknown', {}, 'invalid_arguments'),
    ('reinfolib_records', [], 'invalid_arguments'),
    ('reinfolib_records', {}, 'invalid_arguments'),
    ('reinfolib_records', {'snapshot_id': '../private'}, 'invalid_snapshot_id'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'limit': True}, 'invalid_paging'),
    ('reinfolib_export', {'snapshot_id': SNAPSHOT_ID, 'format': 'bad'}, 'invalid_export_format'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': []}, 'invalid_filters'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': {'private': 'x'}}, 'unknown_filter'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': {'type': ''}}, 'invalid_filter_text'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': {'building_year_min': True}}, 'invalid_year_filter'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': {'area_min': 'private'}}, 'invalid_numeric_filter'),
    ('reinfolib_records', {'snapshot_id': SNAPSHOT_ID, 'filters': {'area_min': '5', 'area_max': '1'}}, 'inverted_range'),
])
def test_parameter_errors_have_fixed_details_before_io(monkeypatch, name, args, detail):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid arguments reached IO')
    monkeypatch.setattr(adapter, 'load_snapshot', forbidden)
    monkeypatch.setattr(adapter, 'fetch_snapshot', forbidden)
    payload = json.loads(adapter.dispatch(name, args))
    assert_failure(payload, 'invalid_argument', detail)
    assert 'private' not in json.dumps(payload)


def test_oserror_has_fixed_storage_detail(tmp_path, monkeypatch):
    local_dispatch(monkeypatch, tmp_path, snapshot())
    (tmp_path / 'exports').write_text('blocks-directory')
    payload = json.loads(adapter.dispatch('reinfolib_export', {'snapshot_id': SNAPSHOT_ID}))
    assert_failure(payload, 'storage', 'storage_unavailable')
    assert str(tmp_path) not in json.dumps(payload)


def test_unexpected_valueerror_is_not_reported_as_user_error(tmp_path, monkeypatch):
    local_dispatch(monkeypatch, tmp_path, snapshot())
    def broken(*args, **kwargs):
        raise ValueError('private internal exception text')
    monkeypatch.setattr(adapter, 'export', broken)
    payload = json.loads(adapter.dispatch('reinfolib_export', {'snapshot_id': SNAPSHOT_ID}))
    assert_failure(payload, 'internal', 'local_view_failed')
    assert 'private' not in json.dumps(payload)


def test_filter_validation_does_not_depend_on_assertions():
    import ast
    import inspect
    # Assertions must not be used as production validation, even for invariants.
    tree = ast.parse(inspect.getsource(views))
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))


@pytest.mark.parametrize('tool', ['reinfolib_records', 'reinfolib_export'])
@pytest.mark.parametrize('state,kind,detail', [
    ('missing', 'not_found', 'snapshot_not_found'),
    ('corrupt', 'invalid_response', 'invalid_snapshot'),
    ('symlink_root', 'storage', 'storage_unavailable'),
])
def test_load_snapshot_preserves_safe_core_errors(tmp_path, monkeypatch, tool, state, kind, detail):
    # Characterize the existing CoreError -> AdapterFailure sibling path.
    monkeypatch.setattr(adapter, 'data_root', lambda: tmp_path)
    root = tmp_path / 'snapshots'
    if state == 'symlink_root':
        real = tmp_path / 'real'
        real.mkdir()
        root.symlink_to(real, target_is_directory=True)
    elif state == 'corrupt':
        root.mkdir()
        (root / (SNAPSHOT_ID + '.json')).write_text('private corrupt contents')
    payload = json.loads(adapter.dispatch(tool, {'snapshot_id': SNAPSHOT_ID}))
    assert_failure(payload, kind, detail)
    assert payload['status_code'] is None
    assert 'private' not in json.dumps(payload)
    assert str(tmp_path) not in json.dumps(payload)


def test_direct_views_validate_in_optimized_python():
    import subprocess
    import sys
    from pathlib import Path
    script = '''
import json
from reinfolib import views
cases = [
    (lambda: views.page({}, limit=True), 'invalid_paging'),
    (lambda: views.validate_filters({'area_min': 'NaN'}), 'invalid_numeric_filter'),
    (lambda: views.validate_filters({'area_min': '5', 'area_max': '1'}), 'inverted_range'),
    (lambda: views.export({}, '.', format='bad'), 'invalid_export_format'),
]
for operation, expected in cases:
    try:
        operation()
    except views.ViewError as error:
        if not isinstance(error, ValueError) or error.error_type != 'invalid_argument' or error.detail != expected:
            raise RuntimeError('unexpected diagnostic')
    else:
        raise RuntimeError('validation omitted')
print(json.dumps({'validated': len(cases)}))
'''
    result = subprocess.run([sys.executable, '-O', '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'validated': 4}
