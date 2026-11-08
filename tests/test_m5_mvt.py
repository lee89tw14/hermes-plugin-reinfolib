"""M5 slice d: stdlib Mapbox Vector Tile decoder (hand-encoded fixtures; real tiles opt-in)."""
import json
import math
import os
import struct
from pathlib import Path

import pytest

from reinfolib import mvt, spatial
from reinfolib.storage import CoreError


# -- tiny protobuf encoder (test-only) --------------------------------------------------

def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def key(num, wire):
    return varint(num << 3 | wire)


def ld(num, payload):
    return key(num, 2) + varint(len(payload)) + payload


def vi(num, value):
    return key(num, 0) + varint(value)


def packed(num, values):
    return ld(num, b''.join(varint(v) for v in values))


def zz(n):
    return (n << 1) ^ (n >> 63)


def cmd(c, count):
    return c | count << 3


def value(kind, v):
    encoders = {'s': lambda: ld(1, v.encode()), 'f': lambda: key(2, 5) + struct.pack('<f', v),
                'd': lambda: key(3, 1) + struct.pack('<d', v), 'i': lambda: vi(4, v & (2 ** 64 - 1)),
                'u': lambda: vi(5, v), 'z': lambda: vi(6, zz(v)), 'b': lambda: vi(7, int(v))}
    return encoders[kind]()


def feature(geom_type, geometry, tags=(), fid=None):
    body = (vi(1, fid) if fid is not None else b'') + (packed(2, tags) if tags else b'') + vi(3, geom_type) + packed(4, geometry)
    return ld(2, body)


def layer(features, keys=('name', 'n'), values=(('s', 'A'), ('i', -5)), extent=4096, name='test', extra=b''):
    body = vi(15, 2) + ld(1, name.encode()) + b''.join(features)
    body += b''.join(ld(3, k.encode()) for k in keys) + b''.join(ld(4, value(*v)) for v in values)
    return ld(3, body + vi(5, extent) + extra)


Z, X, Y = 15, 29099, 12902
WEST, SOUTH, EAST, NORTH = spatial.tile_bounds(Z, X, Y)

POINT = [cmd(1, 1), zz(2048), zz(2048)]
LINE = [cmd(1, 1), zz(0), zz(0), cmd(2, 2), zz(4096), zz(0), zz(-4096), zz(4096)]
SQUARE = [cmd(1, 1), zz(0), zz(0), cmd(2, 3), zz(4096), zz(0), zz(0), zz(4096), zz(-4096), zz(0), cmd(7, 1)]
HOLE = [cmd(1, 1), zz(1024), zz(-3072), cmd(2, 3), zz(0), zz(2048), zz(2048), zz(0), zz(0), zz(-2048), cmd(7, 1)]


def decode(blob):
    return mvt.decode(blob, Z, X, Y)


def test_point_decodes_to_tile_centre_with_properties_and_id():
    tile = decode(layer([feature(1, POINT, tags=(0, 0, 1, 1), fid=42)]))
    (lyr,) = tile['layers']
    assert lyr['name'] == 'test' and lyr['extent'] == 4096 and lyr['version'] == 2
    (f,) = lyr['features']
    assert f['type'] == 'Feature' and f['id'] == 42
    assert f['properties'] == {'name': 'A', 'n': -5}
    lon, lat = f['geometry']['coordinates']
    centre_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (Y + 0.5) / 2 ** Z))))
    assert f['geometry']['type'] == 'Point'
    assert abs(lon - (WEST + EAST) / 2) < 1e-12 and abs(lat - centre_lat) < 1e-12


def test_line_and_multiline_use_tile_corners():
    f = decode(layer([feature(2, LINE)]))['layers'][0]['features'][0]
    coords = f['geometry']['coordinates']
    assert f['geometry']['type'] == 'LineString' and len(coords) == 3
    assert abs(coords[0][0] - WEST) < 1e-12 and abs(coords[0][1] - NORTH) < 1e-12
    assert abs(coords[1][0] - EAST) < 1e-12 and abs(coords[2][1] - SOUTH) < 1e-12
    two = decode(layer([feature(2, LINE + [cmd(1, 1), zz(10), zz(10), cmd(2, 1), zz(5), zz(5)])]))
    assert two['layers'][0]['features'][0]['geometry']['type'] == 'MultiLineString'


def test_polygon_winding_assigns_holes_and_splits_multipolygons():
    poly = decode(layer([feature(3, SQUARE + HOLE)]))['layers'][0]['features'][0]['geometry']
    assert poly['type'] == 'Polygon' and len(poly['coordinates']) == 2
    assert poly['coordinates'][0][0] == poly['coordinates'][0][-1]
    second = [cmd(1, 1), zz(-4096), zz(-4096), cmd(2, 3), zz(100), zz(0), zz(0), zz(100), zz(-100), zz(0), cmd(7, 1)]
    multi = decode(layer([feature(3, SQUARE + second)]))['layers'][0]['features'][0]['geometry']
    assert multi['type'] == 'MultiPolygon' and len(multi['coordinates']) == 2
    centre = ((WEST + EAST) / 2, (SOUTH + NORTH) / 2)
    assert spatial.relate_geometry(centre, poly, 0)['relation'] == 'outside'          # centre lies in the hole


def test_multipoint_and_all_value_types():
    vals = (('s', 'x'), ('f', 2.5), ('d', 1.25), ('i', -7), ('u', 7), ('z', -3), ('b', True))
    keys = ('s', 'f', 'd', 'i', 'u', 'z', 'b')
    tags = [t for i in range(7) for t in (i, i)]
    blob = layer([feature(1, [cmd(1, 2), zz(0), zz(0), zz(10), zz(10)], tags=tags)], keys=keys, values=vals)
    f = decode(blob)['layers'][0]['features'][0]
    assert f['geometry']['type'] == 'MultiPoint'
    assert f['properties'] == {'s': 'x', 'f': 2.5, 'd': 1.25, 'i': -7, 'u': 7, 'z': -3, 'b': True}


def test_unknown_fields_are_skipped_and_empty_tile_is_empty():
    unknown = vi(99, 1) + ld(98, b'zz') + key(97, 5) + b'\0\0\0\0' + key(96, 1) + b'\0' * 8
    tile = decode(layer([feature(1, POINT)], extra=unknown))
    assert len(tile['layers'][0]['features']) == 1
    assert decode(b'') == {'layers': [], 'feature_count': 0, 'dropped_degenerate_rings': 0}


def test_zero_area_ring_is_dropped_and_counted():
    flat = [cmd(1, 1), zz(0), zz(0), cmd(2, 2), zz(10), zz(0), zz(10), zz(0), cmd(7, 1)]
    tile = decode(layer([feature(3, SQUARE + flat)]))
    assert tile['dropped_degenerate_rings'] == 1
    assert tile['layers'][0]['features'][0]['geometry']['type'] == 'Polygon'


@pytest.mark.parametrize('blob', [
    b'\x1a\x05\x0a',                                    # truncated length-delimited layer
    b'\x1a' + b'\xff' * 11,                             # overlong varint
    key(3, 3) + b'\x00',                                # group wire type
    layer([feature(1, POINT, tags=(0, 5))]),            # value index out of range
    layer([feature(1, POINT, tags=(0,))]),              # odd tag list
    layer([feature(2, [cmd(2, 1), zz(1), zz(1)])]),     # LineTo before MoveTo
    layer([feature(1, [cmd(4, 1), zz(1), zz(1)])]),     # unknown command
    layer([feature(1, [cmd(1, 3), zz(1), zz(1)])]),     # count exceeds parameters
    layer([feature(3, [cmd(1, 1), zz(0), zz(0), cmd(2, 1), zz(1), zz(1)])]),  # unclosed polygon ring
    layer([feature(1, POINT)], extent=0),
    layer([feature(1, POINT)], name=''),
])
def test_malformed_tiles_are_fixed_invalid_response(blob):
    with pytest.raises(CoreError) as error:
        decode(blob)
    assert (error.value.error_type, error.value.detail) == ('invalid_response', 'bad_pbf')


def test_feature_budget_is_enforced():
    with pytest.raises(CoreError) as error:
        mvt.decode(layer([feature(1, POINT)] * 3), Z, X, Y, max_features=2)
    assert (error.value.error_type, error.value.detail) == ('response_too_large', 'too_many_features')


# -- real tiles (opt-in; raw bytes are kept outside Git) --------------------------------

TILE_DIR = os.environ.get('REINFOLIB_M5_TILE_DIR')


def _pairs():
    if not TILE_DIR:
        return []
    return sorted(p.with_suffix('') for p in Path(TILE_DIR).glob('*.pbf') if p.with_suffix('.geojson').exists())


def _paths(geometry):
    kind, c = geometry['type'], geometry['coordinates']
    if kind == 'Point':
        return [[c]]
    if kind in ('MultiPoint',):
        return [[q] for q in c]
    if kind == 'LineString':
        return [c]
    if kind == 'MultiPolygon':
        return [ring for poly in c for ring in poly]
    return c                                   # MultiLineString / Polygon


def _gap(p, paths):
    """Shortest planar distance (degrees) from vertex p to any segment/point of paths."""
    best = math.inf
    for path in paths:
        pairs = zip(path, path[1:]) if len(path) > 1 else [(path[0], path[0])]
        for a, b in pairs:
            ax, ay, bx, by = a[0] - p[0], a[1] - p[1], b[0] - p[0], b[1] - p[1]
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = 0 if length2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / length2))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


@pytest.mark.skipif(not TILE_DIR, reason='set REINFOLIB_M5_TILE_DIR to real GeoJSON/PBF pairs (kept outside Git)')
@pytest.mark.parametrize('stem', _pairs(), ids=lambda p: p.name)
def test_decoded_real_pbf_matches_geojson_of_same_tile(stem):
    """Every decoded vertex lies on the GeoJSON geometry (<=0.5 px); GeoJSON vertices lie within
    6 px of the decoded geometry because the PBF is a simplified encoding (measured max 4.9 px)."""
    endpoint, z, x, y = stem.name.split('-')
    z, x, y = int(z), int(x), int(y)
    geo = json.loads(stem.with_suffix('.geojson').read_bytes())
    tile = mvt.decode(stem.with_suffix('.pbf').read_bytes(), z, x, y)
    decoded = [f for lyr in tile['layers'] for f in lyr['features']]
    assert len(decoded) == len(geo['features']) and tile['dropped_degenerate_rings'] == 0
    by_id = {f['properties']['_id']: f for f in decoded}
    assert len(by_id) == len(decoded)
    west, south, east, north = spatial.tile_bounds(z, x, y)
    pixel = (east - west) / tile['layers'][0]['extent']
    for expected in geo['features']:
        got = by_id[expected['properties']['_id']]
        assert {k: str(v) for k, v in got['properties'].items()} == {k: str(v) for k, v in expected['properties'].items() if v is not None}
        assert got['geometry']['type'] == expected['geometry']['type']
        want, have = _paths(expected['geometry']), _paths(got['geometry'])
        assert max(_gap(p, want) for path in have for p in path) <= 0.5 * pixel
        assert max(_gap(p, have) for path in want for p in path) <= 6 * pixel
