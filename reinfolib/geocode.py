"""GSI (国土地理院) address search through the M3 gated transport.

The coordinate GSI returns is a representative point of the matched address text
(often a block, 街区), never a parcel or building footprint. GSI returns only
``title``, ``addressCode`` and a Point; ``matched_level`` is derived here from the
title text, conservatively. No API key is involved.

Terms (checked 2026-11-08):
- 国土地理院コンテンツ利用規約 (PDL1.0 based; credit 「出典：国土地理院ウェブサイト」,
  state processing, never imply GSI made processed output):
  https://www.gsi.go.jp/kikakuchousei/kikakuchousei40182.html
- 地理院地図 利用規約 (no liability; traffic that burdens the server may be blocked
  without notice): https://maps.gsi.go.jp/help/termsofuse.html
- GSI FAQ (gsimaps issue #29, label よくあるご質問): the server-side search is meant
  mainly for GSI Maps, may not be offered always or long-term and may change without
  notice; address matching uses CSIS シンプルジオコーディング:
  https://github.com/gsi-cyberjapan/gsimaps/issues/29
"""
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import quote

from .storage import CoreError

ORIGIN = 'https://msearch.gsi.go.jp'
PATH = '/address-search/AddressSearch'
CAP = 1_000_000
TIMEOUT = 15
MAX_ADDRESS = 200
MAX_RETURNED = 50
# Generous WGS84 box around Japan (Yonaguni 122.9E, Minamitorishima 154.0E,
# Okinotorishima 20.4N, Etorofu 45.6N). A screen, not a sovereignty boundary.
JAPAN_BOUNDS = (122.0, 20.0, 154.0, 46.0)
RELIABLE_LEVELS = ('house', 'block')
SEMANTICS = 'geocoded_address_representative_point_not_parcel_or_building'
TERMS = {
    'content_terms_url': 'https://www.gsi.go.jp/kikakuchousei/kikakuchousei40182.html',
    'service_terms_url': 'https://maps.gsi.go.jp/help/termsofuse.html',
    'availability_faq_url': 'https://github.com/gsi-cyberjapan/gsimaps/issues/29',
}
ATTRIBUTION = '出典：国土地理院ウェブサイト（https://msearch.gsi.go.jp/address-search/AddressSearch）'
PROCESSING = ('国土地理院の住所検索結果をもとに本pluginが加工（詳細度の分類、日本範囲外の除外、候補IDの付与）。'
              '加工結果は国土地理院が作成したものではありません。')
AVAILABILITY = ('GSI states this server-side search is meant mainly for GSI Maps, may not be offered '
                'always or long-term and may change without notice; its address matching uses CSIS '
                'simple geocoding. Requests are spaced at least 1 second apart.')


def provenance():
    return {'geocoder_source': '国土地理院 地理院地図 住所検索 (msearch.gsi.go.jp AddressSearch)',
            'geocoder_attribution': ATTRIBUTION, 'geocoder_processing': PROCESSING,
            'geocoder_availability_note': AVAILABILITY, 'geocoder_terms': dict(TERMS),
            'coordinate_semantics': SEMANTICS}


def validate_address(value):
    if not isinstance(value, str):
        raise CoreError('invalid_argument', 'bad_address')
    text = value.strip()
    if not text or len(text) > MAX_ADDRESS or any(unicodedata.category(c)[0] == 'C' for c in text):
        raise CoreError('invalid_argument', 'bad_address')
    return text


def build_url(address):
    return ORIGIN + PATH + '?q=' + quote(address, safe='')


def classify_level(title):
    text = unicodedata.normalize('NFKC', title).strip()
    if re.search(r'号$', text):
        return 'house'
    if re.search(r'[0-9]+番(?:地)?(?:[のノ\-−‐][0-9]+)*$', text):
        return 'block'
    if re.search(r'丁目$', text):
        return 'chome'
    return 'area_or_place'


def overlay_eligible(level):
    return level in RELIABLE_LEVELS


def _reject_constant(_value):
    raise ValueError('non_json_number')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse(payload):
    """Return (candidates, outside_japan_excluded_n); any shape problem is fixed invalid_response."""
    try:
        raw = json.loads(payload.decode('utf-8'), parse_constant=_reject_constant)
        if not isinstance(raw, list):
            raise ValueError('not_list')
        candidates, outside = [], 0
        for feature in raw:
            geometry = feature.get('geometry') if isinstance(feature, dict) else None
            properties = feature.get('properties') if isinstance(feature, dict) else None
            if not isinstance(geometry, dict) or geometry.get('type') != 'Point' or not isinstance(properties, dict):
                raise ValueError('bad_feature')
            coordinates = geometry.get('coordinates')
            if not isinstance(coordinates, list) or len(coordinates) < 2 or not all(map(_number, coordinates[:2])):
                raise ValueError('bad_point')
            title, code = properties.get('title'), properties.get('addressCode', '')
            if not isinstance(title, str) or not title or not isinstance(code, str):
                raise ValueError('bad_properties')
            lon, lat = float(coordinates[0]), float(coordinates[1])
            west, south, east, north = JAPAN_BOUNDS
            if not (west <= lon <= east and south <= lat <= north):
                outside += 1
                continue
            level = classify_level(title)
            identity = hashlib.sha256(f'{title}|{lon:.6f}|{lat:.6f}'.encode()).hexdigest()[:16]
            candidates.append({'candidate_id': identity, 'title': title, 'address_code': code,
                               'lon': lon, 'lat': lat, 'matched_level': level,
                               'overlay_eligible': overlay_eligible(level)})
    except (ValueError, UnicodeError, RecursionError, AttributeError, TypeError):
        raise CoreError('invalid_response', 'not_geocode_json', 200) from None
    return candidates, outside


def geocode(address, *, transport, gate, policy, absolute_deadline=None):
    """One GSI search (bounded retry, gated). Raises CoreError with fixed fields."""
    from .transport import request, TransportFailure
    text = validate_address(address)
    url = build_url(text)
    try:
        response = request(url, {'Accept': 'application/json'}, timeout=TIMEOUT, cap=CAP, policy=policy,
                           transport=transport, gate=gate, absolute_deadline=absolute_deadline)
    except TransportFailure as failure:
        error = CoreError(failure.kind, failure.detail)
        error.attempts = failure.attempts
        raise error from None
    status = response.status
    headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}

    def fail(kind, detail=''):
        error = CoreError(kind, detail, status)
        error.attempts = response.attempts
        return error
    if 300 <= status < 400:
        raise fail('redirect', 'redirect')
    if status == 404:
        raise fail('not_found', 'geocoder_not_found')
    if status == 429:
        raise fail('rate_limited', 'rate_limited')
    if status != 200:
        raise fail('upstream', 'geocoder_status')
    if headers.get('content-encoding', '').strip().lower() not in ('', 'identity'):
        raise fail('invalid_response', 'bad_encoding')
    if len(response.payload) > CAP:
        raise fail('response_too_large', 'too_big')
    try:
        candidates, outside = parse(response.payload)
    except CoreError as error:
        error.attempts = response.attempts
        raise
    return {'ok': True, 'query': text, 'status': 'candidates' if candidates else 'no_candidates',
            'candidate_count': len(candidates), 'returned_count': min(len(candidates), MAX_RETURNED),
            'truncated': len(candidates) > MAX_RETURNED, 'candidates': candidates[:MAX_RETURNED],
            'all_candidates': candidates, 'outside_japan_excluded_n': outside,
            'attempts': response.attempts, 'stop_reason': response.stop_reason, **provenance()}


def select(result, candidate_id=None):
    """Pick one candidate explicitly; never auto-pick among several; refuse coarse matches."""
    candidates = result.get('all_candidates', result.get('candidates', []))
    if candidate_id is not None:
        chosen = [c for c in candidates if c['candidate_id'] == candidate_id]
        if not chosen:
            raise CoreError('not_found', 'candidate_not_found')
        candidate = chosen[0]
    elif not candidates:
        raise CoreError('not_found', 'no_geocode_candidates')
    elif len(candidates) > 1:
        raise CoreError('ambiguous_location', 'multiple_candidates')
    else:
        candidate = candidates[0]
    if not candidate['overlay_eligible']:
        raise CoreError('location_not_reliable', 'matched_level_' + candidate['matched_level'])
    return candidate
