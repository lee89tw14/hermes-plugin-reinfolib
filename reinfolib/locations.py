"""XIT002 municipality list lookup; bounded GET/retry, no files, no Hermes imports.

The list is reference data returned as fetched. Name matching is local and only lists
candidates; it never picks one when several match.
"""
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlencode

from .storage import CoreError, _unique_object, _validate_json

if '.' in (__package__ or ''):
    from ..client import CallError, Client, parse_origin
    from ..schema import contains_secret
else:
    from client import CallError, Client, parse_origin
    from schema import contains_secret

ORIGIN = 'https://www.reinfolib.mlit.go.jp'
PATH = '/ex-api/external/XIT002'


def validate_municipality_query(args) -> dict:
    if not isinstance(args, dict):
        raise CoreError('invalid_argument', 'not_object')
    if set(args) - {'area', 'language', 'name'}:
        raise CoreError('invalid_argument', 'unknown_argument')
    area = args.get('area')
    if not isinstance(area, str) or not re.fullmatch(r'[0-9]{2}', area) or not 1 <= int(area) <= 47:
        raise CoreError('invalid_argument', 'bad_area')
    language = args.get('language', 'ja')
    if language not in ('ja', 'en'):
        raise CoreError('invalid_argument', 'bad_language')
    query = {'area': area, 'language': language}
    if 'name' in args:
        name = args['name']
        if not isinstance(name, str) or not name.strip() or len(name) > 50:
            raise CoreError('invalid_argument', 'bad_name')
        query['name'] = name
    return query


def _norm(text):
    return unicodedata.normalize('NFKC', text).strip().casefold()


KIND_BASIS = ('Derived locally from the code range (1xx ward/designated city, 2xx city, 3xx+ town/village) '
              'and names in this XIT002 response; not an upstream field.')


def _classify(municipalities, area, language):
    """Designated cities are 1xx entries named 市/City; their wards are the 1xx entries that
    follow them. XIT002 returns ward names without the city, so display_name adds the parent."""
    suffix = ' City' if language == 'en' else '市'
    parent = None
    for item in sorted(municipalities, key=lambda m: m['code']):
        code, name = item['code'], item['name']
        if code[2] == '1' and name.endswith(suffix):
            item['kind'], parent = 'designated_city', item
            item['ward_codes'] = []
        elif code[2] == '1' and parent is not None:
            item.update(kind='designated_city_ward', parent_code=parent['code'], parent_name=parent['name'])
            parent['ward_codes'].append(code)
        elif code[2] == '1':
            item['kind'] = 'special_ward' if area == '13' else 'unclassified'
        else:
            parent = None
            item['kind'] = 'city' if code[2] == '2' else 'town_or_village'
        if 'parent_name' in item:
            item['display_name'] = (f"{name}, {item['parent_name']}" if language == 'en'
                                    else item['parent_name'] + name)
        else:
            item['display_name'] = name


def match_candidates(municipalities, name):
    """Every NFKC/case-insensitive substring match on name or display_name, in list order."""
    needle = _norm(name)
    return [m for m in municipalities if needle in _norm(m['name']) or needle in _norm(m['display_name'])]


def _get(query, key, transport, gap, retry=None):
    if not isinstance(key, str) or not key or any(ch.isspace() for ch in key):
        raise CoreError('config', 'missing_key')
    client = Client(parse_origin(ORIGIN), key, transport=transport, gap=gap)
    url = client.origin.value + PATH + '?' + urlencode({'area': query['area'], 'language': query['language']})
    if contains_secret(url, key):
        raise CoreError('config', 'key_in_url')

    def send():
        try:
            return client.transport(url, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': key},
                                    30, 8_000_000)
        except TimeoutError:
            raise CoreError('timeout', 'timeout') from None
        except CallError as error:
            kind = error.error_type if error.error_type in ('timeout', 'invalid_response', 'upstream') else 'upstream'
            detail = error.detail if error.detail in ('too_big', 'timeout', 'network') else 'network'
            raise CoreError(kind, detail) from None
        except Exception:
            raise CoreError('upstream', 'network') from None
    attempts = None
    if retry is None:
        status, headers, payload = client.gap.run(send)
    else:
        from .transport import request, TransportFailure
        try:
            response = request(url, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': key},
                               timeout=30, cap=8_000_000, policy=retry,
                               transport=client.transport, gate=gap)
        except TransportFailure as failure:
            error = CoreError(failure.kind, failure.detail)
            error.attempts = failure.attempts
            raise error from None
        status, headers, payload = response.status, response.headers, response.payload
        attempts = response.attempts
    try:
        return (*client._finish(status, headers, payload), attempts)
    except CallError as error:
        raise CoreError(error.error_type, error.detail, error.status_code) from None
    except (ValueError, TypeError, OverflowError):
        raise CoreError('invalid_response', 'bad_headers', status) from None


def fetch_municipalities(args, *, key: str, transport=None, gap=None, retry=None) -> dict:
    """HTTP 404 is not_found (official no-data reply), never an empty municipality list."""
    query = validate_municipality_query(args)
    if contains_secret(query, key):
        raise CoreError('internal', 'secret')
    status, _headers, payload, attempts = _get(query, key, transport, gap, retry)
    if status != 200:
        kind = {404: 'not_found', 401: 'auth', 403: 'auth', 429: 'rate_limited'}.get(status, 'upstream')
        error = CoreError(kind, status_code=status)
        if attempts is not None:
            error.attempts = attempts
        raise error
    try:
        text = payload.decode('utf-8')
        if contains_secret(text, key):
            raise CoreError('internal', 'secret')
        data = json.loads(text, object_pairs_hook=_unique_object)
        _validate_json(data)
    except (ValueError, UnicodeError, RecursionError):
        raise CoreError('invalid_response', 'not_json', 200) from None
    if not isinstance(data, dict) or data.get('status') != 'OK':
        raise CoreError('invalid_response', 'not_ok', 200)
    rows = data.get('data')
    if not isinstance(rows, list):
        raise CoreError('invalid_response', 'bad_record', 200)
    if not rows:
        raise CoreError('invalid_response', 'empty_data', 200)
    seen = set()
    municipalities = []
    for row in rows:
        code, name = (row.get('id'), row.get('name')) if isinstance(row, dict) else (None, None)
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{5}', code) or code[:2] != query['area']
                or code in seen or not isinstance(name, str) or not name):
            raise CoreError('invalid_response', 'bad_record', 200)
        seen.add(code)
        municipalities.append({'code': code, 'name': name})
    _classify(municipalities, query['area'], query['language'])
    result = {'ok': True, 'endpoint': 'XIT002', 'area': query['area'], 'language': query['language'],
              'fetched_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'count': len(municipalities), 'kind_basis': KIND_BASIS}
    if 'name' in query:
        candidates = match_candidates(municipalities, query['name'])
        result.update(query_name=query['name'], candidates=candidates,
                      match_status='no_match' if not candidates else 'unique' if len(candidates) == 1 else 'ambiguous')
    else:
        result['municipalities'] = municipalities
    if attempts is not None:
        result['attempts'] = attempts
    if contains_secret(result, key):
        raise CoreError('internal', 'secret')
    return result
