"""M4 provenance: retrieval time differs from source vintage/publication."""
from datetime import datetime, timezone
from .views import provenance as common_provenance


def provenance():
    return {**common_provenance(),
            'processing':'Official land/tile raw response retained; no spatial joins, deduplication, valuation or PBF decoding.',
            'fetched_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}
