"""M5 slice a: GSI address search through the M3 gated transport (offline, injected wire)."""
import json

import pytest

from reinfolib import geocode
from reinfolib.scheduling import RateGate
from reinfolib.storage import CoreError
from reinfolib.transport import RetryPolicy

TOCHO = [{"geometry": {"coordinates": [139.691772, 35.689629], "type": "Point"}, "type": "Feature",
          "properties": {"addressCode": "", "title": "東京都新宿区西新宿二丁目８番"}}]
FAST = RetryPolicy(max_attempts=3, backoff_base=0, backoff_max=0, deadline=30)


def wire(payload, status=200, headers=None, seen=None):
    def call(url, headers_, timeout, cap):
        if seen is not None:
            seen.append((url, dict(headers_), timeout, cap))
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        return status, headers or {'content-type': 'application/json'}, body
    return call


def run(address, payload, tmp_path, **kw):
    gate = RateGate(tmp_path / 'gsi-gate', spacing=0)
    return geocode.geocode(address, transport=wire(payload, **kw), gate=gate, policy=FAST)


def test_single_block_level_candidate_is_reported_with_gsi_fields_and_semantics(tmp_path):
    seen = []
    gate = RateGate(tmp_path / 'gsi-gate', spacing=0)
    result = geocode.geocode('東京都新宿区西新宿2-8-1', transport=wire(TOCHO, seen=seen), gate=gate, policy=FAST)
    assert result['ok'] is True and result['candidate_count'] == 1
    (cand,) = result['candidates']
    assert cand['title'] == '東京都新宿区西新宿二丁目８番'
    assert cand['lon'] == 139.691772 and cand['lat'] == 35.689629
    assert cand['address_code'] == ''
    assert cand['matched_level'] == 'block' and cand['overlay_eligible'] is True
    assert len(cand['candidate_id']) == 16
    assert result['coordinate_semantics'] == 'geocoded_address_representative_point_not_parcel_or_building'
    assert '国土地理院' in result['geocoder_attribution']
    assert result['attempts'] == [{'n': 1, 'status': 200}]
    (url, headers, timeout, cap) = seen[0]
    assert url.startswith('https://msearch.gsi.go.jp/address-search/AddressSearch?q=')
    assert 'Ocp-Apim-Subscription-Key' not in headers
    assert cap <= 1_000_000 and timeout <= 30


@pytest.mark.parametrize('title,level,eligible', [
    ('東京都新宿区西新宿二丁目８番１号', 'house', True),
    ('東京都新宿区西新宿二丁目８番', 'block', True),
    ('北海道札幌市中央区北一条西二丁目１番地', 'block', True),
    ('長野県松本市大字島内１２３番地の４', 'block', True),
    ('東京都新宿区西新宿二丁目', 'chome', False),
    ('東京都新宿区西新宿', 'area_or_place', False),
    ('東京都新宿区', 'area_or_place', False),
    ('富士山', 'area_or_place', False),
])
def test_matched_level_is_derived_from_the_gsi_title_conservatively(title, level, eligible):
    assert geocode.classify_level(title) == level
    assert geocode.overlay_eligible(level) is eligible


def test_candidates_outside_japan_bounds_are_excluded_and_counted(tmp_path):
    payload = TOCHO + [{"geometry": {"coordinates": [-0.1, 51.5], "type": "Point"}, "type": "Feature",
                        "properties": {"addressCode": "", "title": "外国"}}]
    result = run('西新宿', payload, tmp_path)
    assert result['candidate_count'] == 1 and result['outside_japan_excluded_n'] == 1


def test_empty_list_is_no_candidates_not_an_error(tmp_path):
    result = run('存在しない住所ああああ', [], tmp_path)
    assert result['ok'] is True and result['status'] == 'no_candidates' and result['candidates'] == []


@pytest.mark.parametrize('bad', ['', '   ', 'a' * 201, 123, None, 'x\ny', '\x00'])
def test_bad_address_is_rejected_before_any_request(bad, tmp_path):
    seen = []
    with pytest.raises(CoreError) as error:
        geocode.geocode(bad, transport=wire(TOCHO, seen=seen), gate=RateGate(tmp_path / 'g', spacing=0), policy=FAST)
    assert (error.value.error_type, error.value.detail) == ('invalid_argument', 'bad_address')
    assert seen == []


@pytest.mark.parametrize('payload', [b'not json', b'{"a":1}', [{"geometry": {"type": "Point", "coordinates": ["x", 1]}}],
                                     [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [139.0, 35.0]}, "properties": {"title": 5}}],
                                     [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}, "properties": {"title": "a"}}],
                                     b'[{"type":"Feature","geometry":{"type":"Point","coordinates":[NaN,1]},"properties":{"title":"a"}}]'])
def test_malformed_gsi_payload_is_fixed_invalid_response(payload, tmp_path):
    with pytest.raises(CoreError) as error:
        run('西新宿', payload, tmp_path)
    assert (error.value.error_type, error.value.detail) == ('invalid_response', 'not_geocode_json')


@pytest.mark.parametrize('status,kind', [(404, 'not_found'), (403, 'upstream'), (400, 'upstream'), (500, 'upstream'), (429, 'rate_limited')])
def test_http_errors_map_to_fixed_types(status, kind, tmp_path):
    with pytest.raises(CoreError) as error:
        run('西新宿', TOCHO, tmp_path, status=status)
    assert error.value.error_type == kind and error.value.status_code == status


def test_redirect_and_oversize_and_encoding_are_refused(tmp_path):
    for kw, detail in [({'status': 302}, 'redirect'),
                       ({'headers': {'content-encoding': 'gzip'}}, 'bad_encoding')]:
        with pytest.raises(CoreError) as error:
            run('西新宿', TOCHO, tmp_path, **kw)
        assert error.value.detail == detail
    with pytest.raises(CoreError) as error:
        run('西新宿', b'[' + b' ' * 1_000_001 + b']', tmp_path)
    assert (error.value.error_type, error.value.detail) == ('response_too_large', 'too_big')


def test_retries_503_through_the_gate_and_reports_attempts(tmp_path):
    answers = iter([(503, {}, b''), (200, {}, json.dumps(TOCHO).encode())])
    result = geocode.geocode('西新宿', transport=lambda *a: next(answers),
                             gate=RateGate(tmp_path / 'g', spacing=0), policy=FAST)
    assert [a.get('status') for a in result['attempts']] == [503, 200]


def test_gate_storage_failure_is_fixed_storage_without_http(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    seen = []
    with pytest.raises(CoreError) as error:
        geocode.geocode('西新宿', transport=wire(TOCHO, seen=seen), gate=RateGate(blocker / 'g', spacing=0), policy=FAST)
    assert (error.value.error_type, error.value.detail) == ('storage', 'rate_gate_unavailable')
    assert seen == []


def test_candidate_selection_never_auto_picks_among_several(tmp_path):
    two = TOCHO + [{"geometry": {"coordinates": [135.5, 34.7], "type": "Point"}, "type": "Feature",
                    "properties": {"addressCode": "", "title": "大阪府大阪市北区梅田一丁目１番"}}]
    result = run('一丁目１番', two, tmp_path)
    with pytest.raises(CoreError) as error:
        geocode.select(result)
    assert (error.value.error_type, error.value.detail) == ('ambiguous_location', 'multiple_candidates')
    chosen = geocode.select(result, result['candidates'][1]['candidate_id'])
    assert chosen['title'].startswith('大阪府')
    with pytest.raises(CoreError) as error:
        geocode.select(result, '0' * 16)
    assert (error.value.error_type, error.value.detail) == ('not_found', 'candidate_not_found')


def test_selection_refuses_coarse_or_missing_location(tmp_path):
    coarse = [{"geometry": {"coordinates": [139.69, 35.69], "type": "Point"}, "type": "Feature",
               "properties": {"addressCode": "", "title": "東京都新宿区西新宿二丁目"}}]
    with pytest.raises(CoreError) as error:
        geocode.select(run('西新宿二丁目', coarse, tmp_path))
    assert (error.value.error_type, error.value.detail) == ('location_not_reliable', 'matched_level_chome')
    with pytest.raises(CoreError) as error:
        geocode.select(run('ああ', [], tmp_path))
    assert (error.value.error_type, error.value.detail) == ('not_found', 'no_geocode_candidates')
    single = geocode.select(run('西新宿2-8-1', TOCHO, tmp_path))
    assert single['matched_level'] == 'block'
