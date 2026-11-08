"""Stdlib Mapbox Vector Tile (v2) decoder for official Reinfolib PBF tiles.

Decodes protobuf varint/zigzag, the MoveTo/LineTo/ClosePath geometry command stream and
the per-layer extent into WGS84 GeoJSON-like features for the requested z/x/y. Output
coordinates are quantised to the tile grid (extent, usually 4096) and include the
server's clip buffer; they are a decoding of the raw bytes, not the GeoJSON response.

Specification: https://github.com/mapbox/vector-tile-spec/tree/master/2.1
Every malformed input is a fixed ``invalid_response/bad_pbf``; budgets bound work.
"""
import math
import struct

from .storage import CoreError

MAX_FEATURES = 50_000
MAX_GEOMETRY_INTS = 4_000_000


def _bad():
    return CoreError('invalid_response', 'bad_pbf')


def _varint(buf, pos):
    result = shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise _bad()
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
    raise _bad()


def _fields(buf):
    """Yield (field, wire, value) where value is int or bytes. Unknown fields are allowed."""
    pos, end = 0, len(buf)
    while pos < end:
        tag, pos = _varint(buf, pos)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise _bad()
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 1:
            if pos + 8 > end:
                raise _bad()
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            length, pos = _varint(buf, pos)
            if pos + length > end:
                raise _bad()
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 5:
            if pos + 4 > end:
                raise _bad()
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise _bad()
        yield field, wire, value


def _packed(buf):
    out, pos = [], 0
    while pos < len(buf):
        value, pos = _varint(buf, pos)
        out.append(value)
    return out


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def _text(raw):
    try:
        return bytes(raw).decode('utf-8')
    except UnicodeError:
        raise _bad() from None


def _value(buf):
    result = None
    for field, wire, raw in _fields(buf):
        if field == 1 and wire == 2:
            result = _text(raw)
        elif field == 2 and wire == 5:
            result = struct.unpack('<f', raw)[0]
        elif field == 3 and wire == 1:
            result = struct.unpack('<d', raw)[0]
        elif field == 4 and wire == 0:
            result = raw - (1 << 64) if raw >= 1 << 63 else raw
        elif field == 5 and wire == 0:
            result = raw
        elif field == 6 and wire == 0:
            result = _zigzag(raw)
        elif field == 7 and wire == 0:
            result = bool(raw)
    if isinstance(result, float) and not math.isfinite(result):
        raise _bad()
    return result


def _commands(ints):
    """Return a list of parts (lists of (x, y) tile coordinates); ClosePath marks rings closed."""
    parts, x, y, i = [], 0, 0, 0
    current = None
    while i < len(ints):
        command, count = ints[i] & 7, ints[i] >> 3
        i += 1
        if command in (1, 2):
            if command == 2 and current is None:
                raise _bad()
            if count == 0 or i + 2 * count > len(ints):
                raise _bad()
            for _ in range(count):
                x += _zigzag(ints[i])
                y += _zigzag(ints[i + 1])
                i += 2
                if command == 1:
                    current = {'points': [(x, y)], 'closed': False}
                    parts.append(current)
                else:
                    current['points'].append((x, y))
        elif command == 7:
            if current is None or count != 1:
                raise _bad()
            current['closed'] = True
        else:
            raise _bad()
    return parts


class _Projector:
    def __init__(self, z, x, y, extent):
        self.n, self.x, self.y, self.extent = 2 ** z, x, y, extent

    def __call__(self, point):
        px, py = point
        lon = (self.x + px / self.extent) / self.n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (self.y + py / self.extent) / self.n))))
        return [lon, lat]


def _area(points):
    return sum(points[i][0] * points[i + 1][1] - points[i + 1][0] * points[i][1]
               for i in range(len(points) - 1)) / 2.0


def _geometry(kind, parts, project, stats):
    if kind == 1:
        points = [project(p) for part in parts for p in part['points']]
        if not points:
            raise _bad()
        return {'type': 'Point', 'coordinates': points[0]} if len(points) == 1 else {'type': 'MultiPoint', 'coordinates': points}
    if kind == 2:
        lines = [[project(p) for p in part['points']] for part in parts]
        if not lines or any(len(line) < 2 for line in lines):
            raise _bad()
        return {'type': 'LineString', 'coordinates': lines[0]} if len(lines) == 1 else {'type': 'MultiLineString', 'coordinates': lines}
    if kind == 3:
        polygons = []
        for part in parts:
            if not part['closed'] or len(part['points']) < 3:
                raise _bad()
            ring = part['points'] + [part['points'][0]]
            area = _area(ring)
            if area == 0:
                stats['dropped_degenerate_rings'] += 1
                continue
            coords = [project(p) for p in ring]
            if area > 0:
                polygons.append([coords])
            elif polygons:
                polygons[-1].append(coords)
            else:
                raise _bad()               # interior ring before any exterior ring
        if not polygons:
            return None
        return {'type': 'Polygon', 'coordinates': polygons[0]} if len(polygons) == 1 else {'type': 'MultiPolygon', 'coordinates': polygons}
    return None                            # UNKNOWN geometry type: keep the feature, no geometry


def _layer(buf, z, x, y, stats, max_features):
    name, extent, version = None, 4096, 1
    raw_features, keys, values = [], [], []
    for field, wire, raw in _fields(buf):
        if field == 1 and wire == 2:
            name = _text(raw)
        elif field == 2 and wire == 2:
            raw_features.append(raw)
        elif field == 3 and wire == 2:
            keys.append(_text(raw))
        elif field == 4 and wire == 2:
            values.append(_value(raw))
        elif field == 5 and wire == 0:
            extent = raw
        elif field == 15 and wire == 0:
            version = raw
    if not name or extent <= 0:
        raise _bad()
    project = _Projector(z, x, y, extent)
    features = []
    for raw in raw_features:
        stats['feature_count'] += 1
        if stats['feature_count'] > max_features:
            raise CoreError('response_too_large', 'too_many_features')
        fid, tags, kind, geometry = None, [], 0, []
        for field, wire, item in _fields(raw):
            if field == 1 and wire == 0:
                fid = item
            elif field == 2 and wire == 2:
                tags = _packed(item)
            elif field == 3 and wire == 0:
                kind = item
            elif field == 4 and wire == 2:
                geometry = _packed(item)
        stats['geometry_ints'] += len(geometry)
        if stats['geometry_ints'] > MAX_GEOMETRY_INTS:
            raise CoreError('response_too_large', 'too_many_features')
        if len(tags) % 2:
            raise _bad()
        properties = {}
        for k, v in zip(tags[::2], tags[1::2]):
            if k >= len(keys) or v >= len(values):
                raise _bad()
            properties[keys[k]] = values[v]
        feature = {'type': 'Feature', 'geometry': _geometry(kind, _commands(geometry), project, stats),
                   'properties': properties}
        if fid is not None:
            feature['id'] = fid
        features.append(feature)
    return {'name': name, 'version': version, 'extent': extent, 'features': features}


def decode(payload, z, x, y, *, max_features=MAX_FEATURES):
    """Decode one MVT tile at z/x/y. Raises CoreError(invalid_response, bad_pbf)."""
    stats = {'feature_count': 0, 'geometry_ints': 0, 'dropped_degenerate_rings': 0}
    try:
        layers = [_layer(raw, z, x, y, stats, max_features)
                  for field, wire, raw in _fields(memoryview(bytes(payload))) if field == 3 and wire == 2]
    except (IndexError, struct.error, RecursionError, OverflowError):
        raise _bad() from None
    return {'layers': layers, 'feature_count': stats['feature_count'],
            'dropped_degenerate_rings': stats['dropped_degenerate_rings']}
