"""Raw XIT001 transaction path; no Hermes or secret-scope imports."""
import json
import re
from datetime import date
from urllib.parse import urlencode

from .storage import CoreError, SnapshotStore, _period, _unique_object, _validate_json

if '.' in (__package__ or ''):
    from ..client import CallError, Client, parse_origin
    from ..schema import contains_secret
else:
    from client import CallError, Client, parse_origin
    from schema import contains_secret


def validate_query(args: dict, today=None) -> dict:
    """Validate without coercion; include defaults, never invent a scope or quarter."""
    clock = today or date.today()
    allowed = {'year', 'quarter', 'area', 'city', 'station', 'language', 'price_classification'}
    if not isinstance(args, dict):
        raise CoreError('invalid_argument', 'not_object')
    if any(name not in allowed for name in args):
        raise CoreError('invalid_argument', 'unknown_argument')
    year = args.get('year')
    if type(year) is not int or not 2005 <= year <= clock.year:
        raise CoreError('invalid_argument', 'bad_year')
    scopes = [name for name in ('area', 'city', 'station') if name in args]
    if len(scopes) != 1:
        raise CoreError('invalid_argument', 'bad_scope')
    scope = scopes[0]
    code = args[scope]
    width = {'area': 2, 'city': 5, 'station': 6}[scope]
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{%d}' % width, code):
        raise CoreError('invalid_argument', 'bad_' + scope)
    if scope != 'station' and not 1 <= int(code[:2]) <= 47:
        raise CoreError('invalid_argument', 'bad_' + scope)
    if 'quarter' in args:
        quarter = args['quarter']
        if type(quarter) is not int or not 1 <= quarter <= 4:
            raise CoreError('invalid_argument', 'bad_quarter')
        if year == 2005 and quarter < 3:
            raise CoreError('invalid_argument', 'bad_quarter')
        if year == clock.year and (quarter - 1) * 3 + 1 > clock.month:
            raise CoreError('invalid_argument', 'future_quarter')
    classification = args.get('price_classification', '01')
    language = args.get('language', 'ja')
    if classification not in ('01', '02', 'both') or classification == '02' and year < 2021:
        raise CoreError('invalid_argument', 'bad_price_classification')
    if language not in ('ja', 'en'):
        raise CoreError('invalid_argument', 'bad_language')
    return {'price_classification': classification, 'language': language, **args}


def fetch_transactions(args, *, key: str, storage: SnapshotStore, transport=None, gap=None, today=None,
                       retry=None, clock=None, sleep=None, absolute_deadline=None) -> dict:
    """One bounded official GET, then seal the whole response before saving raw rows.

    HTTP 404 is not_found; HTTP 200 with empty data is invalid_response/empty_data.
    Unknown Period formats are retained with a snapshot warning, not guessed.

    With ``retry`` (a transport.RetryPolicy) the GET goes through transport.request:
    bounded 429/5xx/network retry, every attempt through ``gap`` (a RateGate) and the
    result/CoreError carry ``attempts``. Without it the original single GET is unchanged.
    """
    query = validate_query(args, today=today)
    if not isinstance(key, str) or not key or any(ch.isspace() for ch in key):
        raise CoreError('config', 'missing_key')
    if contains_secret(query, key):
        raise CoreError('internal', 'secret')
    client = Client(parse_origin('https://www.reinfolib.mlit.go.jp'), key, transport=transport, gap=gap)
    url = client.origin.value + '/ex-api/external/XIT001?' + urlencode({
        ('priceClassification' if name == 'price_classification' else name): value
        for name, value in query.items() if not (name == 'price_classification' and value == 'both')})
    if contains_secret(url, key):
        raise CoreError('config', 'key_in_url')
    def send():
        try:
            return client.transport(url, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': key},
                                    30, 8_000_000)
        except TimeoutError:
            raise CoreError('timeout', 'timeout') from None
        except CallError as error:
            # Only fixed transport diagnostics may cross this boundary.
            kind = error.error_type if error.error_type in ('timeout', 'invalid_response', 'upstream') else 'upstream'
            detail = error.detail if error.detail in ('too_big', 'timeout', 'network') else 'network'
            raise CoreError(kind, detail) from None
        except Exception:
            raise CoreError('upstream', 'network') from None
    attempts = None
    if retry is None:
        status, headers, payload = client.gap.run(send)
    else:
        from .transport import TransportFailure, request
        try:
            response = request(url, {'Accept': 'application/json', 'Ocp-Apim-Subscription-Key': key},
                               timeout=30, cap=8_000_000, policy=retry, transport=client.transport,
                               gate=gap, clock=clock, sleep=sleep, absolute_deadline=absolute_deadline)
        except TransportFailure as failure:
            error = CoreError(failure.kind, failure.detail)
            error.attempts = failure.attempts
            raise error from None
        status, headers, payload, attempts = (response.status, response.headers, response.payload,
                                              response.attempts)
    try:
        snapshot = _seal(query, key, client, storage, status, headers, payload)
    except CoreError as error:
        if attempts is not None:
            error.attempts = attempts
            error.stop_reason = response.stop_reason
        raise
    if attempts is not None:
        snapshot['attempts'] = attempts
    return snapshot


def _seal(query, key, client, storage, status, headers, payload):
    try:
        status, headers, payload = client._finish(status, headers, payload)
    except CallError as error:
        raise CoreError(error.error_type, error.detail, error.status_code) from None
    except (ValueError, TypeError, OverflowError):
        raise CoreError('invalid_response', 'bad_headers', status) from None
    if status != 200:
        kind = {404: 'not_found', 401: 'auth', 403: 'auth', 429: 'rate_limited'}.get(status, 'upstream')
        if status == 400 and 'station' in query:
            kind = 'fallback_required'
        raise CoreError(kind, status_code=status)
    try:
        text = payload.decode('utf-8')
        if contains_secret(text, key):
            raise CoreError('internal', 'secret')
        body = json.loads(text, parse_float=_unsupported_number,
                          parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
        _validate_json(body)
    except (ValueError, UnicodeError, RecursionError):
        raise CoreError('invalid_response', 'not_json', 200) from None
    if contains_secret(body, key):
        raise CoreError('internal', 'secret')
    if not isinstance(body, dict) or body.get('status') != 'OK' or 'statusCode' in body:
        raise CoreError('invalid_response', 'not_ok', 200)
    records = body.get('data')
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise CoreError('invalid_response', 'bad_record', 200)
    # A successful empty envelope is anomalous: official no-data is HTTP 404.
    if not records:
        raise CoreError('invalid_response', 'empty_data', 200)
    for row in records:
        period = _period(row.get('Period'))
        if period is not None and (period[0] != query['year'] or
                                   'quarter' in query and period[1] != query['quarter']):
            raise CoreError('invalid_response', 'mismatched_period', 200)
    return storage.save(query, records)


def _unsupported_number(value):
    # Official transaction columns are strings. Reject new fractional/exponent JSON
    # numbers rather than silently rounding them through binary float persistence.
    raise CoreError('invalid_response', 'unsupported_json_number', 200)


def _invalid_constant(value):
    raise ValueError('non_json_number')
