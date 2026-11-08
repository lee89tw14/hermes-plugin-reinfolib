import base64
import gzip
import json
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY, QUERY, RAW


@pytest.mark.parametrize('body', [b'', b' ', b'{"type":"FeatureCollection","features":[]}'])
def test_empty_200_is_not_safety(body):
    result = layers.fetch('XKT002', QUERY, key=KEY, transport=lambda *a: (200, {}, body))
    assert result['status'] == 'no_features'
    assert result['feature_count'] == 0
    assert 'safe' not in result


@pytest.mark.parametrize('status,kind', [(404,'not_found'), (401,'auth'), (403,'auth'), (429,'rate_limited'), (500,'upstream'), (302,'redirect')])
def test_http_errors(status,kind):
    with pytest.raises(CoreError) as caught:
        layers.fetch('XKT002', QUERY, key=KEY, transport=lambda *a: (status, {}, b'private error'))
    assert caught.value.error_type == kind
    assert caught.value.status_code == status
    assert 'private' not in str(caught.value)


def test_pbf_is_explicit_opaque_bytes():
    blob = b'\x1a\x00\xff'
    result = layers.fetch('XKT002', {**QUERY, 'response_format': 'pbf'}, key=KEY, transport=lambda *a: (200, {}, blob))
    assert result['format'] == 'pbf'
    assert result['encoding'] == 'base64'
    assert base64.b64decode(result['raw_base64']) == blob
    assert result['decoded'] is False
    assert 'feature_count' not in result and 'raw' not in result


@pytest.mark.parametrize('headers,body,detail', [
    ({'content-encoding':'gzip'}, b'invalid', 'bad_gzip'),
    ({'content-encoding':'gzip'}, gzip.compress(b'x'*8_000_001), 'too_big'),
    ({}, b'x'*8_000_001, 'too_big'),
    ({'content-length':'1000'}, b'{}', 'short_body'),
    ({}, json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','properties':{'key':KEY}}]}).encode(), 'secret'),
    ({}, b'{"type":"FeatureCollection","features":[null]}', 'bad_feature'),
    ({}, b'{"type":"FeatureCollection","features":[],"bad":NaN}', 'not_geojson'),
])
def test_bounded_safe_payload(headers,body,detail):
    with pytest.raises(CoreError) as caught:
        layers.fetch('XKT002', QUERY, key=KEY, transport=lambda *a: (200, headers, body))
    assert caught.value.detail == detail


def test_gzip_preserves_multigeometry_unknown_properties():
    result = layers.fetch('XKT002', QUERY, key=KEY, transport=lambda *a: (200, {'content-encoding':'gzip'}, gzip.compress(json.dumps(RAW).encode())))
    assert result['raw'] == RAW
