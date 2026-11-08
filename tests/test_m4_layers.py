"""Vertical tile contract tests; endpoint slices added after each green."""
import json
import pytest
from reinfolib import layers
from reinfolib.storage import CoreError

KEY = 'offline-test-credential'
QUERY = {'response_format': 'geojson', 'z': 14, 'x': 14551, 'y': 6450}
RAW = {'type': 'FeatureCollection', 'future': {'raw': True}, 'features': [
    {'type': 'Feature', 'geometry': {'type': 'MultiLineString', 'coordinates': [[[139.1, 35.1], [139.2, 35.2]]]}, 'properties': {'unknown': None}},
    {'type': 'Feature', 'geometry': {'type': 'GeometryCollection', 'geometries': []}, 'properties': {'unknown': 'same tile fragment'}}]}


def test_xkt002_tracer():
    seen = []
    def transport(url, headers, timeout, cap):
        seen.append((url, headers, timeout, cap))
        return 200, {}, json.dumps(RAW).encode()
    result = layers.fetch('XKT002', QUERY, key=KEY, transport=transport)
    assert result['raw'] == RAW
    assert result['feature_count'] == 2
    assert result['status'] == 'features'
    assert result['geometry_semantics'] == 'tile_fragments_not_spatial_analysis'
    assert 'XIT001' not in result['processing']
    assert result['fetched_at'].endswith('Z')
    assert '/XKT002?' in seen[0][0]
    assert KEY not in seen[0][0]
    assert seen[0][1]['Ocp-Apim-Subscription-Key'] == KEY
    assert seen[0][2:] == (30, 8_000_000)
    for bad in ({'z': 10}, {'x': -1}, {'y': 16384}, {'z': True}, {'url': 'https://invalid'}):
        with pytest.raises(CoreError):
            layers.fetch('XKT002', {**QUERY, **bad}, key=KEY, transport=transport)
    assert len(seen) == 1
