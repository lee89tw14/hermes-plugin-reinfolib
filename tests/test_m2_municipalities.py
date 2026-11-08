"""M2 slice 1: XIT002 municipality lookup (offline; injected transports, synthetic key)."""
import gzip
import json
from urllib.parse import parse_qs, urlsplit

import pytest

KEY = 'synthetic-test-key-123456789'


class NoWait:
    def run(self, call):
        return call()


def body(rows, status='OK'):
    return json.dumps({'status': status, 'data': rows}, ensure_ascii=False).encode('utf-8')


# Shape observed in the real XIT002 response (2026-11-08): designated cities are listed
# with their own code, and their wards follow with bare ward names.
KANAGAWA = [
    {'id': '14100', 'name': '横浜市'},
    {'id': '14101', 'name': '鶴見区'},
    {'id': '14104', 'name': '中区'},
    {'id': '14113', 'name': '緑区'},
    {'id': '14130', 'name': '川崎市'},
    {'id': '14131', 'name': '川崎区'},
    {'id': '14150', 'name': '相模原市'},
    {'id': '14151', 'name': '緑区'},
    {'id': '14152', 'name': '中央区'},
    {'id': '14201', 'name': '横須賀市'},
    {'id': '14341', 'name': '大磯町'},
]


def fetch(args, rows=KANAGAWA, status=200, headers=None, payload=None, calls=None):
    from reinfolib.locations import fetch_municipalities

    def transport(url, sent, timeout, cap):
        if calls is not None:
            calls.append((url, sent, timeout, cap))
        return status, headers or {'content-type': 'application/json'}, body(rows) if payload is None else payload
    return fetch_municipalities(args, key=KEY, transport=transport, gap=NoWait())


@pytest.mark.parametrize('args', [
    None, [], {}, {'area': 13}, {'area': '00'}, {'area': '48'}, {'area': '１３'}, {'area': '13 '},
    {'area': '13', 'language': 'JA'}, {'area': '13', 'language': None},
    {'area': '13', 'name': ''}, {'area': '13', 'name': 'x' * 51}, {'area': '13', 'name': 13},
    {'area': '13', 'city': '13101'},
])
def test_rejects_bad_arguments_without_request(args):
    from reinfolib.storage import CoreError
    calls = []
    with pytest.raises(CoreError) as caught:
        fetch(args, calls=calls)
    assert caught.value.error_type == 'invalid_argument'
    assert calls == []


def test_one_header_auth_get_to_fixed_xit002_url():
    calls = []
    result = fetch({'area': '14'}, calls=calls)
    assert len(calls) == 1
    url, headers, timeout, cap = calls[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ('https', 'www.reinfolib.mlit.go.jp', '/ex-api/external/XIT002')
    assert parse_qs(parts.query) == {'area': ['14'], 'language': ['ja']}
    assert KEY not in url and headers['Ocp-Apim-Subscription-Key'] == KEY
    assert cap == 8_000_000
    assert result['ok'] is True and result['area'] == '14' and result['language'] == 'ja'
    assert result['count'] == len(KANAGAWA)
    assert [m['code'] for m in result['municipalities']] == [r['id'] for r in KANAGAWA]
    assert result['fetched_at'].endswith('Z')
    assert KEY not in json.dumps(result, ensure_ascii=False)


def test_gzip_and_english():
    rows = [{'id': '13101', 'name': 'Chiyoda Ward'}]
    calls = []
    result = fetch({'area': '13', 'language': 'en'}, headers={'Content-Encoding': 'gzip'},
                   payload=gzip.compress(body(rows)), calls=calls)
    assert parse_qs(urlsplit(calls[0][0]).query)['language'] == ['en']
    assert result['municipalities'][0]['name'] == 'Chiyoda Ward'


@pytest.mark.parametrize('status,kind', [(404, 'not_found'), (401, 'auth'), (403, 'auth'),
                                         (429, 'rate_limited'), (400, 'upstream'), (500, 'upstream')])
def test_status_mapping(status, kind):
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        fetch({'area': '13'}, status=status, payload=b'')
    assert caught.value.error_type == kind and caught.value.status_code == status


@pytest.mark.parametrize('payload,detail', [
    (b'not json', 'not_json'),
    (body([], status='NG'), 'not_ok'),
    (body([]), 'empty_data'),
    (body([{'id': '13101'}]), 'bad_record'),
    (body([{'id': '1310', 'name': 'x'}]), 'bad_record'),
    (body([{'id': '14101', 'name': '横浜市鶴見区'}]), 'bad_record'),  # other prefecture
    (body([{'id': '13101', 'name': 'a'}, {'id': '13101', 'name': 'b'}]), 'bad_record'),
    (json.dumps({'status': 'OK', 'data': {}}).encode(), 'bad_record'),
])
def test_invalid_payloads(payload, detail):
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        fetch({'area': '13'}, payload=payload)
    assert (caught.value.error_type, caught.value.detail) == ('invalid_response', detail)


def test_name_substring_lists_all_candidates_without_choosing():
    result = fetch({'area': '14', 'name': '横浜市'})
    assert result['match_status'] == 'ambiguous'
    assert [m['code'] for m in result['candidates']] == ['14100', '14101', '14104', '14113']
    assert 'municipalities' not in result
    assert 'selected' not in result


def test_name_unique_and_none():
    unique = fetch({'area': '14', 'name': '横須賀'})
    assert unique['match_status'] == 'unique'
    assert [m['code'] for m in unique['candidates']] == ['14201']
    none = fetch({'area': '14', 'name': '札幌'})
    assert none['match_status'] == 'no_match' and none['candidates'] == []


def test_name_match_is_width_and_case_insensitive():
    rows = [{'id': '13101', 'name': 'Chiyoda Ward'}]
    assert fetch({'area': '13', 'language': 'en', 'name': 'ｃｈｉｙｏｄａ'}, rows=rows)['match_status'] == 'unique'


def test_secret_echo_is_refused():
    from reinfolib.storage import CoreError
    with pytest.raises(CoreError) as caught:
        fetch({'area': '13'}, rows=[{'id': '13101', 'name': KEY}])
    assert caught.value.error_type == 'internal'


def test_kinds_distinguish_designated_city_wards_special_wards_and_cities():
    result = fetch({'area': '14'})
    kinds = {m['code']: m['kind'] for m in result['municipalities']}
    assert kinds['14100'] == 'designated_city' and kinds['14130'] == 'designated_city'
    assert kinds['14101'] == 'designated_city_ward' and kinds['14151'] == 'designated_city_ward'
    assert kinds['14201'] == 'city' and kinds['14341'] == 'town_or_village'
    ward = next(m for m in result['municipalities'] if m['code'] == '14151')
    assert (ward['parent_code'], ward['parent_name'], ward['display_name']) == ('14150', '相模原市', '相模原市緑区')
    city = next(m for m in result['municipalities'] if m['code'] == '14100')
    assert 'parent_code' not in city and city['ward_codes'] == ['14101', '14104', '14113']
    tokyo = fetch({'area': '13'}, rows=[{'id': '13101', 'name': '千代田区'}, {'id': '13201', 'name': '八王子市'}])
    assert [m['kind'] for m in tokyo['municipalities']] == ['special_ward', 'city']


def test_same_ward_name_in_two_designated_cities_is_ambiguous_with_parents():
    result = fetch({'area': '14', 'name': '緑区'})
    assert result['match_status'] == 'ambiguous'
    assert [(m['code'], m['parent_name']) for m in result['candidates']] == [('14113', '横浜市'), ('14151', '相模原市')]
    assert fetch({'area': '14', 'name': '相模原市緑区'})['match_status'] == 'unique'


def test_english_kinds_and_display_name():
    rows = [{'id': '14150', 'name': 'Sagamihara City'}, {'id': '14151', 'name': 'Midori Ward'}]
    result = fetch({'area': '14', 'language': 'en'}, rows=rows)
    assert [m['kind'] for m in result['municipalities']] == ['designated_city', 'designated_city_ward']
    assert result['municipalities'][1]['display_name'] == 'Midori Ward, Sagamihara City'


def test_orphan_ward_code_outside_tokyo_is_unclassified_not_guessed():
    result = fetch({'area': '27'}, rows=[{'id': '27102', 'name': '都島区'}])
    assert result['municipalities'][0]['kind'] == 'unclassified'
