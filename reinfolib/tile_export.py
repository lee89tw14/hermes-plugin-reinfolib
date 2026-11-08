"""Opt-in permanent raw PBF tile artifact: `<uuid>.pbf` + `<uuid>.metadata.json` in exports/.

Writes the exact payload bytes the base64/decoder path sees (after transport gzip handling).
Uses the same private publish path as transaction exports (0600 temp + exclusive link,
symlinked roots refused); the 30-day retention sweep and `delete export` cover the pair.
"""
import hashlib
import json
import uuid
from pathlib import Path

from .views import ViewError, _publish, provenance

SEMANTICS = 'raw_mapbox_vector_tile_bytes_not_geojson'
PROCESSING = ('raw Mapbox Vector Tile bytes, not GeoJSON: the official PBF response body exactly as '
              'received (after HTTP gzip decoding); no decoding, clipping, merging or spatial analysis.')


def save(root, *, endpoint, query, payload, fetched_at, source_metadata, retention_days,
         geometry_semantics, status, warnings=()):
    root = Path(root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ViewError('storage', 'symlink_export_root')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    artifact_id = uuid.uuid4().hex
    digest = hashlib.sha256(payload).hexdigest()
    meta = {**provenance(), 'artifact_id': artifact_id, 'kind': 'tile_pbf', 'endpoint': endpoint,
            'query': dict(query), 'format': 'pbf', 'encoding': 'raw_bytes', 'decoded': False,
            'semantics': SEMANTICS, 'fetched_at': fetched_at, 'byte_count': len(payload),
            'sha256': digest, 'source_metadata': source_metadata,
            'geometry_semantics': geometry_semantics, 'status': status,
            'warnings': list(warnings),
            'property_level_spatial_inference_allowed': False,
            'retention_days': retention_days, 'processing': PROCESSING}
    path = _publish(root, artifact_id + '.pbf', payload)
    try:
        manifest = _publish(root, artifact_id + '.metadata.json',
                            json.dumps(meta, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except BaseException:
        path.unlink()
        raise
    return {'artifact_id': artifact_id, 'format': 'pbf', 'semantics': SEMANTICS,
            'path': str(path.resolve()), 'manifest_path': str(manifest.resolve()),
            'sha256': digest, 'byte_count': len(payload), 'retention_days': retention_days}
