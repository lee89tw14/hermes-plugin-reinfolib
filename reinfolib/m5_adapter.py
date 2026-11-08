"""Localized M5 tool definitions/dispatch: GSI geocoding and per-layer point overlay."""
import time
from datetime import datetime, timezone

from . import geocode, layers, overlay
from .storage import CoreError
from .layer_provenance import provenance
from .m4_adapter import METADATA, failure_result, _Immediate

GEOCODE_DESCRIPTION = (
    'Find coordinates for a Japanese address via GSI. '
    'Uses the 国土地理院 address search (no API key) and returns every candidate with its title, lon/lat '
    'and a matched_level derived from the title (house, block, chome, area_or_place); it never picks one. '
    'The point is a geocoded representative point of the matched address text (often a block), not a parcel '
    'or building footprint. 出典：国土地理院ウェブサイト. GSI may change or stop this search without notice.')
OVERLAY_DESCRIPTION = (
    'Check official map layers at one Japanese address. '
    'Geocodes via GSI (only block or house level; ambiguous or coarse matches are refused and candidates listed; '
    'pass candidate_id to choose), fetches the official GeoJSON tiles covering the point plus tolerance_m and reports '
    'each layer separately: relation inside / within_tolerance / outside / no_features_in_tile / not_covered / failed, '
    'source vintage, licensing, coverage and whether it is a legal designation or a hazard estimate. '
    'No features, outside or 404 are never safe. XPT001 and XPT002 are refused; commercial purpose fails closed.')
OVERLAY_ENDPOINTS = sorted(overlay.LAYER_KINDS)
DEFINITIONS = {
    'reinfolib_geocode': (GEOCODE_DESCRIPTION, {
        'address': {'type': 'string', 'minLength': 1, 'maxLength': geocode.MAX_ADDRESS}}, ['address']),
    'reinfolib_point_layers': (OVERLAY_DESCRIPTION, {
        'address': {'type': 'string', 'minLength': 1, 'maxLength': geocode.MAX_ADDRESS},
        'candidate_id': {'type': 'string', 'pattern': '^[0-9a-f]{16}$'},
        'endpoints': {'type': 'array', 'minItems': 1, 'maxItems': overlay.MAX_ENDPOINTS, 'uniqueItems': True,
                      'items': {'type': 'string', 'enum': OVERLAY_ENDPOINTS}},
        'tolerance_m': {'type': 'number', 'minimum': 0, 'maximum': overlay.MAX_TOLERANCE_M},
        'purpose': {'type': 'string', 'enum': ['research', 'commercial']}}, ['address', 'endpoints']),
}
CANDIDATE_LIMIT = 20


def gsi_gate():
    """Profile-scoped 1 s gate for GSI, separate from the Reinfolib gate (different service)."""
    from . import adapter
    from .scheduling import RateGate
    return RateGate(adapter.data_root() / 'gsi-rate-gate', spacing=1.0)


def _modules():
    if '.' in (__package__ or ''):
        from ..service import _secret
        from .. import client as client_module
    else:
        from service import _secret
        import client as client_module
    return _secret, client_module


def _policy():
    from . import adapter
    return adapter.RETRY_POLICY or adapter._retry_policy()


def _public(found):
    return {k: v for k, v in found.items() if k != 'all_candidates'}


def _geocode(address, client_module, end):
    return geocode.geocode(address, transport=client_module.default_transport, gate=gsi_gate(),
                           policy=_policy(), absolute_deadline=end)


def _check_args(args, allowed, required):
    if not isinstance(args, dict) or set(args) - allowed or not required <= set(args):
        raise CoreError('invalid_argument', 'invalid_parameters')


def _geocode_tool(args, end):
    _check_args(args, {'address'}, {'address'})
    geocode.validate_address(args['address'])
    _secret, client_module = _modules()
    found = _public(_geocode(args['address'], client_module, end))
    return {**found, 'processing_note': 'matched_level and overlay_eligible are derived by this plugin from the GSI title.'}


class _Refusal(Exception):
    def __init__(self, error, extra):
        super().__init__(error.error_type)
        self.error, self.extra = error, extra


def _overlay_tool(args, end):
    _check_args(args, {'address', 'candidate_id', 'endpoints', 'tolerance_m', 'purpose'}, {'address', 'endpoints'})
    endpoints = overlay.validate_endpoints(args['endpoints'])
    tolerance = overlay.validate_tolerance(args.get('tolerance_m'))
    candidate_id = args.get('candidate_id')
    if candidate_id is not None and (not isinstance(candidate_id, str) or len(candidate_id) != 16 or
                                     any(c not in '0123456789abcdef' for c in candidate_id)):
        raise CoreError('invalid_argument', 'bad_candidate_id')
    purpose = args.get('purpose', 'research')
    if purpose not in ('research', 'commercial'):
        raise CoreError('invalid_argument', 'bad_purpose')
    address = geocode.validate_address(args['address'])
    if purpose == 'commercial':
        raise CoreError('license_review_required', 'regional_terms_not_cleared')
    _secret, client_module = _modules()
    key = _secret('REINFOLIB_API_KEY')
    if not isinstance(key, str) or not key:
        raise CoreError('config', 'missing_key')
    found = _geocode(address, client_module, end)
    try:
        chosen = geocode.select(found, candidate_id)
    except CoreError as error:
        extra = {'candidates': found['all_candidates'][:CANDIDATE_LIMIT], 'candidate_count': found['candidate_count'],
                 'gsi_attempts': found['attempts'], **geocode.provenance()}
        if error.error_type == 'location_not_reliable':
            extra['reason'] = ('The GSI match is coarser than block (街区) or house level, so the point may be far from '
                               'the property; object-level overlay is refused. Use a more specific address.')
        elif error.error_type == 'ambiguous_location':
            extra['reason'] = 'Several candidates match; pass candidate_id of the intended one. None was picked.'
        raise _Refusal(error, extra) from None
    point = (chosen['lon'], chosen['lat'])
    planned = overlay.plan(*point, endpoints, tolerance)
    from . import adapter
    from .transport import request, TransportFailure
    gate = adapter.production_gate()
    policy = _policy()
    results, reinfolib_requests = [], 0
    for endpoint, z, tiles in planned:
        fetched, error, attempts = [], None, []
        for x, y in tiles:
            diagnostics = {}

            def scheduled(url, headers, timeout, cap, diagnostics=diagnostics):
                try:
                    response = request(url, headers, timeout=timeout, cap=cap, policy=policy,
                                       transport=client_module.default_transport, gate=gate, absolute_deadline=end)
                except TransportFailure as failure:
                    diagnostics['attempts'] = failure.attempts
                    raise CoreError(failure.kind, failure.detail) from None
                diagnostics['attempts'] = response.attempts
                return response.status, response.headers, response.payload
            query = {'response_format': 'geojson', 'z': z, 'x': x, 'y': y}
            try:
                tile = layers.fetch(endpoint, query, key=key, transport=scheduled, gap=_Immediate())
            except CoreError as failure:
                error = failure
            else:
                fetched.append({'z': z, 'x': x, 'y': y, 'features': tile['raw']['features']})
            finally:
                for attempt in diagnostics.get('attempts', []):
                    attempts.append({'tile': [z, x, y], **attempt})
            if error is not None:
                break
        reinfolib_requests += len(attempts)
        layer = overlay.layer_result(endpoint, z, tiles, fetched, error, point, tolerance, METADATA)
        layer['attempts'] = attempts
        results.append(layer)
    location = {**chosen, 'selection': 'candidate_id' if candidate_id else 'single_candidate',
                'coordinate_semantics': geocode.SEMANTICS}
    return {'ok': True, 'query': {'address': address, 'endpoints': endpoints, 'tolerance_m': tolerance,
                                  'candidate_id': candidate_id},
            'location': location, 'tolerance_m': tolerance, 'layers': results,
            'failed_layer_n': sum(r['relation'] in ('failed', 'not_covered') for r in results),
            'gsi_attempts': found['attempts'], 'reinfolib_requests': reinfolib_requests,
            'limitations': list(overlay.LIMITATIONS), **geocode.provenance(), **provenance(),
            'processing': ('This plugin geocoded the address with GSI, fetched official Reinfolib GeoJSON tiles and '
                           'computed point/feature relations with an explicit tolerance; layer_kind is a plugin '
                           'classification. Not produced by MLIT or GSI.'),
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


def dispatch(name, args, *, absolute_deadline=None):
    end = time.time() + _policy().deadline
    if absolute_deadline is not None:
        end = min(end, absolute_deadline)
    try:
        if name == 'reinfolib_geocode':
            return _geocode_tool(args, end)
        if name == 'reinfolib_point_layers':
            return _overlay_tool(args, end)
        raise CoreError('invalid_argument', 'invalid_parameters')
    except _Refusal as refusal:
        error = refusal.error
        return {**failure_result(error.error_type, error.detail, error.status_code), **refusal.extra}
    except CoreError as error:
        extra = {'attempts': error.attempts} if hasattr(error, 'attempts') else {}
        extra.update(geocode.provenance() if name == 'reinfolib_geocode' else {})
        return failure_result(error.error_type, error.detail, error.status_code, extra)
    except OSError:
        return failure_result('storage', 'storage_unavailable')
    except Exception:
        return failure_result('internal', 'local_view_failed')
