"""Localized M4 tool definitions/dispatch; no Hermes imports in endpoint modules."""
import json
from pathlib import Path
from . import layers, land_values
from .storage import CoreError
from .views import ViewError
from .layer_provenance import provenance

METADATA=json.loads((Path(__file__).parent/'data/layer-metadata.json').read_text())
TILE_QUERY={'type':'object','properties':{
    'response_format':{'type':'string','enum':['geojson','pbf']},
    'z':{'type':'integer','minimum':9,'maximum':15},
    'x':{'type':'integer','minimum':0},'y':{'type':'integer','minimum':0},
    **{name:({'type':'integer','minimum':1995} if name=='year' else {'type':'string'})
       for spec in layers.CONTRACTS.values() for name in spec['filters']}},
    'required':['response_format','z','x','y'],'additionalProperties':False}
XCT_QUERY={'type':'object','properties':{'year':{'type':'integer'},'area':{'type':'string','pattern':'^[0-9]{2}$'},'division':{'type':'string','enum':list(land_values.DIVISIONS)}},'required':['year','area','division'],'additionalProperties':False}
DESCRIPTION=('Retrieve Japan land-use, facility and hazard tiles. '
             'Preserves raw geometry/properties including split tile fragments. '
             'GeoJSON or base64 PBF (opaque unless decode_pbf); no joins or safety/property-level inferences. '
             '200 empty means no_features, never safe; 404 and auth errors distinct. '
             'XPT001 coordinates are nearest_station, NEVER property hazard/school/radius judgments. '
             'Regional licensing conditions and source vintages differ; commercial export not cleared. ')
DECODE_NOTE=('decode_pbf=true (pbf only) adds decoded_layers from a stdlib MVT decoder; geometry is the simplified, '
             'tile-quantised PBF encoding, not the GeoJSON response; raw_base64 is kept. ')
SAVE_NOTE=('save_artifact=true (pbf only) also writes the exact raw PBF bytes plus a metadata file to private '
           'profile exports (path, sha256, artifact_id returned); not GeoJSON; deleted after 30 days. ')
DEFINITIONS={
    'reinfolib_layer':(DESCRIPTION+DECODE_NOTE+SAVE_NOTE,{'endpoint':{'type':'string','enum':sorted(layers.CONTRACTS)},'query':TILE_QUERY,'purpose':{'type':'string','enum':['research','commercial']},'decode_pbf':{'type':'boolean'},'save_artifact':{'type':'boolean'}},['endpoint','query']),
    'reinfolib_land_values':('Standard-land background, not arbitrary property appraisal. XCT001 uses year/area/division and rolling five years. XPT002 uses tile coordinates, year since 1995, classification 0/1 (not 01/02). '+DESCRIPTION,{'endpoint':{'type':'string','enum':['XCT001','XPT002']},'query':{'oneOf':[XCT_QUERY,TILE_QUERY]},'purpose':{'type':'string','enum':['research','commercial']}},['endpoint','query'])}


class _Immediate:
    """M3 request owns spacing; never also invoke the legacy per-process gap."""
    def run(self, call):
        return call()


def dispatch(name,args, *, absolute_deadline=None):
    diagnostics = {}
    try:
        allowed_keys={'endpoint','query','purpose'}|({'decode_pbf','save_artifact'} if name=='reinfolib_layer' else set())
        if not isinstance(args,dict) or args.keys()-allowed_keys or not {'endpoint','query'}<=args.keys():
            raise CoreError('invalid_argument','invalid_parameters')
        decode=args.get('decode_pbf',False)
        if type(decode) is not bool:
            raise CoreError('invalid_argument','bad_decode_flag')
        if decode and (not isinstance(args['query'],dict) or args['query'].get('response_format')!='pbf'):
            raise CoreError('invalid_argument','decode_requires_pbf')
        save=args.get('save_artifact',False)
        if type(save) is not bool:
            raise CoreError('invalid_argument','bad_save_artifact_flag')
        if save and (not isinstance(args['query'],dict) or args['query'].get('response_format')!='pbf'):
            raise CoreError('invalid_argument','save_artifact_requires_pbf')
        endpoint=args['endpoint']
        allowed=layers.CONTRACTS if name=='reinfolib_layer' else {'XCT001','XPT002'}
        if not isinstance(endpoint,str) or endpoint not in allowed:
            raise CoreError('invalid_argument','unsupported_endpoint')
        purpose=args.get('purpose','research')
        if purpose not in ('research','commercial'):
            raise CoreError('invalid_argument','bad_purpose')
        if purpose=='commercial':
            raise CoreError('license_review_required','regional_terms_not_cleared')
        if '.' in (__package__ or ''):
            from ..service import _secret
            from .. import client as client_module
        else:
            from service import _secret
            import client as client_module
        # Validate before scoped secret access or HTTP.
        if endpoint=='XCT001': land_values.validate(args['query'])
        else: layers.validate(endpoint,args['query'])
        key=_secret('REINFOLIB_API_KEY')
        from . import adapter
        from .transport import request, TransportFailure
        import time
        policy = adapter.RETRY_POLICY or adapter._retry_policy()
        end = time.time() + policy.deadline
        if absolute_deadline is not None:
            end = min(end, absolute_deadline)
        gate = adapter.production_gate()
        def scheduled(url, headers, timeout, cap):
            try:
                response = request(url, headers, timeout=timeout, cap=cap, policy=policy,
                                   transport=client_module.default_transport,
                                   gate=gate, absolute_deadline=end)
            except TransportFailure as failure:
                diagnostics['attempts'] = failure.attempts
                diagnostics['stop_reason'] = failure.stop_reason
                raise CoreError(failure.kind, failure.detail) from None
            diagnostics.update(attempts=response.attempts, stop_reason=response.stop_reason)
            return response.status, response.headers, response.payload
        result=(land_values.fetch(args['query'],key=key,transport=scheduled,gap=_Immediate()) if endpoint=='XCT001' else layers.fetch(endpoint,args['query'],key=key,transport=scheduled,gap=_Immediate()))
        result.update(diagnostics)
        if decode:
            from . import mvt
            import base64
            q=result['query']
            tile=mvt.decode(base64.b64decode(result['raw_base64']),q['z'],q['x'],q['y'])
            result.update(decoded=True,decoded_layers=tile['layers'],decoded_feature_count=tile['feature_count'],
                          dropped_degenerate_rings=tile['dropped_degenerate_rings'],
                          decoded_semantics='mvt_decoded_simplified_quantised_not_the_geojson_response',
                          status='features' if tile['feature_count'] else 'no_features')
        if (decode or save) and endpoint=='XPT001':
            result['warnings']=['prohibited_property_hazard_school_zone_radius_inference']
        result['source_metadata']=METADATA[endpoint]
        if save:
            from . import adapter as plugin_adapter, maintenance, tile_export
            import base64
            artifact_warnings=list(result.get('warnings',[]))
            if not plugin_adapter._sweep():  # same best-effort 30-day cleanup as reinfolib_export
                result['warnings']=artifact_warnings+['retention_cleanup_failed']
            result['artifact']=tile_export.save(
                plugin_adapter.data_root()/'exports', endpoint=endpoint, query=result['query'],
                payload=base64.b64decode(result['raw_base64']), fetched_at=result['fetched_at'],
                source_metadata=METADATA[endpoint], retention_days=maintenance.RETENTION_DAYS,
                geometry_semantics=result['geometry_semantics'], status=result['status'],
                warnings=artifact_warnings)
        result['processing']=('Raw official PBF retained and decoded by this plugin (stdlib MVT decoder); decoded geometry is simplified and quantised; no spatial analysis.'
                              if decode else 'Raw official response retained; single-tile geometry may be clipped/split; no spatial analysis or PBF decoding.')
        return result
    except CoreError as error:
        return failure_result(error.error_type, error.detail, error.status_code, diagnostics)
    except ViewError as error:
        return failure_result(error.error_type, error.detail, diagnostics=diagnostics)
    except OSError:
        return failure_result('storage', 'storage_unavailable', diagnostics=diagnostics)
    except Exception:
        return failure_result('internal', 'local_view_failed', diagnostics=diagnostics)


def failure_result(kind, detail, status=None, diagnostics=None):
    """The error path must survive a failing provenance provider too."""
    try:
        source = provenance()
    except Exception:
        source = {}
    return {**source, 'ok': False, 'error_type': kind, 'detail': detail,
            'status_code': status, **(diagnostics or {})}
