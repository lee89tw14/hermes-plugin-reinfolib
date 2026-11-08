from datetime import date
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY, QUERY, RAW
import json


def test_xpt002_separate_year_classification_schema():
    args = {**QUERY, 'year': 2027, 'priceClassification': '0', 'useCategoryCode': '00,05'}
    assert layers.validate('XPT002', args, today=date(2027,8,1)) == args
    assert layers.fetch('XPT002', args, today=date(2027,8,1), key=KEY, transport=lambda *a: (200,{},json.dumps(RAW).encode()))['raw'] == RAW
    for patch in ({'year':1994}, {'year':2028}, {'year':True}, {'priceClassification':'01'}, {'useCategoryCode':'01'}, {'z':12}, {'useCategoryCode':'00, 05'}):
        with pytest.raises(CoreError):
            layers.validate('XPT002', {**args, **patch}, today=date(2027,8,1))
    with pytest.raises(CoreError):
        layers.validate('XPT002', QUERY)
