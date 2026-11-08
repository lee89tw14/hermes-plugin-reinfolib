import json
from datetime import date
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY, QUERY, RAW


def test_xpt001_nearest_station_not_property_coordinates():
    query={**QUERY,'from':'20241','to':'20244','priceClassification':'02','landTypeCode':'01,07'}
    result=layers.fetch('XPT001',query,key=KEY,transport=lambda *a:(200,{},json.dumps(RAW).encode()))
    assert result['geometry_semantics']=='nearest_station'
    assert result['property_level_spatial_inference_allowed'] is False
    assert 'prohibited_property_hazard_school_zone_radius_inference' in result['warnings']
    assert layers.validate('XPT001',{**query,'from':'20053','priceClassification':'01'})
    for patch in ({'from':'20052'},{'from':'20211','to':'20204'},{'from':'20241','to':'20245'}, {'from':'20053'}, {'priceClassification':'0'},{'landTypeCode':'00'},{'from':20241}):
        with pytest.raises(CoreError):
            layers.validate('XPT001',{**query,**patch})
    with pytest.raises(CoreError):
        layers.validate('XPT001',{**query,'to':'20281'},today=date(2027,1,1))
