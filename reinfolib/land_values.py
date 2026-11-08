"""XCT001 appraisal background, not arbitrary property valuation."""
from datetime import date
import json
import re
from .layers import get_payload, _json_payload, contains_secret
from .storage import CoreError
from .layer_provenance import provenance

DIVISIONS = ('00','03','05','07','09','10','13','20')


def validate(args, today=None):
    clock = today or date.today()
    if not isinstance(args, dict) or set(args) != {'year','area','division'}:
        raise CoreError('invalid_argument','invalid_parameters')
    year, area, division = args['year'], args['area'], args['division']
    if type(year) is not int or not clock.year-4 <= year <= clock.year:
        raise CoreError('invalid_argument','bad_year')
    if not isinstance(area,str) or not re.fullmatch('[0-9]{2}',area) or not 1 <= int(area) <= 47:
        raise CoreError('invalid_argument','bad_area')
    if division not in DIVISIONS:
        raise CoreError('invalid_argument','bad_division')
    return dict(args)


def fetch(args, *, key, transport=None, gap=None, today=None):
    query = validate(args, today=today)
    payload = get_payload('XCT001',query,key=key,transport=transport,gap=gap)
    try:
        raw=_json_payload(payload)
    except (ValueError,UnicodeError,RecursionError):
        raise CoreError('invalid_response','not_json',200) from None
    if contains_secret(raw,key):
        raise CoreError('internal','secret')
    records = raw if isinstance(raw,list) else raw.get('data') if isinstance(raw,dict) and raw.get('status')=='OK' else None
    if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
        raise CoreError('invalid_response','bad_records',200)
    return {'ok':True,'endpoint':'XCT001','query':query,'raw':raw,'record_count':len(records),
            'status':'records' if records else 'no_records',
            'source_year':query['year'],'limitations':['standard_land_background_not_property_appraisal'],**provenance()}
