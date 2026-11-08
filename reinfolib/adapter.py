"""Hermes adapter; all profile state and scoped credentials resolve at call time."""
import json
from pathlib import Path

from .views import ViewError, provenance, page, export, validate_filters
import re

QUERY_PROPERTIES = {
    'year': {'type': 'integer', 'minimum': 2005},
    'quarter': {'type': 'integer', 'minimum': 1, 'maximum': 4},
    'area': {'type': 'string', 'pattern': '^[0-9]{2}$'},
    'city': {'type': 'string', 'pattern': '^[0-9]{5}$'},
    'station': {'type': 'string', 'pattern': '^[0-9]{6}$'},
    'price_classification': {'type': 'string', 'enum': ['01', '02', 'both']},
    'language': {'type': 'string', 'enum': ['ja', 'en']},
}
FILTER_PROPERTIES = {key: {'type': 'string'} for key in ('district_name', 'type', 'floor_plan', 'price_category', 'period', 'area_min', 'area_max', 'price_min', 'price_max')}
FILTER_PROPERTIES.update({key: {'type': 'integer'} for key in ('building_year_min', 'building_year_max')})
FILTER_SCHEMA = {'type': 'object', 'properties': FILTER_PROPERTIES, 'additionalProperties': False}
PAGE_PROPERTIES = {'offset': {'type': 'integer', 'minimum': 0},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                   'filters': FILTER_SCHEMA}
SNAPSHOT = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
DEFINITIONS = {
    'reinfolib_transactions': (
        'Japan property sales by station, city or prefecture. '
        'Station uses the six-digit N02_005g code. '
        'Provide exactly one of station/city/area and year; omit quarter for annual data. '
        '01 survey, 02 contract, both may overlap. Saves complete raw snapshot in profile plugin data; returns first local page. '
        'No building names or exact property addresses. Missing rows HTTP404; station400 requires explicit regional fallback.',
        {**QUERY_PROPERTIES, **PAGE_PROPERTIES}, ['year']),
    'reinfolib_records': (
        'Filter and page saved property transaction records. '
        'Local snapshot access only, without API requests. '
        'Counts/statistics cover all matched records, not only this page. Exact string filters; numeric bounds are decimal strings.',
        {'snapshot_id': SNAPSHOT, **PAGE_PROPERTIES}, ['snapshot_id']),
    'reinfolib_export': (
        'Export complete property transaction records to JSON/CSV. '
        'Exports all matched rows to private profile plugin exports as JSON or UTF-8 BOM CSV. '
        'Writes files with attribution and a provenance manifest; returns absolute paths and SHA256. '
        'CSV formula text is escaped; original raw values remain in JSON. No user-supplied output path.',
        {'snapshot_id': SNAPSHOT, 'format': {'type': 'string', 'enum': ['json', 'csv']}, 'filters': FILTER_SCHEMA}, ['snapshot_id']),
    'reinfolib_catalog': (
        'List MLIT real estate APIs and implementation status. '
        'Lists all 36 documented APIs; implemented adapters and live verification are separate. '
        'Does not call an upstream API or create state.', {}, []),
    'reinfolib_municipalities': (
        'List municipality codes in one Japanese prefecture. '
        'Official XIT002 list for a two-digit prefecture code: five-digit code, name and a locally derived kind '
        '(designated_city, designated_city_ward with parent, special_ward, city, town_or_village). '
        'Optional name lists every substring match as candidates and never picks one; ward names repeat across cities, '
        'so use display_name. HTTP404 is not_found, not an empty list. No snapshots saved; profile rate-gate state is persisted.',
        {'area': QUERY_PROPERTIES['area'], 'language': QUERY_PROPERTIES['language'],
         'name': {'type': 'string', 'minLength': 1, 'maxLength': 50}}, ['area']),
    'reinfolib_stations': (
        'Find Japanese railway station codes for property queries. '
        'Local official N02 (国土数値情報 鉄道データ, packaged version) lookup: station name or six-digit code -> '
        'N02_005g group code candidates with operator and line, for reinfolib_transactions station. '
        'Exact name first (trailing 駅 ignored), otherwise substring matches; same-name stations are listed '
        'as separate candidates and none is picked. No API key, request or files.',
        {'name': {'type': 'string', 'minLength': 1, 'maxLength': 50},
         'code': {'type': 'string', 'pattern': '^[0-9]{6}$'},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, []),
    'reinfolib_snapshots': (
        'List saved property snapshots for one query, newest first. '
        'Local only, no API request: snapshot_id, fetched_at and upstream_count of earlier '
        'reinfolib_transactions results with the same effective query (defaults 01/ja applied). '
        'A new reinfolib_transactions call always creates a new immutable snapshot; none is overwritten. '
        'Snapshots expire after 30 days.',
        {**QUERY_PROPERTIES, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['year']),
}
from .research_tools import DEFINITIONS as _RESEARCH_DEFINITIONS, HANDLERS as RESEARCH_HANDLERS  # noqa: E402
DEFINITIONS.update(_RESEARCH_DEFINITIONS)


from . import m4_adapter
DEFINITIONS.update(m4_adapter.DEFINITIONS)
from . import m5_adapter  # noqa: E402
DEFINITIONS.update(m5_adapter.DEFINITIONS)


def data_root():
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir('jp-reinfolib') / 'v2'


def _retry_policy():
    from .transport import DEFAULT_POLICY
    return DEFAULT_POLICY


RETRY_POLICY = None  # None -> transport.DEFAULT_POLICY; tests may inject a faster policy


def production_gate():
    """Profile-scoped cross-process 1 s gate for every v2 outbound request."""
    from .scheduling import RateGate
    return RateGate(data_root() / 'rate-gate')


def catalog():
    path = Path(__file__).resolve().parent / 'data' / 'endpoint-inventory.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    return {'ok': True, 'endpoint_count': len(data['endpoints']),
            'endpoints': [{'id': e['id'], 'title': e['title'], 'transport': e['transport'],
                           'parameters': e['documented_parameters'], 'zoom': e['zoom'],
                           'implemented': e['implementation_status'].startswith('implemented'),
                           'live_verified': e.get('new_adapter_live_verified', False),
                           'live_verification_scope': e.get('live_verification_scope', []),
                           'source_url': e['source_url'],
                           'source_metadata': m4_adapter.METADATA.get(e['id'],{}),
                           'tile_contract': m4_adapter.layers.CONTRACTS.get(e['id']),
                           'overlay': e.get('m5_overlay')} for e in data['endpoints']], **provenance()}


class AdapterFailure(Exception):
    def __init__(self, kind, detail='', status=None, attempts=None):
        super().__init__(kind)
        self.kind, self.detail, self.status, self.attempts = kind, detail, status, attempts


def load_snapshot(snapshot_id):
    from .storage import SnapshotStore, CoreError
    try:
        return SnapshotStore(data_root() / 'snapshots').load(snapshot_id)
    except CoreError as error:
        raise AdapterFailure(error.error_type, error.detail, error.status_code) from None


def fetch_snapshot(query):
    from .core import fetch_transactions, validate_query, CoreError
    from .storage import SnapshotStore
    if __package__ and '.' in __package__:
        from ..service import _secret
    else:
        from service import _secret
    try:
        query = validate_query(query)
        key = _secret('REINFOLIB_API_KEY')
        return fetch_transactions(query, key=key, storage=SnapshotStore(data_root() / 'snapshots'),
                                  gap=production_gate(), retry=RETRY_POLICY or _retry_policy())
    except CoreError as error:
        if error.detail == 'too_big':
            # Upstream body over the 8 MB cap (compressed or decoded): nothing was saved or truncated.
            raise AdapterFailure('response_too_large', 'too_big', error.status_code, getattr(error, 'attempts', None)) from None
        raise AdapterFailure(error.error_type, error.detail, error.status_code, getattr(error, 'attempts', None)) from None


def _scoped_key():
    if __package__ and '.' in __package__:
        from ..service import _secret
    else:
        from service import _secret
    return _secret('REINFOLIB_API_KEY')


def lookup_municipalities(args):
    from . import locations
    from .storage import CoreError
    try:
        query = locations.validate_municipality_query(args)
        return locations.fetch_municipalities(query, key=_scoped_key(), gap=production_gate(),
                                               retry=RETRY_POLICY or _retry_policy())
    except CoreError as error:
        raise AdapterFailure(error.error_type, error.detail, error.status_code, getattr(error, 'attempts', None)) from None


def lookup_stations(args):
    from . import stations
    try:
        found = stations.lookup(args.get('name'), code=args.get('code'), limit=args.get('limit', 20))
    except stations.StationIndexError as error:
        raise ViewError(error.error_type, error.detail) from None
    found['station_attribution'] = found.pop('attribution')
    found['station_license'] = found.pop('license')
    found['station_license_url'] = found.pop('license_url')
    found['note'] = ('Pass group_code to reinfolib_transactions station; a station_code that is not a group code '
                     'returned HTTP 400 (fallback_required) in live tests, as did an N02-24-only group code. '
                     'Codes come from this N02 version (the one the XIT001 manual links) and can change between '
                     'versions. On 400 choose an area or city explicitly.')
    return {**found, **provenance(),
            'processing': 'Station candidates from a local index derived from official N02 data; no API request.'}


def list_saved_snapshots(args):
    from .snapshots import list_snapshots
    from .storage import CoreError, SnapshotStore
    query = {k: v for k, v in args.items() if k in QUERY_PROPERTIES}
    try:
        return list_snapshots(SnapshotStore(data_root() / 'snapshots'), query, limit=args.get('limit', 20))
    except CoreError as error:
        raise AdapterFailure(error.error_type, error.detail, error.status_code, getattr(error, 'attempts', None)) from None


def _sweep():
    """Best-effort 30-day retention; any failure becomes one fixed warning."""
    from . import maintenance
    try:
        return maintenance.sweep_expired(data_root()).get('ok') is True
    except Exception:
        return False


def dispatch(name, args):
    if name in m4_adapter.DEFINITIONS or name in m5_adapter.DEFINITIONS:
        module = m4_adapter if name in m4_adapter.DEFINITIONS else m5_adapter
        try:
            return json.dumps(module.dispatch(name, args), ensure_ascii=False, allow_nan=False)
        except OSError:
            return '{"ok":false,"error_type":"storage","detail":"storage_unavailable"}'
        except Exception:
            # Do not invoke a failed encoder/provenance provider on the error path.
            return '{"ok":false,"error_type":"internal","detail":"local_view_failed"}'
    cleanup_ok = True
    try:
        if name not in DEFINITIONS or not isinstance(args, dict):
            raise ViewError('invalid_argument', 'invalid_arguments')
        _, properties, required = DEFINITIONS[name]
        if set(args) - set(properties) or any(k not in args for k in required):
            raise ViewError('invalid_argument', 'invalid_arguments')
        if name == 'reinfolib_catalog':
            result = catalog()
        elif name == 'reinfolib_municipalities':
            result = {**lookup_municipalities(args), **provenance(),
                      'processing': 'XIT002 list as returned; kind, display_name and candidate matching derived locally by this plugin.'}
        elif name == 'reinfolib_stations':
            result = lookup_stations(args)
        elif name == 'reinfolib_snapshots':
            cleanup_ok = _sweep()
            result = {**list_saved_snapshots(args), **provenance()}
        elif name in RESEARCH_HANDLERS:
            cleanup_ok = _sweep()
            result = RESEARCH_HANDLERS[name](args)
        else:
            cleanup_ok = _sweep()
            if 'snapshot_id' in properties:
                snapshot_id = args['snapshot_id']
                if not isinstance(snapshot_id, str) or not re.fullmatch('[0-9a-f]{32}', snapshot_id):
                    raise ViewError('invalid_argument', 'invalid_snapshot_id')
            filters = validate_filters(args.get('filters'))
            if name == 'reinfolib_export':
                fmt = args.get('format', 'json')
                if fmt not in ('json', 'csv'):
                    raise ViewError('invalid_argument', 'invalid_export_format')
                result = export(load_snapshot(args['snapshot_id']), data_root() / 'exports', format=fmt, filters=filters)
            else:
                offset, limit = args.get('offset', 0), args.get('limit', 20)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
                    raise ViewError('invalid_argument', 'invalid_paging')
                if name == 'reinfolib_transactions':
                    query = {k: v for k, v in args.items() if k in QUERY_PROPERTIES}
                    snapshot = fetch_snapshot(query)
                else:
                    snapshot = load_snapshot(args['snapshot_id'])
                result = page(snapshot, offset=offset, limit=limit, filters=filters)
                if 'attempts' in snapshot:
                    result['attempts'] = snapshot['attempts']
    except AdapterFailure as error:
        result = {'ok': False, 'error_type': error.kind, 'detail': error.detail,
                  'status_code': error.status, **provenance()}
        if error.attempts is not None:
            result['attempts'] = error.attempts
        if error.kind == 'fallback_required':
            result['fallback'] = 'Retry explicitly with area or city; this changes the geographic scope. No automatic fallback was performed.'
        elif error.kind == 'response_too_large':
            result['hint'] = 'Upstream response exceeded the 8 MB cap; nothing was saved or truncated. Query a single quarter or a smaller scope.'
    except ViewError as error:
        result = {'ok': False, 'error_type': error.error_type, 'detail': error.detail, **provenance()}
    except ValueError:
        result = {'ok': False, 'error_type': 'internal', 'detail': 'local_view_failed', **provenance()}
    except OSError:
        result = {'ok': False, 'error_type': 'storage', 'detail': 'storage_unavailable', **provenance()}
    if not cleanup_ok:
        result.setdefault('warnings', []).append('retention_cleanup_failed')
    return json.dumps(result, ensure_ascii=False, allow_nan=False)


class PartialRegistration(RuntimeError):
    """Hermes accepted some v2 tools and rejected others; rolled back."""


def _prepared():
    prepared = []
    for name, (description, properties, required) in DEFINITIONS.items():
        prepared.append({
            'name': name, 'toolset': 'jp_reinfolib',
            'schema': {'name': name, 'description': description,
                       'parameters': {'type': 'object', 'properties': properties,
                                      'required': required, 'additionalProperties': False}},
            'handler': lambda args, _name=name, **kwargs: dispatch(_name, args),
        })
    return prepared


def _rollback(handles):
    for handle in reversed(handles):
        dispose = getattr(handle, 'dispose', None)
        if callable(dispose):
            try:
                dispose()
            except Exception:
                pass


def register(ctx):
    """All-or-nothing: build every definition first, then register; on any failure
    dispose the PluginRegistration handles Hermes returned so no partial v2 remains.
    A mix of handles and None (Hermes rejected a name) is also treated as failure.
    A context returning None for every tool exposes no handles to judge or undo."""
    prepared = _prepared()
    handles = []
    try:
        for item in prepared:
            handles.append(ctx.register_tool(**item))
        if any(h is None for h in handles) and any(h is not None for h in handles):
            raise PartialRegistration('v2 registration incomplete')
    except BaseException:
        _rollback([h for h in handles if h is not None])
        raise
