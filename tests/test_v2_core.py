"""Offline transaction-core contract tests; injected transports use synthetic keys."""
import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

TODAY = date(2026, 2, 15)
KEY = "synthetic-test-key-123456789"


class NoWait:
    def run(self, call):
        return call()


@pytest.mark.parametrize('args', [
    {}, None, [], {'year': 2015}, {'year': 2015, 'area': '13', 'city': '13102'},
    {'year': 2015, 'area': '13', 'url': 'anything'},
    *[{'year': v, 'area': '13'} for v in [True, 2015.0, '2015', 2004, 2027]],
    *[{'year': 2015, 'quarter': v, 'area': '13'} for v in [True, 2.0, '2', 0, 5, None]],
    *[{'year': 2015, 'area': v} for v in ['00', '48', '１３', '13 ', 13, None, []]],
    *[{'year': 2015, 'city': v} for v in ['00101', '48101', '１３１０２', '1310', 13102]],
    *[{'year': 2015, 'station': v} for v in ['00001', '１２３４５６', 123456]],
    {'year': 2005, 'quarter': 1, 'area': '13'},
    {'year': 2005, 'quarter': 2, 'area': '13'},
    {'year': 2026, 'quarter': 2, 'area': '13'},
    {'year': 2020, 'price_classification': '02', 'area': '13'},
    *[{'year': 2015, 'language': v, 'area': '13'} for v in ['JA', 'ja ', None, []]],
    *[{'year': 2015, 'price_classification': v, 'area': '13'} for v in [1, None, [], '03']],
])
def test_strict_query_rejects_without_coercion(args):
    from reinfolib.core import CoreError, validate_query
    with pytest.raises(CoreError) as caught:
        validate_query(args, today=TODAY)
    assert caught.value.error_type == 'invalid_argument'


@pytest.mark.parametrize('args', [
    {'year': 2005, 'area': '01'},
    {'year': 2015, 'station': '000001', 'language': 'en', 'price_classification': 'both'},
    {'year': 2021, 'quarter': 1, 'city': '01100', 'price_classification': '02'},
    {'year': 2026, 'quarter': 1, 'area': '47'},
])
def test_query_canonical_defaults(args):
    from reinfolib.core import validate_query
    original = args.copy()
    assert validate_query(args, today=TODAY) == {'price_classification': '01', 'language': 'ja', **args}
    assert args == original


def test_city_quarter_raw_snapshot(tmp_path):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore

    row = {"Period": "2015年第2四半期", "MunicipalityCode": "13103",
           "PriceCategory": "取引価格情報", "UnknownFutureColumn": "original", "Null": None}
    calls = []
    def transport(url, headers, timeout, cap):
        calls.append((url, headers, timeout, cap))
        return 200, {}, json.dumps({"status": "OK", "data": [row, row]}).encode()
    store = SnapshotStore(tmp_path / "snapshots")
    result = fetch_transactions({"city": "13102", "year": 2015, "quarter": 2},
                                key=KEY, storage=store, transport=transport, gap=NoWait(), today=TODAY)
    assert result["records"] == [row, row]
    assert result["upstream_count"] == 2
    assert result["schema_version"] == 1
    assert store.load(result["snapshot_id"]) == result
    url, headers, timeout, cap = calls[0]
    assert urlsplit(url)._replace(query="").geturl() == "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001"
    assert parse_qs(urlsplit(url).query) == {"city": ["13102"], "year": ["2015"], "quarter": ["2"], "priceClassification": ["01"], "language": ["ja"]}
    assert KEY not in url
    assert headers["Ocp-Apim-Subscription-Key"] == KEY
    assert (timeout, cap) == (30, 8_000_000)


@pytest.mark.parametrize('classification', ['01', '02', 'both'])
def test_annual_english_classification_request(tmp_path, classification):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    seen = []
    rows = [{'Period': '1st quarter 2021', 'PriceCategory': 'localized unknown', 'Area': '80以上'},
            {'Period': '4th quarter 2021', 'PriceCategory': None}]
    def transport(url, *unused):
        seen.append(parse_qs(urlsplit(url).query))
        return 200, {}, json.dumps({'status': 'OK', 'data': rows}).encode()
    result = fetch_transactions({'year': 2021, 'station': '000001', 'language': 'en',
                                 'price_classification': classification}, key=KEY,
                                storage=SnapshotStore(tmp_path), transport=transport, gap=NoWait())
    assert result['records'] == rows
    assert 'quarter' not in seen[0]
    assert seen[0]['station'] == ['000001']
    if classification == 'both':
        assert 'priceClassification' not in seen[0]
    else:
        assert seen[0]['priceClassification'] == [classification]


@pytest.mark.parametrize('status,scope,kind', [(400,'station','fallback_required'), (400,'city','upstream'),
    (404,'city','not_found'), (401,'city','auth'), (403,'city','auth'),
    (429,'city','rate_limited'), (500,'city','upstream'), (204,'city','upstream')])
def test_http_errors_no_retry_no_snapshot(tmp_path, status, scope, kind):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    calls = []
    def transport(*args):
        calls.append(args)
        return status, {}, b'not json'
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, scope: '000001' if scope == 'station' else '13102'},
                           key=KEY, storage=SnapshotStore(tmp_path), transport=transport, gap=NoWait())
    assert (caught.value.error_type, caught.value.status_code) == (kind, status)
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('payload', [b'not json', b'[]', b'null', b'{"status":"ERROR","data":[{}]}',
    b'{"status":"OK","statusCode":200,"data":[{}]}', b'{"status":"OK","data":[]}',
    b'{"status":"OK","data":{}}', b'{"status":"OK","data":[null]}',
    b'{"status":"OK","data":[1]}', b'{"status":"OK","data":[{"x":NaN}]}'])
def test_malformed_envelope_is_not_saved(tmp_path, payload):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           transport=lambda *a: (200, {}, payload), gap=NoWait())
    assert caught.value.error_type == 'invalid_response'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('period', ['2014年第2四半期', '2015年第3四半期', '2nd quarter 2014', '3rd quarter 2015'])
def test_recognized_period_mismatch_rejected(tmp_path, period):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'quarter': 2, 'area': '13'}, key=KEY,
                           storage=SnapshotStore(tmp_path), gap=NoWait(), transport=lambda *a:
                           (200, {}, json.dumps({'status': 'OK', 'data': [{'Period': period}]}).encode()))
    assert caught.value.detail == 'mismatched_period'


def test_unrecognized_period_preserved_with_warning(tmp_path):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    store = SnapshotStore(tmp_path)
    row = {'Period': 'Unconfirmed localized format'}
    result = fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=store,
                               gap=NoWait(), transport=lambda *a:
                               (200, {}, json.dumps({'status': 'OK', 'data': [row]}).encode()))
    assert result['records'] == [row]
    assert 'unrecognized_period' in result['warnings']
    assert store.load(result['snapshot_id']) == result


@pytest.mark.parametrize('where,encoding', [('row','plain'), ('row','percent'), ('envelope','unicode'), ('field','plain')])
def test_secret_echo_anywhere_is_not_persisted(tmp_path, where, encoding):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    secret = KEY if encoding == 'plain' else (''.join('%%%02X' % ord(c) for c in KEY)
                                            if encoding == 'percent' else ''.join('\\u%04x' % ord(c) for c in KEY))
    body = {'status': 'OK', 'data': [{'Period': '2015年第2四半期'}]}
    if where == 'envelope':
        body['ignored'] = secret
    elif where == 'field':
        body['data'][0][secret] = None
    else:
        body['data'][0]['unknown'] = {'nested': [secret]}
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           gap=NoWait(), transport=lambda *a: (200, {}, json.dumps(body).encode()))
    assert caught.value.detail == 'secret'
    assert KEY not in str(caught.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('key', ['', ' ', 'with space', 'newline\n', None])
def test_bad_key_fixed_error(tmp_path, key):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=key, storage=SnapshotStore(tmp_path),
                           transport=lambda *a: pytest.fail('must not send'))
    assert (caught.value.error_type, caught.value.detail) == ('config', 'missing_key')


@pytest.mark.parametrize('error,kind', [(TimeoutError(KEY), 'timeout'), (RuntimeError(KEY), 'upstream')])
def test_network_exception_is_safe(tmp_path, error, kind):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    def transport(*a):
        raise error
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           gap=NoWait(), transport=transport)
    assert caught.value.error_type == kind
    import traceback
    assert KEY not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('status,headers,payload,kind,detail', [
    (302, {'Location': 'https://other.example'}, b'', 'redirect', 'redirect'),
    (200, {'Content-Length': '8000001'}, b'{}', 'invalid_response', 'too_big'),
    (200, {'Content-Length': '20'}, b'{}', 'invalid_response', 'short_body'),
    (200, {'Content-Encoding': 'br'}, b'{}', 'invalid_response', 'bad_encoding'),
])
def test_inherited_transport_guards(tmp_path, status, headers, payload, kind, detail):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    calls = []
    def transport(*args):
        calls.append(args)
        return status, headers, payload
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           transport=transport, gap=NoWait())
    assert (caught.value.error_type, caught.value.detail) == (kind, detail)
    assert len(calls) == 1


def test_gzip_full_29_columns_preserved(tmp_path):
    import gzip
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    names = ('Type Region MunicipalityCode Prefecture Municipality DistrictName TradePrice PricePerUnit '
             'FloorPlan Area UnitPrice LandShape Frontage TotalFloorArea BuildingYear Structure Use Purpose '
             'Direction Classification Breadth CityPlanning CoverageRatio FloorAreaRatio Period Renovation '
             'Remarks PriceCategory DistrictCode').split()
    assert len(names) == 29
    row = {name: None if i % 2 else 'raw' for i, name in enumerate(names)}
    row['Period'] = '2nd quarter 2015'
    row['FutureField'] = 'long raw text ' * 100
    body = gzip.compress(json.dumps({'status': 'OK', 'data': [row]*25}).encode())
    result = fetch_transactions({'year': 2015, 'quarter': 2, 'area': '01', 'language': 'en'}, key=KEY,
        storage=SnapshotStore(tmp_path), transport=lambda *a: (200, {'Content-Encoding':'gzip'}, body), gap=NoWait())
    assert result['records'] == [row]*25
    assert result['upstream_count'] == 25


@pytest.mark.parametrize('payload', [
    b'{"status":"OK","data":[{"Period":"unknown","x":1e9999}]}',
    b'{"status":"OK","data":[{}],"duplicate":1,"duplicate":2}',
])
def test_non_lossy_payload_rejects_ambiguous_json(tmp_path, payload):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           transport=lambda *a: (200, {}, payload), gap=NoWait())
    assert caught.value.error_type == 'invalid_response'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('suffix', ['', ',"data":[{}]'])
def test_raw_secret_scan_precedes_parsing_and_duplicate_discard(tmp_path, suffix):
    from reinfolib.core import CoreError, fetch_transactions
    from reinfolib.storage import SnapshotStore
    payload = ('{"status":"OK","data":[{"unknown":"' + KEY + '"}]' + suffix).encode()
    with pytest.raises(CoreError) as caught:
        fetch_transactions({'year': 2015, 'area': '13'}, key=KEY, storage=SnapshotStore(tmp_path),
                           transport=lambda *a: (200, {}, payload), gap=NoWait())
    assert caught.value.detail == 'secret'


@pytest.mark.parametrize('period', ['1st quarter 2015', '2nd quarter 2015', '3rd quarter 2015', '4th quarter 2015'])
def test_known_english_periods_annual(tmp_path, period):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    result = fetch_transactions({'year': 2015, 'area': '13', 'language': 'en'}, key=KEY,
        storage=SnapshotStore(tmp_path), gap=NoWait(), transport=lambda *a:
        (200, {}, json.dumps({'status': 'OK', 'data': [{'Period': period}]}).encode()))
    assert result['warnings'] == []


def test_injected_clock_applies_to_snapshot_path(tmp_path):
    from reinfolib.core import fetch_transactions
    from reinfolib.storage import SnapshotStore
    store = SnapshotStore(tmp_path)
    result = fetch_transactions({'year': 2030, 'quarter': 1, 'city': '13102'}, key=KEY,
        storage=store, today=date(2030, 2, 1), gap=NoWait(), transport=lambda *a:
        (200, {}, json.dumps({'status':'OK', 'data':[{'Period':'2030年第1四半期'}]}).encode()))
    assert store.load(result['snapshot_id']) == result


def test_nested_plugin_package_loading_without_hermes(tmp_path):
    import importlib
    import sys
    import types
    from pathlib import Path
    name = 'v2_core_isolated_plugin'
    parent = types.ModuleType(name)
    parent.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[name] = parent
    try:
        module = importlib.import_module(name + '.reinfolib.core')
        storage = importlib.import_module(name + '.reinfolib.storage')
        assert module.CoreError is storage.CoreError
        store = storage.SnapshotStore(tmp_path)
        result = module.fetch_transactions({'year': 2005, 'area': '01', 'price_classification':'both'},
            key=KEY, storage=store, gap=NoWait(), transport=lambda *a:
            (200, {}, json.dumps({'status':'OK', 'data':[{'Period':'2005年第3四半期'}]}).encode()))
        assert result['warnings'] == ['classification_02_available_from_2021']
        assert store.load(result['snapshot_id']) == result
    finally:
        for loaded in list(sys.modules):
            if loaded == name or loaded.startswith(name+'.'):
                del sys.modules[loaded]








