"""P2-3: range filters separate unknown values from known non-matches."""
from pathlib import Path
import json
import pytest
from reinfolib import views


def snap(rows):
    return {'schema_version': 1, 'snapshot_id': 'a' * 32, 'query': {}, 'fetched_at': '2026-11-08T00:00:00Z',
            'upstream_count': len(rows), 'records': rows}


@pytest.mark.parametrize('field,column,good,bad_value,bounded,filter_value', [
    ('area', 'Area', '50', '5', '2000㎡以上', '10'),
    ('price', 'TradePrice', '50000000', '5', '1億円以上', '10'),
    ('building_year', 'BuildingYear', '2010年', '1990年', '戦前', 2000),
])
def test_page_reports_range_exclusion_reasons(field, column, good, bad_value, bounded, filter_value):
    rows = [{column: good}, {column: bad_value}, {column: bounded}, {column: ''}, {}]
    p = views.page(snap(rows), filters={field + '_min': filter_value})
    assert p['matched_count'] == 1 and p['filtered_out_n'] == 4
    assert p['filter_diagnostics'] == {field: {'did_not_match_n': 1, 'unparseable_n': 1, 'missing_n': 2}}
    assert 'unknown_values_excluded_from_range_filters' in p['warnings']


def test_export_manifest_uses_same_range_diagnostics(tmp_path):
    rows = [{'Area': '50'}, {'Area': '5'}, {'Area': '2000㎡以上'}, {'Area': ''}, {}]
    s = snap(rows)
    p = views.page(s, filters={'area_min': '10'})
    e = views.export(s, tmp_path, format='json', filters={'area_min': '10'})
    meta = json.loads(Path(e['manifest_path']).read_text())
    assert meta['matched_count'] == p['matched_count'] == e['count'] == 1
    assert meta['filtered_out_n'] == p['filtered_out_n'] == 4
    assert meta['filter_diagnostics'] == p['filter_diagnostics']
    assert 'unknown_values_excluded_from_range_filters' in meta['warnings']


def test_record_failing_multiple_ranges_counts_each_field_but_one_exclusion():
    p = views.page(snap([{'Area': '', 'TradePrice': '5'}]),
                   filters={'area_min': '10', 'price_min': '10'})
    assert p['filtered_out_n'] == 1
    assert p['filter_diagnostics'] == {'area': {'did_not_match_n': 0, 'unparseable_n': 0, 'missing_n': 1},
                                       'price': {'did_not_match_n': 1, 'unparseable_n': 0, 'missing_n': 0}}
