import json
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY, QUERY


def test_pbf_xpt001_retains_inference_prohibition():
    result=layers.fetch('XPT001',{**QUERY,'response_format':'pbf','from':'20241','to':'20244'},key=KEY,transport=lambda *a:(200,{},b'\x1a\x01'))
    assert result['geometry_semantics']=='nearest_station'
    assert result['property_level_spatial_inference_allowed'] is False


@pytest.mark.parametrize('body',[
    b'{"type":"FeatureCollection","features":[],"bad":1e309}',
    b'{"type":"FeatureCollection","features":[],"features":[null]}',
    json.dumps({'type':'FeatureCollection','features':[], 'nested':[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}).encode(),
])
def test_json_bounded_and_unambiguous(body):
    with pytest.raises(CoreError):
        layers.fetch('XKT002',QUERY,key=KEY,transport=lambda *a:(200,{},body))


def test_internal_getter_is_not_arbitrary_endpoint():
    with pytest.raises(CoreError):
        layers.get_payload('../XIT001',{},key=KEY,transport=lambda *a:(200,{},b''))
