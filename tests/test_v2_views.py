"""New snapshot views: local-only paging, filters, statistics and export."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def views():
    path = ROOT / 'reinfolib' / 'views.py'
    assert path.exists(), 'new snapshot views are not implemented'
    spec = importlib.util.spec_from_file_location('v2_views_test', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(rows):
    return {'schema_version': 1, 'snapshot_id': 'a' * 32,
            'query': {'city': '13104', 'year': 2025, 'quarter': 1,
                      'price_classification': '01', 'language': 'ja'},
            'fetched_at': '2026-11-08T00:00:00Z', 'upstream_count': len(rows),
            'records': rows}


def test_pages_preserve_all_fields_and_duplicate_rows():
    v = views()
    rows = [{'TradePrice': '100', 'UnknownFutureColumn': None}] * 5
    s = snapshot(rows)
    pages = [v.page(s, offset=x, limit=2) for x in (0, 2, 4)]
    assert [p['returned_count'] for p in pages] == [2, 2, 1]
    assert [p['next_offset'] for p in pages] == [2, 4, None]
    assert [p['has_more'] for p in pages] == [True, True, False]
    assert all(p['upstream_count'] == p['matched_count'] == 5 for p in pages)
    assert sum((p['records'] for p in pages), []) == rows
    assert pages[0]['statistics_scope'] == 'all_matched_records'
    assert pages[0]['snapshot_id'] == s['snapshot_id']
    pages[0]['records'][0]['TradePrice'] = '999'
    assert s['records'][0]['TradePrice'] == '100'


def test_filter_before_paging_and_keep_unknown_numeric_values_raw():
    v = views()
    rows = [
        {'DistrictName': '西新宿', 'Type': '中古マンション等', 'FloorPlan': '1LDK',
         'Area': area, 'TradePrice': price, 'BuildingYear': year}
        for area, price, year in [('35.5', '50000000', '平成28年'),
                                 ('80以上', '90000000', '戦前'),
                                 ('', '', ''), ('40', '70000000', '2020年')]
    ]
    s = snapshot(rows)
    p = v.page(s, limit=1, filters={'district_name': '西新宿', 'type': '中古マンション等',
                                  'floor_plan': '1LDK', 'area_min': '30', 'area_max': '40',
                                  'price_max': '60000000', 'building_year_min': 2010})
    assert p['upstream_count'] == 4 and p['matched_count'] == 1
    assert p['records'] == rows[:1]
    assert p['filtered_out_n'] == 3 and p['has_more'] is False
    assert s['records'][1]['Area'] == '80以上'


@pytest.mark.parametrize('kwargs', [
    {'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 101}, {'limit': True},
    {'filters': {'unknown': 'x'}}, {'filters': {'area_min': 'NaN'}},
    {'filters': {'price_max': '-1'}}, {'filters': {'building_year_min': True}},
    {'filters': {'area_min': '80', 'area_max': '20'}},
    {'filters': {'type': ['bad']}}, {'filters': []},
])
def test_bad_view_arguments_rejected(kwargs):
    with pytest.raises(ValueError):
        views().page(snapshot([]), **kwargs)


def test_statistics_use_all_matched_rows_grouped_by_type_source_period():
    rows = [
        {'Type': 'condo', 'PriceCategory': 'survey', 'Period': '2025Q1', 'TradePrice': x}
        for x in ['100', '201', '', None, '200以上']
    ] + [{'Type': 'land', 'PriceCategory': 'contract', 'Period': '2025Q1', 'TradePrice': '999'}]
    p = views().page(snapshot(rows), limit=1)
    assert p['source_counts'] == {'survey': 5, 'contract': 1}
    groups = p['statistics']
    assert len(groups) == 2
    condo = next(g for g in groups if g['type'] == 'condo')
    assert condo['median_trade_price'] == '150.5'
    assert (condo['count'], condo['valid_n'], condo['missing_n'], condo['parse_failed_n']) == (5, 2, 2, 1)
    assert any('duplicate' in w.lower() for w in p['warnings'])
    s = snapshot(rows)
    s['warnings'] = ['unrecognized_period', 'classification_02_available_from_2021']
    assert set(s['warnings']).issubset(views().page(s)['warnings'])


@pytest.mark.parametrize('raw,expected', [('2016年', 2016), ('2016', 2016), ('平成28年', 2016),
    ('令和元年', 2019), ('戦前', None), ('', None), ('昭和99年', None), ('80以上', None)])
def test_building_year_precision(raw, expected):
    assert views().building_year(raw) == expected


def test_export_all_filtered_records_with_provenance_and_csv_safety(tmp_path):
    import json
    import csv
    import hashlib
    v = views()
    s = snapshot([{'DistrictName': '=BAD()', 'TradePrice': '100', 'NewField': None},
                  {'DistrictName': '\t+BAD()', 'TradePrice': '200', 'NewField': {'x': 1}}])
    result = v.export(s, tmp_path / 'exports', format='json')
    path = Path(result['path'])
    exported = json.loads(path.read_text())
    assert exported['records'] == s['records'] and result['count'] == 2
    assert exported['snapshot_id'] == s['snapshot_id']
    assert exported['attribution'] and exported['processing'] and exported['credit']
    assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    csv_result = v.export(s, tmp_path / 'exports', format='csv', filters={'price_min': '150'})
    with Path(csv_result['path']).open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == csv_result['count'] == 1
    assert rows[0]['DistrictName'].startswith("'")
    meta = json.loads(Path(csv_result['manifest_path']).read_text())
    assert meta['matched_count'] == 1 and meta['filters'] == {'price_min': '150'}
    assert s['records'][1]['DistrictName'] == '\t+BAD()'


def test_export_rejects_format_and_symlink_root(tmp_path):
    v = views()
    with pytest.raises(ValueError):
        v.export(snapshot([]), tmp_path / 'exports', format='../bad')
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'exports').symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        v.export(snapshot([]), tmp_path / 'exports', format='json')
