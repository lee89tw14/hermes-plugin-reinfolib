import json
from datetime import date
import pytest
from reinfolib import land_values
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY


def test_xct001_rolling_five_years_and_raw_envelope():
    query = {'year':2027,'area':'09','division':'00'}
    raw = {'status':'OK','data':[{'価格時点':'2027','future':None}]}
    seen=[]
    def transport(*args):
        seen.append(args)
        return 200,{},json.dumps(raw).encode()
    result = land_values.fetch(query,key=KEY,transport=transport,today=date(2027,8,1))
    assert result['raw'] == raw
    assert result['record_count'] == 1
    assert 'XCT001?year=2027&area=09&division=00' in seen[0][0]
    assert land_values.validate({'year':2023,'area':'09','division':'20'},today=date(2027,1,1))
    for patch in ({'year':2022},{'year':2028},{'year':True},{'area':'9'},{'area':'48'},{'division':'01'},{'z':14}):
        with pytest.raises(CoreError):
            land_values.fetch({**query,**patch},key=KEY,transport=transport,today=date(2027,8,1))
    assert len(seen)==1


def test_xct001_404_distinct_from_empty_success():
    query={'year':2026,'area':'13','division':'00'}
    with pytest.raises(CoreError) as caught:
        land_values.fetch(query,key=KEY,transport=lambda *a:(404,{},b''))
    assert caught.value.error_type=='not_found'
    result=land_values.fetch(query,key=KEY,transport=lambda *a:(200,{},b'{"status":"OK","data":[]}'))
    assert result['status']=='no_records'
