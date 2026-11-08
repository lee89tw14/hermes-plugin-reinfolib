"""N02 station directory: official 国土数値情報 鉄道データ -> station name -> N02_005g candidates.

The packaged index (data/n02-stations.json) is derived from one official ZIP whose SHA-256 is
recorded in the index. build_index() is the only way to produce it (see
scripts/build_station_index.py). Lookup is local, needs no key and never picks a candidate.
"""
import functools
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from pathlib import Path

INDEX_PATH = Path(__file__).resolve().parent / 'data' / 'n02-stations.json'
MEMBER_CAP = 64_000_000
COLUMNS = ['group_code', 'station_code', 'name', 'operator', 'line']
LICENSE = ('国土数値情報ダウンロードサイトコンテンツ利用規約（政府標準利用規約準拠版）; '
           'N02 data from 2020 onward is open data compatible with CC BY 4.0')
LICENSE_URL = 'https://nlftp.mlit.go.jp/ksj/other/agreement_01.html'


class StationIndexError(Exception):
    def __init__(self, error_type, detail):
        super().__init__(detail)
        self.error_type, self.detail = error_type, detail


def _pages(dataset):
    match = re.fullmatch(r'N02-([0-9]{2})', dataset)
    if not match:
        raise StationIndexError('invalid_argument', 'bad_dataset')
    year = '20' + match[1]
    return (f'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N02-{year}.html',
            f'https://nlftp.mlit.go.jp/ksj/gml/data/N02/{dataset}/{dataset}_GML.zip')


def build_index(zip_bytes, *, dataset, expected_sha256):
    """Verify the official ZIP hash first, then keep only name/operator/line/codes."""
    page, source_url = _pages(dataset)
    digest = hashlib.sha256(zip_bytes).hexdigest()
    if digest != expected_sha256:
        raise StationIndexError('invalid_source', 'sha256_mismatch')
    member = f'{dataset}_GML/UTF-8/{dataset}_Station.geojson'
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            try:
                info = archive.getinfo(member)
            except KeyError:
                raise StationIndexError('invalid_source', 'station_member_missing') from None
            if info.file_size > MEMBER_CAP:
                raise StationIndexError('invalid_source', 'station_member_too_large')
            with archive.open(info) as handle:
                raw = handle.read(MEMBER_CAP + 1)
    except zipfile.BadZipFile:
        raise StationIndexError('invalid_source', 'bad_zip') from None
    if len(raw) > MEMBER_CAP:
        raise StationIndexError('invalid_source', 'station_member_too_large')
    try:
        features = json.loads(raw.decode('utf-8'))['features']
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise StationIndexError('invalid_source', 'bad_geojson') from None
    rows = set()
    count = 0
    for item in features if isinstance(features, list) else [None]:
        props = item.get('properties') if isinstance(item, dict) else None
        if not isinstance(props, dict):
            raise StationIndexError('invalid_source', 'bad_station_record')
        row = tuple(props.get(k) for k in ('N02_005g', 'N02_005c', 'N02_005', 'N02_004', 'N02_003'))
        if (not all(isinstance(v, str) and v and len(v) <= 100 for v in row)
                or not re.fullmatch(r'[0-9]{6}', row[0]) or not re.fullmatch(r'[0-9]{6}', row[1])):
            raise StationIndexError('invalid_source', 'bad_station_record')
        rows.add(row)
        count += 1
    ordered = sorted(rows)
    return {'schema_version': 1, 'dataset': dataset, 'dataset_page': page, 'source_url': source_url,
            'source_zip_sha256': digest, 'source_member': member,
            'source_member_sha256': hashlib.sha256(raw).hexdigest(),
            'license': LICENSE, 'license_url': LICENSE_URL,
            'attribution': f'「国土数値情報（鉄道データ）」（国土交通省）（{page}）を加工して作成',
            'processing': ('Station name, operator, line, station code (N02_005c) and group code (N02_005g) '
                           'extracted; geometry removed; duplicate rows merged and sorted.'),
            'station_count': count, 'group_count': len({row[0] for row in ordered}),
            'columns': COLUMNS, 'rows': [list(row) for row in ordered]}


def dumps_index(index) -> bytes:
    """Deterministic encoding, one station row per line so updates diff readably."""
    head = {k: v for k, v in index.items() if k != 'rows'}
    text = json.dumps(head, ensure_ascii=False)[:-1] + ', "rows": [\n'
    text += ',\n'.join(json.dumps(row, ensure_ascii=False) for row in index['rows'])
    return (text + '\n]}\n').encode('utf-8')


@functools.lru_cache(maxsize=1)
def _packaged():
    try:
        index = json.loads(INDEX_PATH.read_text(encoding='utf-8'))
        if index.get('schema_version') != 1 or index.get('columns') != COLUMNS or not index.get('rows'):
            raise ValueError('index')
        return index
    except (OSError, ValueError, UnicodeError):
        raise StationIndexError('internal', 'station_index_unavailable') from None


def load_index():
    return _packaged()


def _norm(text):
    text = unicodedata.normalize('NFKC', text).strip()
    return text[:-1] if len(text) > 1 and text.endswith('駅') else text


def lookup(name=None, *, code=None, index=None, limit=20):
    """Exact (NFKC, trailing 駅 ignored) name matches first; substring only when none is exact."""
    if (name is None) == (code is None):
        raise StationIndexError('invalid_argument', 'name_or_code_required')
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 50):
        raise StationIndexError('invalid_argument', 'bad_name')
    if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code)):
        raise StationIndexError('invalid_argument', 'bad_code')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise StationIndexError('invalid_argument', 'bad_limit')
    index = load_index() if index is None else index
    groups = {}
    for group, station, station_name, operator, line in index['rows']:
        entry = groups.setdefault((group, station_name), [])
        entry.append({'operator': operator, 'line': line, 'station_code': station})
    partial_count = 0
    if code is not None:
        keys, match_type = [k for k in groups if k[0] == code], 'code'
    else:
        needle = _norm(name)
        exact = [k for k in groups if unicodedata.normalize('NFKC', k[1]) == needle]
        partial = [k for k in groups if k not in exact and needle in unicodedata.normalize('NFKC', k[1])]
        keys, match_type = (exact, 'exact') if exact else (partial, 'partial')
        partial_count = len(partial) if exact else 0
    keys.sort()
    candidates = [{'group_code': group, 'station_name': station_name,
                   'lines': sorted(groups[(group, station_name)], key=lambda l: (l['line'], l['operator'], l['station_code']))}
                  for group, station_name in keys]
    result = {'ok': True, 'dataset': index['dataset'], 'source_zip_sha256': index['source_zip_sha256'],
              'match_type': match_type,
              'match_status': 'no_match' if not keys else 'unique' if len(keys) == 1 else 'ambiguous',
              'candidate_count': len(candidates), 'truncated': len(candidates) > limit,
              'candidates': candidates[:limit],
              'attribution': index['attribution'], 'license': index['license'], 'license_url': index['license_url']}
    if code is None:
        result.update(query_name=name, partial_match_count=partial_count)
    else:
        result['query_code'] = code
    return result
