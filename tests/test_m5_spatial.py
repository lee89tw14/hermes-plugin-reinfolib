"""M5 slice b: tile math, bounded coverage and point/feature relations (pure stdlib, hand-made fixtures)."""
import math

import pytest

from reinfolib import spatial
from reinfolib.storage import CoreError

LON0, LAT0 = 139.691772, 35.689629          # Tokyo Metropolitan Government (GSI block point)
M_LAT = 1 / 111195.08                       # degrees per metre (spherical, R=6371008.8)
M_LON = M_LAT / math.cos(math.radians(LAT0))


def at(dx, dy, lon=LON0, lat=LAT0):
    """Coordinate dx metres east, dy metres north of (lon, lat)."""
    return [lon + dx * M_LON, lat + dy * M_LAT]


def square(cx, cy, half):
    return [at(cx - half, cy - half), at(cx + half, cy - half), at(cx + half, cy + half),
            at(cx - half, cy + half), at(cx - half, cy - half)]


def feature(geometry, fid=None, **props):
    f = {'type': 'Feature', 'geometry': geometry, 'properties': props}
    if fid is not None:
        f['id'] = fid
    return f


# -- tile math --------------------------------------------------------------------------

def test_tile_index_known_values():
    assert spatial.lonlat_to_tile(LON0, LAT0, 15) == (29099, 12902)
    assert spatial.lonlat_to_tile(LON0, LAT0, 14) == (14549, 6451)
    assert spatial.lonlat_to_tile(-100.0, 60.0, 2) == (0, 1)
    assert spatial.lonlat_to_tile(LON0, LAT0, 0) == (0, 0)
    assert spatial.lonlat_to_tile(180.0, 0.0, 3)[0] == 7          # east edge clamps into the last column


def test_tile_bounds_contain_their_points_and_match_z1_quadrant():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    assert west <= LON0 < east and south < LAT0 <= north
    w, s, e, n = spatial.tile_bounds(1, 0, 0)
    assert (w, s, e) == (-180.0, 0.0, 0.0) and abs(n - 85.0511287798) < 1e-9


@pytest.mark.parametrize('lon,lat', [(LON0, 86.0), (LON0, -86.0), (181.0, 35.0), (float('nan'), 35.0), (True, 35.0), ('139', 35.0)])
def test_bad_coordinates_are_refused(lon, lat):
    with pytest.raises(CoreError) as error:
        spatial.lonlat_to_tile(lon, lat, 15)
    assert (error.value.error_type, error.value.detail) == ('invalid_argument', 'bad_coordinate')


def test_zoom_for_uses_each_endpoint_contract_maximum():
    assert spatial.zoom_for('XKT026') == 15 and spatial.zoom_for('XKT019') == 15
    with pytest.raises(CoreError) as error:
        spatial.zoom_for('XIT001')
    assert error.value.detail == 'unsupported_endpoint'


def test_point_coverage_lists_only_needed_tiles_and_refuses_oversized_bbox():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    centre = ((west + east) / 2, (south + north) / 2)
    assert spatial.tiles_for_point(*centre, 15, 10) == [(29099, 12902)]
    corner = (east - 2 * M_LON, north - 2 * M_LAT)           # 2 m from the NE corner
    assert sorted(spatial.tiles_for_point(*corner, 15, 20)) == [(29099, 12901), (29099, 12902), (29100, 12901), (29100, 12902)]
    with pytest.raises(CoreError) as error:
        spatial.tiles_for_point(*centre, 15, 5000)
    assert (error.value.error_type, error.value.detail) == ('invalid_argument', 'too_many_tiles')


def test_bbox_tiles_counts_before_listing():
    assert spatial.bbox_tiles((LON0, LAT0, LON0, LAT0), 15, max_tiles=1) == [(29099, 12902)]
    with pytest.raises(CoreError) as error:
        spatial.bbox_tiles((122.0, 20.0, 154.0, 46.0), 15, max_tiles=16)
    assert error.value.detail == 'too_many_tiles'
    with pytest.raises(CoreError) as error:
        spatial.bbox_tiles((LON0 + 1, LAT0, LON0, LAT0), 15, max_tiles=4)
    assert error.value.detail == 'bad_bbox'


# -- polygon relations ------------------------------------------------------------------

def relate(geometry, tol=0.0, clip=None):
    return spatial.relate_geometry((LON0, LAT0), geometry, tol, clip_bounds=clip)


def test_point_inside_square_reports_inside_and_boundary_distance():
    result = relate({'type': 'Polygon', 'coordinates': [square(0, 0, 50)]}, tol=10)
    assert result['relation'] == 'inside' and result['contains'] is True
    assert abs(result['distance_m'] - 50) < 0.5


def test_point_outside_square():
    result = relate({'type': 'Polygon', 'coordinates': [square(200, 0, 50)]}, tol=10)
    assert result['relation'] == 'outside' and result['contains'] is False
    assert abs(result['distance_m'] - 150) < 0.5


def test_point_in_hole_is_outside_and_hole_edge_counts_as_boundary():
    poly = {'type': 'Polygon', 'coordinates': [square(0, 0, 100), square(0, 0, 20)]}
    result = relate(poly, tol=5)
    assert result['relation'] == 'outside' and result['contains'] is False
    assert abs(result['distance_m'] - 20) < 0.5
    assert relate(poly, tol=25)['relation'] == 'within_tolerance'


def test_boundary_exact_point_is_within_tolerance_even_with_zero_tolerance():
    edge = {'type': 'Polygon', 'coordinates': [[at(0, -50), at(100, -50), at(100, 50), at(0, 50), at(0, -50)]]}
    result = relate(edge, tol=0)
    assert result['relation'] == 'within_tolerance' and result['distance_m'] < 1e-6
    vertex = {'type': 'Polygon', 'coordinates': [[at(0, 0), at(100, 0), at(100, 100), at(0, 100), at(0, 0)]]}
    assert relate(vertex, tol=0)['relation'] == 'within_tolerance'


def test_tolerance_band_inside_near_edge():
    poly = {'type': 'Polygon', 'coordinates': [square(45, 0, 50)]}           # edge 5 m west of point
    assert relate(poly, tol=10)['relation'] == 'within_tolerance'
    assert relate(poly, tol=1)['relation'] == 'inside'


def test_concave_polygon_notch_is_outside():
    u_shape = [at(-60, -60), at(60, -60), at(60, 60), at(30, 60), at(30, -30), at(-30, -30), at(-30, 60),
               at(-60, 60), at(-60, -60)]
    result = relate({'type': 'Polygon', 'coordinates': [u_shape]}, tol=5)
    assert result['relation'] == 'outside' and abs(result['distance_m'] - 30) < 0.5


def test_multipolygon_any_part_contains():
    multi = {'type': 'MultiPolygon', 'coordinates': [[square(500, 500, 50)], [square(0, 0, 40)]]}
    result = relate(multi, tol=1)
    assert result['relation'] == 'inside' and abs(result['distance_m'] - 40) < 0.5


def test_ring_orientation_does_not_matter():
    clockwise = list(reversed(square(0, 0, 50)))
    assert relate({'type': 'Polygon', 'coordinates': [clockwise]}, tol=1)['relation'] == 'inside'


def test_clipped_tile_edge_is_not_treated_as_a_real_boundary():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    lon, lat = east - 3 * M_LON, (south + north) / 2                     # 3 m west of the tile's east edge
    clipped = {'type': 'Polygon', 'coordinates': [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}
    plain = spatial.relate_geometry((lon, lat), clipped, 10)
    aware = spatial.relate_geometry((lon, lat), clipped, 10, clip_bounds=(west, south, east, north))
    assert plain['relation'] == 'within_tolerance'
    assert aware['relation'] == 'inside' and aware['clip_edges_ignored'] > 0


# -- lines and points -------------------------------------------------------------------

def test_line_distance_never_inside():
    line = {'type': 'LineString', 'coordinates': [at(-100, 30), at(100, 30)]}
    near = relate(line, tol=50)
    assert near['relation'] == 'within_tolerance' and abs(near['distance_m'] - 30) < 0.5
    assert relate(line, tol=10)['relation'] == 'outside'
    assert 'contains' not in near or near['contains'] is None


def test_multilinestring_nearest_part_and_segment_endpoint():
    multi = {'type': 'MultiLineString', 'coordinates': [[at(500, 0), at(600, 0)], [at(40, 0), at(40, 300)]]}
    assert abs(relate(multi, tol=0)['distance_m'] - 40) < 0.5


def test_point_and_multipoint_distance():
    assert abs(relate({'type': 'Point', 'coordinates': at(0, 25)}, tol=30)['distance_m'] - 25) < 0.5
    assert relate({'type': 'MultiPoint', 'coordinates': [at(0, 300), at(10, 0)]}, tol=15)['relation'] == 'within_tolerance'


def test_geometry_collection_takes_strongest_member():
    collection = {'type': 'GeometryCollection', 'geometries': [
        {'type': 'LineString', 'coordinates': [at(-100, 30), at(100, 30)]},
        {'type': 'Polygon', 'coordinates': [square(0, 0, 10)]}]}
    assert relate(collection, tol=1)['relation'] == 'inside'


@pytest.mark.parametrize('geometry', [
    {'type': 'Polygon', 'coordinates': [[at(0, 0), at(1, 1), at(0, 0)]]},
    {'type': 'Polygon', 'coordinates': [[['a', 1], [2, 2], [3, 3], ['a', 1]]]},
    {'type': 'LineString', 'coordinates': [[float('inf'), 1], [2, 2]]},
    {'type': 'Point', 'coordinates': [True, 35.0]},
    {'type': 'Polygon', 'coordinates': 'x'},
])
def test_malformed_geometry_is_fixed_invalid_response(geometry):
    with pytest.raises(CoreError) as error:
        relate(geometry)
    assert (error.value.error_type, error.value.detail) == ('invalid_response', 'bad_geometry')


def test_null_and_unknown_geometry_are_reported_not_guessed():
    assert relate(None)['relation'] == 'no_geometry'
    assert relate({'type': 'Circle', 'coordinates': []})['relation'] == 'unsupported_geometry'


# -- fragments across tiles -------------------------------------------------------------

def tile(features, x=29099, y=12902):
    return {'z': 15, 'x': x, 'y': y, 'features': features}


def test_layer_relation_merges_fragments_only_by_real_feature_id():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    left = feature({'type': 'Polygon', 'coordinates': [square(0, 0, 30)]}, fid=7, name='A')
    right = feature({'type': 'Polygon', 'coordinates': [square(0, 0, 30)]}, fid=7, name='A')
    merged = spatial.relate_layer((LON0, LAT0), [tile([left]), tile([right], x=29100)], 5)
    assert merged['relation'] == 'inside' and merged['matched_groups'][0]['fragment_count'] == 2
    assert merged['matched_groups'][0]['merge_basis'] == 'feature_id'
    anonymous = [feature({'type': 'Polygon', 'coordinates': [square(0, 0, 30)]}, name='A')]
    split = spatial.relate_layer((LON0, LAT0), [tile(anonymous), tile(anonymous, x=29100)], 5)
    assert [g['fragment_count'] for g in split['matched_groups']] == [1, 1]
    assert {g['merge_basis'] for g in split['matched_groups']} == {'none_reported_as_fragment'}
    assert 'fragments_not_merged_without_feature_id' in split['warnings']
    assert split['matched_fragment_n'] == 2


def test_layer_relation_ranks_and_reports_empty_tiles():
    near_line = feature({'type': 'LineString', 'coordinates': [at(-100, 5), at(100, 5)]})
    far = feature({'type': 'Polygon', 'coordinates': [square(500, 0, 20)]})
    result = spatial.relate_layer((LON0, LAT0), [tile([far, near_line])], 10)
    assert result['relation'] == 'within_tolerance' and result['feature_fragment_n'] == 2
    assert result['outside_fragment_n'] == 1
    assert spatial.relate_layer((LON0, LAT0), [tile([])], 10)['relation'] == 'no_features_in_tile'
    assert spatial.relate_layer((LON0, LAT0), [tile([far])], 10)['relation'] == 'outside'


def test_layer_relation_bounds_reported_matches():
    many = [feature({'type': 'Polygon', 'coordinates': [square(0, 0, 30 + i)]}) for i in range(30)]
    result = spatial.relate_layer((LON0, LAT0), [tile(many)], 1, max_reported=20)
    assert result['matched_fragment_n'] == 30 and len(result['matched_groups']) == 20
    assert result['matched_truncated'] is True


# -- refinements learned from real tiles (real-tile findings) --

def test_clip_edges_in_the_server_buffer_outside_the_tile_are_ignored():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    pad_x, pad_y = 5 * (east - west) / 4096, 5 * (north - south) / 4096          # ~5 px buffer seen live
    w, s, e, n = west - pad_x, south - pad_y, east + pad_x, north + pad_y
    clipped = {'type': 'Polygon', 'coordinates': [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    lon, lat = east - 3 * M_LON, (south + north) / 2
    aware = spatial.relate_geometry((lon, lat), clipped, 10, clip_bounds=(west, south, east, north))
    assert aware['relation'] == 'inside' and aware['clip_edges_ignored'] == 4


def test_axis_aligned_real_edge_well_inside_the_tile_is_kept():
    west, south, east, north = spatial.tile_bounds(15, 29099, 12902)
    mid = (west + east) / 2
    poly = {'type': 'Polygon', 'coordinates': [[[west, south], [mid, south], [mid, north], [west, north], [west, south]]]}
    result = spatial.relate_geometry((mid - 2 * M_LON, (south + north) / 2), poly, 10, clip_bounds=(west, south, east, north))
    assert result['relation'] == 'within_tolerance' and result['clip_edges_ignored'] == 3


def test_fragments_merge_by_source_document_id_property():
    same = [feature({'type': 'Polygon', 'coordinates': [square(0, 0, 30)]}, _id='YqXk', name='A')]
    merged = spatial.relate_layer((LON0, LAT0), [tile(same), tile(same, x=29100)], 5)
    (group,) = merged['matched_groups']
    assert group['fragment_count'] == 2 and group['merge_basis'] == 'properties._id'
    assert group['feature_id'] == 'YqXk' and merged['warnings'] == []


TILE_DIR = __import__('os').environ.get('REINFOLIB_M5_TILE_DIR')


@pytest.mark.skipif(not TILE_DIR, reason='set REINFOLIB_M5_TILE_DIR to saved real tiles (kept outside Git)')
def test_real_adjacent_xkt001_tiles_merge_and_ignore_clip_box_edges():
    import json
    from pathlib import Path
    tiles = []
    for x in (14626, 14627):
        raw = json.loads((Path(TILE_DIR) / f'XKT001-14-{x}-6020.geojson').read_bytes())
        tiles.append({'z': 14, 'x': x, 'y': 6020, 'features': raw['features']})
    west, south, east, north = spatial.tile_bounds(14, 14626, 6020)
    point = (east - 1 * M_LON, (south + north) / 2)                 # 1 m west of the shared tile edge
    result = spatial.relate_layer(point, tiles, 10)
    assert result['feature_fragment_n'] == 6 and result['relation'] == 'inside'
    assert all(g['merge_basis'] == 'properties._id' for g in result['matched_groups'])
    assert any(g['fragment_count'] == 2 for g in result['matched_groups'])
    assert result['warnings'] == []
