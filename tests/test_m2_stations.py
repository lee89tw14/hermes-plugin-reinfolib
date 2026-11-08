"""M2 slice 2: N02 station directory (offline fixture + the packaged real-data index)."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def feature(name, code, group, line, operator):
    return {'type': 'Feature', 'properties': {
        'N02_001': '11', 'N02_002': '2', 'N02_003': line, 'N02_004': operator,
        'N02_005': name, 'N02_005c': code, 'N02_005g': group},
        'geometry': {'type': 'LineString', 'coordinates': [[139.0, 35.0], [139.001, 35.001]]}}


FEATURES = [
    feature('府中', '000101', '000101', '京王線', '京王電鉄'),
    feature('府中', '000202', '000202', '福塩線', '西日本旅客鉄道'),
    feature('新宿', '000301', '000300', '山手線', '東日本旅客鉄道'),
    feature('新宿', '000300', '000300', '中央線', '東日本旅客鉄道'),
    feature('新宿', '000302', '000300', '京王線', '京王電鉄'),
    feature('西新宿五丁目', '003696', '003696', '12号線大江戸線', '東京都'),
    feature('西新宿', '000400', '000400', '4号線丸ノ内線', '東京地下鉄'),
]


def fixture_zip(features=FEATURES, dataset='N02-25', extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(f'{dataset}_GML/UTF-8/{dataset}_Station.geojson',
                         json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def build(features=FEATURES):
    from reinfolib.stations import build_index
    data, digest = fixture_zip(features)
    return build_index(data, dataset='N02-25', expected_sha256=digest)


def test_build_index_records_version_hash_and_license():
    index = build()
    assert index['dataset'] == 'N02-25'
    assert index['source_zip_sha256'] == fixture_zip()[1]
    assert index['source_url'].startswith('https://nlftp.mlit.go.jp/ksj/gml/data/N02/N02-25/')
    assert 'CC BY 4.0' in index['license'] and '加工して作成' in index['attribution']
    assert index['station_count'] == len(FEATURES) and index['group_count'] == 5
    assert '139.0' not in json.dumps(index['rows']) and all(len(row) == 5 for row in index['rows'])


def test_build_refuses_hash_mismatch_and_missing_member():
    from reinfolib.stations import build_index, StationIndexError
    data, _ = fixture_zip()
    with pytest.raises(StationIndexError) as caught:
        build_index(data, dataset='N02-25', expected_sha256='0' * 64)
    assert caught.value.detail == 'sha256_mismatch'
    other, digest = fixture_zip(dataset='N02-24')
    with pytest.raises(StationIndexError) as caught:
        build_index(other, dataset='N02-25', expected_sha256=digest)
    assert caught.value.detail == 'station_member_missing'
    with pytest.raises(StationIndexError) as caught:
        build_index(b'not a zip', dataset='N02-25', expected_sha256=hashlib.sha256(b'not a zip').hexdigest())
    assert caught.value.detail == 'bad_zip'


@pytest.mark.parametrize('bad', [
    {'N02_005g': '12345'}, {'N02_005g': 12345}, {'N02_005': ''}, {'N02_005c': None},
])
def test_build_refuses_malformed_station_properties(bad):
    from reinfolib.stations import StationIndexError
    broken = json.loads(json.dumps(FEATURES))
    broken[0]['properties'].update(bad)
    with pytest.raises(StationIndexError) as caught:
        build(broken)
    assert caught.value.detail == 'bad_station_record'


def test_same_name_in_different_groups_is_ambiguous_and_lists_every_line():
    from reinfolib.stations import lookup
    result = lookup('府中', index=build())
    assert result['match_status'] == 'ambiguous' and result['match_type'] == 'exact'
    assert [c['group_code'] for c in result['candidates']] == ['000101', '000202']
    assert result['candidates'][1]['lines'] == [{'operator': '西日本旅客鉄道', 'line': '福塩線', 'station_code': '000202'}]
    assert 'selected' not in result


def test_one_group_with_several_lines_is_one_candidate():
    from reinfolib.stations import lookup
    result = lookup('新宿駅', index=build())
    assert result['match_status'] == 'unique'
    candidate = result['candidates'][0]
    assert candidate['group_code'] == '000300' and candidate['station_name'] == '新宿'
    assert [l['line'] for l in candidate['lines']] == ['中央線', '京王線', '山手線']


def test_exact_match_hides_partial_but_partial_used_when_no_exact():
    from reinfolib.stations import lookup
    index = build()
    exact = lookup('西新宿', index=index)
    assert exact['match_type'] == 'exact' and [c['group_code'] for c in exact['candidates']] == ['000400']
    assert exact['partial_match_count'] == 1
    partial = lookup('新宿五', index=index)
    assert partial['match_type'] == 'partial' and partial['match_status'] == 'unique'
    assert partial['candidates'][0]['group_code'] == '003696'
    none = lookup('存在しない駅名', index=index)
    assert none['match_status'] == 'no_match' and none['candidates'] == []


def test_lookup_by_group_code_and_nfkc_name():
    from reinfolib.stations import lookup
    index = build()
    assert lookup(code='003696', index=index)['candidates'][0]['station_name'] == '西新宿五丁目'
    assert lookup('ﾌ中', index=index)['match_status'] == 'no_match'  # no fuzzy kana guessing
    assert lookup('府中　', index=index)['match_status'] == 'ambiguous'  # NFKC strips full-width space


@pytest.mark.parametrize('kwargs', [{}, {'name': '', 'code': None}, {'name': 'x' * 51},
                                    {'code': '12345'}, {'name': '新宿', 'code': '000300'}])
def test_lookup_argument_errors(kwargs):
    from reinfolib.stations import lookup, StationIndexError
    with pytest.raises(StationIndexError) as caught:
        lookup(index=build(), **kwargs)
    assert caught.value.error_type == 'invalid_argument'


def test_candidate_limit_reports_truncation():
    from reinfolib.stations import lookup
    many = [feature('駅' + str(i), f'{i:06d}', f'{i:06d}', 'L', 'O') for i in range(1, 80)]
    result = lookup('駅', index=build(many), limit=10)
    assert result['candidate_count'] == 79 and len(result['candidates']) == 10
    assert result['truncated'] is True


def test_packaged_index_is_real_n02_25_and_contains_nishi_shinjuku_gochome():
    from reinfolib.stations import load_index, lookup
    index = load_index()
    assert index['dataset'] == 'N02-25'
    assert index['source_zip_sha256'] == 'aaf76af133b2e771e538fabc4646d2e443dc1d5a67b221382a28d744e706cc9f'
    result = lookup('西新宿五丁目', index=index)
    assert result['match_status'] == 'unique'
    assert result['candidates'][0]['group_code'] == '003696'
    assert result['candidates'][0]['lines'] == [{'operator': '東京都', 'line': '12号線大江戸線', 'station_code': '003696'}]
    assert index['station_count'] == 10234 and index['group_count'] == 9046


def test_rebuild_from_official_zip_is_byte_identical():
    """Opt-in: REINFOLIB_N02_ZIP points at the official N02-25_GML.zip downloaded once."""
    import os
    source = os.environ.get('REINFOLIB_N02_ZIP')
    if not source:
        pytest.skip('Set REINFOLIB_N02_ZIP to the official N02-25_GML.zip to rebuild the index.')
    from reinfolib.stations import build_index, dumps_index, INDEX_PATH, load_index
    packaged = load_index()
    rebuilt = build_index(Path(source).read_bytes(), dataset='N02-25',
                          expected_sha256=packaged['source_zip_sha256'])
    assert dumps_index(rebuilt) == INDEX_PATH.read_bytes()
