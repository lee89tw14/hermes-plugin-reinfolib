import json
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError
from tests.test_m4_layers import KEY, QUERY, RAW


def check(endpoint, zoom, filters):
    query = {**QUERY, 'z':zoom[0], 'x':0, 'y':0, **filters}
    seen=[]
    def transport(url,*rest):
        seen.append(url)
        return 200,{},json.dumps(RAW).encode()
    result = layers.fetch(endpoint,query,key=KEY,transport=transport)
    assert result['raw']==RAW
    assert endpoint+'?' in seen[0]
    assert layers.fetch(endpoint,query,key=KEY,transport=lambda *a:(200,{},b''))['status']=='no_features'
    for patch in ({'z':zoom[0]-1},{'z':zoom[1]+1},{'x':2**zoom[0]},{'y':-1},{'unknown_filter':'1'}):
        with pytest.raises(CoreError):
            layers.validate(endpoint,{**query,**patch})
    for name in filters:
        with pytest.raises(CoreError):
            layers.validate(endpoint,{**query,name:'malformed'})
    assert len(seen)==1

def test_xkt001_independent_contract():
    check('XKT001', [11, 15], {})


def test_xkt003_independent_contract():
    check('XKT003', [11, 15], {})


def test_xkt004_independent_contract():
    check('XKT004', [11, 15], {'administrativeAreaCode': '01101,13102'})


def test_xkt005_independent_contract():
    check('XKT005', [11, 15], {'administrativeAreaCode': '01101,13102'})


def test_xkt006_independent_contract():
    check('XKT006', [13, 15], {})


def test_xkt007_independent_contract():
    check('XKT007', [13, 15], {})


def test_xkt010_independent_contract():
    check('XKT010', [13, 15], {})


def test_xkt011_independent_contract():
    check('XKT011', [13, 15], {'administrativeAreaCode': '01101,13102', 'welfareFacilityClassCode': '01,02', 'welfareFacilityMiddleClassCode': '0101,0201', 'welfareFacilityMinorClassCode': '020101,020102'})


def test_xkt013_independent_contract():
    check('XKT013', [11, 15], {})


def test_xkt014_independent_contract():
    check('XKT014', [11, 15], {})


def test_xkt015_independent_contract():
    check('XKT015', [11, 15], {})


def test_xkt016_independent_contract():
    check('XKT016', [11, 15], {'administrativeAreaCode': '01101,13102'})


def test_xkt017_independent_contract():
    check('XKT017', [13, 15], {'administrativeAreaCode': '01101,13102'})


def test_xkt018_independent_contract():
    check('XKT018', [13, 15], {})


def test_xkt019_independent_contract():
    check('XKT019', [9, 15], {'prefectureCode': '9,11', 'districtCode': '9,10'})


def test_xkt020_independent_contract():
    check('XKT020', [11, 15], {})


def test_xkt021_independent_contract():
    check('XKT021', [11, 15], {'prefectureCode': '09,14', 'administrativeAreaCode': '01101,13102'})


def test_xkt022_independent_contract():
    check('XKT022', [11, 15], {'prefectureCode': '09,14', 'administrativeAreaCode': '01101,13102'})


def test_xkt023_independent_contract():
    check('XKT023', [11, 15], {})


def test_xkt024_independent_contract():
    check('XKT024', [11, 15], {})


def test_xkt025_independent_contract():
    check('XKT025', [11, 15], {})


def test_xkt026_independent_contract():
    check('XKT026', [14, 15], {})


def test_xkt027_independent_contract():
    check('XKT027', [13, 15], {})


def test_xkt028_independent_contract():
    check('XKT028', [14, 15], {})


def test_xkt029_independent_contract():
    check('XKT029', [11, 15], {})


def test_xkt030_independent_contract():
    check('XKT030', [11, 15], {})


def test_xkt031_independent_contract():
    check('XKT031', [9, 15], {'administrativeAreaCode': '01101,13102'})


def test_xgt001_independent_contract():
    check('XGT001', [11, 15], {})


def test_xst001_independent_contract():
    check('XST001', [9, 15], {'disastertype_code': '11,22,37'})


def test_xnt001_independent_contract():
    check('XNT001', [11, 15], {})


# END_SLICES
