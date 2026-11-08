"""Allowlisted single-tile retrieval. No joins, deduplication or PBF decoding."""
import json
from urllib.parse import urlencode

from .storage import CoreError
from .layer_provenance import provenance
if '.' in (__package__ or ''):
    from ..client import Client, CallError, parse_origin
    from ..schema import contains_secret
else:
    from client import Client, CallError, parse_origin
    from schema import contains_secret

# Each entry is added only after its own RED contract test.
from pathlib import Path
from datetime import date
import re
CONTRACTS = json.loads((Path(__file__).parent / 'data' / 'tile-contracts.json').read_text())


def _reject_constant(value):
    raise ValueError('non_json_number')


def _json_payload(payload):
    from .storage import _unique_object
    import math
    def finite_number(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('non_finite_number')
        return number
    def bounded(value, depth=0):
        if depth > 32:
            raise ValueError('json_depth')
        if isinstance(value, dict):
            for child in value.values(): bounded(child, depth + 1)
        elif isinstance(value, list):
            for child in value: bounded(child, depth + 1)
    raw = json.loads(payload.decode('utf-8'), parse_constant=_reject_constant,
                     parse_float=finite_number, object_pairs_hook=_unique_object)
    bounded(raw)
    return raw


def validate(endpoint, args, today=None):
    if endpoint not in CONTRACTS or not isinstance(args, dict):
        raise CoreError('invalid_argument', 'unsupported_endpoint')
    spec = CONTRACTS[endpoint]
    required = {'response_format', 'z', 'x', 'y'}
    if not required <= args.keys() or args.keys() - required - spec['filters'].keys():
        raise CoreError('invalid_argument', 'invalid_parameters')
    if args['response_format'] not in ('geojson', 'pbf'):
        raise CoreError('invalid_argument', 'bad_response_format')
    z = args['z']
    if type(z) is not int or not spec['zoom'][0] <= z <= spec['zoom'][1]:
        raise CoreError('invalid_argument', 'bad_zoom')
    if any(type(args[k]) is not int or not 0 <= args[k] < 2 ** z for k in ('x', 'y')):
        raise CoreError('invalid_argument', 'bad_tile_coordinate')
    if any(k not in args for k in spec.get('required', [])):
        raise CoreError('invalid_argument', 'missing_filter')
    clock = today or date.today()
    for name, rule in spec['filters'].items():
        if name not in args:
            continue
        value = args[name]
        if 'year_min' in rule:
            if type(value) is not int or not rule['year_min'] <= value <= clock.year:
                raise CoreError('invalid_argument', 'bad_year')
            continue
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise CoreError('invalid_argument', 'bad_filter')
        values = value.split(',') if rule.get('multiple') else [value]
        if any(('enum' in rule and v not in rule['enum']) or
               ('pattern' in rule and re.fullmatch(rule['pattern'], v) is None) for v in values):
            raise CoreError('invalid_argument', 'bad_filter')
    if endpoint == 'XPT001':
        minimum = '20211' if args.get('priceClassification') == '02' else '20053'
        maximum = str(clock.year) + str((clock.month - 1) // 3 + 1)
        if not minimum <= args['from'] <= args['to'] <= maximum:
            raise CoreError('invalid_argument', 'bad_period_range')
    return dict(args)


def get_payload(endpoint, query, *, key, transport=None, gap=None):
    if endpoint != 'XCT001' and endpoint not in CONTRACTS:
        raise CoreError('invalid_argument', 'unsupported_endpoint')
    return _official_payload(endpoint, query, key=key, transport=transport, gap=gap)


def _official_payload(endpoint, query, *, key, transport=None, gap=None):
    """Internal fixed-origin transport injection site (also used by XCT001).

    `transport` has the M3 inner transport signature. Parent integration can wrap it
    with request(..., gate=adapter.production_gate()) without changing validators.
    """
    try:
        client = Client(parse_origin('https://www.reinfolib.mlit.go.jp'), key, transport=transport, gap=gap)
        url = client.origin.value + '/ex-api/external/' + endpoint + '?' + urlencode(query)
        if contains_secret(url, key):
            raise CoreError('config', 'key_in_url')
        def send():
            return client.transport(url, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': key}, 30, 8_000_000)
        status, headers, payload = client.gap.run(send)
        status, headers, payload = client._finish(status, headers, payload)
    except CallError as error:
        raise CoreError(error.error_type, error.detail, error.status_code) from None
    except TimeoutError:
        raise CoreError('timeout', 'timeout') from None
    except CoreError:
        raise
    except Exception:
        raise CoreError('upstream', 'network') from None
    if status != 200:
        raise CoreError({401: 'auth', 403: 'auth', 404: 'not_found', 429: 'rate_limited'}.get(status, 'upstream'), status_code=status)
    if contains_secret(payload.decode('utf-8', errors='replace'), key):
        raise CoreError('internal', 'secret')
    return payload


def fetch(endpoint, args, *, key, transport=None, gap=None, today=None):
    query = validate(endpoint, args, today=today)
    payload = get_payload(endpoint, query, key=key, transport=transport, gap=gap)
    status = 200
    if query['response_format'] == 'pbf':
        import base64
        return {'ok': True, 'endpoint': endpoint, 'query': query, 'format': 'pbf',
                'encoding': 'base64', 'raw_base64': base64.b64encode(payload).decode('ascii'),
                'decoded': False, 'byte_count': len(payload),
                'geometry_semantics': 'nearest_station' if endpoint == 'XPT001' else 'opaque_tile',
                'property_level_spatial_inference_allowed': False,
                'status': 'raw_pbf' if payload else 'no_features', **provenance()}
    if not payload.strip():
        payload = b'{"type":"FeatureCollection","features":[]}'
    try:
        raw = _json_payload(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise CoreError('invalid_response', 'not_geojson', status) from None
    if contains_secret(raw, key):
        raise CoreError('internal', 'secret')
    if not isinstance(raw, dict) or raw.get('type') != 'FeatureCollection' or not isinstance(raw.get('features'), list):
        raise CoreError('invalid_response', 'not_geojson', status)
    if any(not isinstance(f, dict) or f.get('type') != 'Feature' or
           not isinstance(f.get('properties'), (dict, type(None))) or
           not isinstance(f.get('geometry'), (dict, type(None))) for f in raw['features']):
        raise CoreError('invalid_response', 'bad_feature', status)
    return {'ok': True, 'endpoint': endpoint, 'query': query, 'raw': raw,
            'feature_count': len(raw['features']), 'status': 'features' if raw['features'] else 'no_features',
            'geometry_semantics': 'nearest_station' if endpoint == 'XPT001' else 'tile_fragments_not_spatial_analysis',
            'property_level_spatial_inference_allowed': False,
            'warnings': ['prohibited_property_hazard_school_zone_radius_inference'] if endpoint == 'XPT001' else [],
            **provenance()}
