import json
from reinfolib import adapter, layers
from tests.test_m4_layers import QUERY, RAW, KEY


def test_m4_registered_separate_tools_and_actual_dispatch(monkeypatch):
    import service
    import client
    monkeypatch.setattr(service,'_secret',lambda name:KEY)
    monkeypatch.setattr(client,'default_transport',lambda *a:(200,{},json.dumps(RAW).encode()))
    for tool,endpoint,query in [('reinfolib_layer','XKT002',QUERY),('reinfolib_land_values','XPT002',{**QUERY,'year':2026})]:
        assert tool in adapter.DEFINITIONS
        result=json.loads(adapter.dispatch(tool,{'endpoint':endpoint,'query':query}))
        assert result['ok'] and result['raw']==RAW
        assert result['source_metadata']['source_vintage']
        assert result['source_metadata']['license_review']=='regional_conditions_not_cleared_for_commercial_export'
        assert result['credit'] and result['attribution']
        assert json.loads(adapter.dispatch(tool,{'endpoint':endpoint,'query':query,'purpose':'commercial'}))['error_type']=='license_review_required'
    assert json.loads(adapter.dispatch('reinfolib_land_values',{'endpoint':'XKT002','query':QUERY}))['error_type']=='invalid_argument'
    assert json.loads(adapter.dispatch('reinfolib_layer',{'endpoint':'XCT001','query':{}}))['error_type']=='invalid_argument'
    catalog=adapter.catalog()
    item=next(e for e in catalog['endpoints'] if e['id']=='XKT002')
    assert item['implemented']
    assert item['tile_contract']['zoom']==[11,15]
    assert item['source_metadata']['source_vintage']
