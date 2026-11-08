"""Pure-stdlib tile math and point/feature relations for single Japan points.

Distances use a local equirectangular projection centred on the query point (spherical
radius 6,371,008.8 m). Over the few hundred metres this module is allowed to cover
the error is far below the positional uncertainty of a geocoded address point.

A tile's GeoJSON may be clipped to the tile, so a fragment edge lying exactly on the
tile border is an artefact of tiling, not a real boundary. Such edges are ignored for
boundary distance when ``clip_bounds`` is given. Fragments from different tiles are
merged only when the feature carries a real GeoJSON ``id``; otherwise each fragment is
reported separately (attribute equality is not identity).
"""
import math

from .storage import CoreError

EARTH_RADIUS_M = 6_371_008.8
MAX_LAT = 85.0511287798
CLIP_EPS_DEG = 1e-6          # ~0.1 m; GeoJSON coordinate rounding on tile borders
# Official tiles are clipped about 5/4096 of a tile outside its bounds (measured live,
# real-tile findings). Axis-aligned edges on or up to 16/4096
# outside a bound are treated as clip artefacts; edges inside the tile are always kept.
CLIP_BUFFER_FRACTION = 16 / 4096
MAX_DEPTH = 4
RANK = {'inside': 3, 'within_tolerance': 2, 'outside': 1}


def _is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check_point(lon, lat):
    if not (_is_number(lon) and _is_number(lat)) or not (-180 <= lon <= 180 and -MAX_LAT <= lat <= MAX_LAT):
        raise CoreError('invalid_argument', 'bad_coordinate')


def lonlat_to_tile(lon, lat, z):
    _check_point(lon, lat)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(z, x, y):
    """(west, south, east, north) in degrees."""
    n = 2 ** z

    def lat(row):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))
    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def zoom_for(endpoint):
    from .layers import CONTRACTS
    if endpoint not in CONTRACTS:
        raise CoreError('invalid_argument', 'unsupported_endpoint')
    return CONTRACTS[endpoint]['zoom'][1]


def bbox_tiles(bbox, z, *, max_tiles):
    west, south, east, north = bbox
    _check_point(west, south)
    _check_point(east, north)
    if west > east or south > north:
        raise CoreError('invalid_argument', 'bad_bbox')
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > max_tiles:
        raise CoreError('invalid_argument', 'too_many_tiles')
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def tiles_for_point(lon, lat, z, radius_m, *, max_tiles=4):
    _check_point(lon, lat)
    if not _is_number(radius_m) or radius_m < 0:
        raise CoreError('invalid_argument', 'bad_tolerance')
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 1e-9)
    bbox = (max(lon - dlon, -180.0), max(lat - dlat, -MAX_LAT), min(lon + dlon, 180.0), min(lat + dlat, MAX_LAT))
    return bbox_tiles(bbox, z, max_tiles=max_tiles)


# -- geometry ----------------------------------------------------------------------------

def _bad():
    return CoreError('invalid_response', 'bad_geometry')


def _position(value):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise _bad()
    lon, lat = value[0], value[1]
    if not (_is_number(lon) and _is_number(lat)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise _bad()
    return float(lon), float(lat)


def _positions(value, minimum):
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        raise _bad()
    return [_position(item) for item in value]


def _ring(value):
    ring = _positions(value, 3)
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) < 4:
        raise _bad()
    return ring


class _Frame:
    def __init__(self, lon0, lat0):
        self.lon0, self.lat0 = lon0, lat0
        self.ky = math.radians(1) * EARTH_RADIUS_M
        self.kx = self.ky * math.cos(math.radians(lat0))

    def xy(self, position):
        return (position[0] - self.lon0) * self.kx, (position[1] - self.lat0) * self.ky


def _segment_distance(a, b):
    """Distance from the origin to segment ab (projected metres)."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def _contains(ring_xy):
    """Even-odd ray cast from the origin."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring_xy, ring_xy[1:]):
        if (y1 > 0) != (y2 > 0):
            if x1 + (0 - y1) * (x2 - x1) / (y2 - y1) > 0:
                inside = not inside
    return inside


def _clip_edge(a, b, clip):
    if clip is None:
        return False
    west, south, east, north = clip
    for i, low, high in ((0, west, east), (1, south, north)):
        if abs(a[i] - b[i]) >= CLIP_EPS_DEG:
            continue
        v = (a[i] + b[i]) / 2
        margin = (high - low) * CLIP_BUFFER_FRACTION + CLIP_EPS_DEG
        if low - margin <= v <= low + CLIP_EPS_DEG or high - CLIP_EPS_DEG <= v <= high + margin:
            return True
    return False


class _Acc:
    def __init__(self):
        self.contains = None
        self.distance = None
        self.clip_ignored = 0

    def add_distance(self, value):
        self.distance = value if self.distance is None else min(self.distance, value)

    def add_contains(self, value):
        self.contains = bool(self.contains) or value


def _edges(acc, frame, positions, clip):
    xy = [frame.xy(p) for p in positions]
    for i in range(len(positions) - 1):
        if _clip_edge(positions[i], positions[i + 1], clip):
            acc.clip_ignored += 1
            continue
        acc.add_distance(_segment_distance(xy[i], xy[i + 1]))
    return xy


def _polygon(acc, frame, rings_value, clip):
    if not isinstance(rings_value, (list, tuple)) or not rings_value:
        raise _bad()
    rings = [_ring(r) for r in rings_value]
    inside = False
    for index, ring in enumerate(rings):
        xy = _edges(acc, frame, ring, clip)
        hit = _contains(xy)
        if index == 0:
            inside = hit
        elif hit:
            inside = False
    acc.add_contains(inside)


def _walk(acc, frame, geometry, clip, depth):
    if depth > MAX_DEPTH:
        raise _bad()
    kind = geometry.get('type')
    coords = geometry.get('coordinates')
    if kind == 'Point':
        acc.add_distance(math.hypot(*frame.xy(_position(coords))))
    elif kind == 'MultiPoint':
        for p in _positions(coords, 1):
            acc.add_distance(math.hypot(*frame.xy(p)))
    elif kind == 'LineString':
        _edges(acc, frame, _positions(coords, 2), None)
    elif kind == 'MultiLineString':
        if not isinstance(coords, (list, tuple)) or not coords:
            raise _bad()
        for line in coords:
            _edges(acc, frame, _positions(line, 2), None)
    elif kind == 'Polygon':
        _polygon(acc, frame, coords, clip)
    elif kind == 'MultiPolygon':
        if not isinstance(coords, (list, tuple)) or not coords:
            raise _bad()
        for polygon in coords:
            _polygon(acc, frame, polygon, clip)
    elif kind == 'GeometryCollection':
        members = geometry.get('geometries')
        if not isinstance(members, list):
            raise _bad()
        for member in members:
            if not isinstance(member, dict):
                raise _bad()
            if not _walk(acc, frame, member, clip, depth + 1):
                return False
    else:
        return False
    return True


def _classify(contains, distance, tolerance):
    if distance is not None and distance <= tolerance:
        return 'within_tolerance'
    if contains:
        return 'inside'
    if distance is None and contains is None:
        return 'undetermined'
    return 'outside'


def relate_geometry(point, geometry, tolerance_m, *, clip_bounds=None):
    """Relation of one point to one GeoJSON geometry with an explicit boundary tolerance (m)."""
    lon, lat = point
    _check_point(lon, lat)
    if not _is_number(tolerance_m) or tolerance_m < 0:
        raise CoreError('invalid_argument', 'bad_tolerance')
    if geometry is None:
        return {'relation': 'no_geometry', 'contains': None, 'distance_m': None, 'geometry_type': None,
                'clip_edges_ignored': 0}
    if not isinstance(geometry, dict):
        raise _bad()
    acc = _Acc()
    try:
        known = _walk(acc, _Frame(lon, lat), geometry, clip_bounds, 0)
    except RecursionError:
        raise _bad() from None
    kind = geometry.get('type') if isinstance(geometry.get('type'), str) else None
    if not known:
        return {'relation': 'unsupported_geometry', 'contains': None, 'distance_m': None,
                'geometry_type': kind, 'clip_edges_ignored': 0}
    return {'relation': _classify(acc.contains, acc.distance, tolerance_m), 'contains': acc.contains,
            'distance_m': None if acc.distance is None else round(acc.distance, 3),
            'geometry_type': kind, 'clip_edges_ignored': acc.clip_ignored}


def _feature_id(feature):
    """(id, basis): a top-level GeoJSON id, else the official properties._id that live tiles
    repeat for the same source feature across adjacent tiles; otherwise (None, None)."""
    value = feature.get('id')
    if (isinstance(value, str) and value) or type(value) is int:
        return value, 'feature_id'
    properties = feature.get('properties')
    value = properties.get('_id') if isinstance(properties, dict) else None
    if isinstance(value, str) and value:
        return value, 'properties._id'
    return None, None


def relate_layer(point, tiles, tolerance_m, *, max_reported=20):
    """Relate a point to every feature fragment of the fetched tiles of one layer.

    ``tiles``: [{'z','x','y','features': [GeoJSON Feature, ...]}]. Empty tiles give
    ``no_features_in_tile`` (never 'safe')."""
    groups, order = {}, []
    counts = {'feature_fragment_n': 0, 'matched_fragment_n': 0, 'outside_fragment_n': 0,
              'undetermined_fragment_n': 0}
    anonymous = False
    for tile in tiles:
        clip = tile_bounds(tile['z'], tile['x'], tile['y'])
        for index, feature in enumerate(tile['features']):
            if not isinstance(feature, dict):
                raise _bad()
            counts['feature_fragment_n'] += 1
            found = relate_geometry(point, feature.get('geometry'), tolerance_m, clip_bounds=clip)
            fid, basis = _feature_id(feature)
            key = (basis, fid) if fid is not None else ('fragment', tile['x'], tile['y'], index)
            anonymous = anonymous or fid is None
            if key not in groups:
                groups[key] = {'feature_id': fid, 'merge_basis': basis or 'none_reported_as_fragment',
                               'fragments': [], 'properties': feature.get('properties')}
                order.append(key)
            groups[key]['fragments'].append({'tile': [tile['z'], tile['x'], tile['y']], **found})
    matched = []
    best = None
    for key in order:
        group = groups[key]
        fragments = group['fragments']
        contains = None
        distance = None
        for f in fragments:
            if f['contains'] is not None:
                contains = bool(contains) or f['contains']
            if f['distance_m'] is not None:
                distance = f['distance_m'] if distance is None else min(distance, f['distance_m'])
        relation = _classify(contains, distance, tolerance_m)
        if all(f['relation'] in ('no_geometry', 'unsupported_geometry') for f in fragments):
            relation = 'undetermined'
        bucket = {'inside': 'matched_fragment_n', 'within_tolerance': 'matched_fragment_n',
                  'outside': 'outside_fragment_n'}.get(relation, 'undetermined_fragment_n')
        counts[bucket] += len(fragments)
        if relation in RANK and (best is None or RANK[relation] > RANK[best]):
            best = relation
        if relation in ('inside', 'within_tolerance'):
            matched.append({'relation': relation, 'contains': contains, 'distance_m': distance,
                            'fragment_count': len(fragments), 'merge_basis': group['merge_basis'],
                            'feature_id': group['feature_id'], 'tiles': [f['tile'] for f in fragments],
                            'properties': group['properties']})
    if counts['feature_fragment_n'] == 0:
        relation = 'no_features_in_tile'
    else:
        relation = best or 'undetermined'
    matched.sort(key=lambda g: (-RANK[g['relation']], g['distance_m'] if g['distance_m'] is not None else math.inf))
    warnings = []
    if anonymous and len(tiles) > 1:
        warnings.append('fragments_not_merged_without_feature_id')
    return {'relation': relation, **counts, 'matched_groups': matched[:max_reported],
            'matched_truncated': len(matched) > max_reported, 'tolerance_m': tolerance_m,
            'tiles': [[t['z'], t['x'], t['y']] for t in tiles], 'warnings': warnings}
