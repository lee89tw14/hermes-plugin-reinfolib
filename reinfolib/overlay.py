"""Per-layer overlay planning and result assembly for one geocoded point (no HTTP here).

``layer_kind`` is this plugin's classification of what a layer represents; it is not an
official field. A geometric relation to a tile feature is not a legal determination,
an engineering assessment or a statement that a place is safe.
"""
from . import spatial
from .storage import CoreError

MAX_ENDPOINTS = 6
MAX_TOTAL_TILES = 16
MAX_TILES_PER_LAYER = 4
MAX_TOLERANCE_M = 100
DEFAULT_TOLERANCE_M = 10
EXCLUDED = {'XPT001': 'xpt001_not_property_level', 'XPT002': 'endpoint_requires_filters'}

KIND_NOTES = {
    'legal_designation': 'Area or line designated/decided under a statute or city plan (source data vintage applies; '
                         'current legal status must be confirmed with the designating authority).',
    'physical_hazard_estimate': 'Modelled or survey-based estimate of a physical hazard; not a legal designation '
                                'and not an observed event.',
    'land_condition_survey': 'Survey/map of land condition (e.g. large fill); presence does not establish hazard level.',
    'hazard_history_record': 'Historical disaster records compiled from available sources; coverage is partial.',
    'administrative_reference_area': 'Administrative reference area (e.g. school district) at the source vintage; '
                                     'municipalities may change it.',
    'facility_location': 'Facility point locations; relation is distance only.',
    'statistical_area': 'Statistical estimate or tabulation area/line, not a designation.',
}
LAYER_KINDS = {
    'XKT001': 'legal_designation', 'XKT002': 'legal_designation', 'XKT003': 'legal_designation',
    'XKT014': 'legal_designation', 'XKT016': 'legal_designation', 'XKT019': 'legal_designation',
    'XKT021': 'legal_designation', 'XKT022': 'legal_designation', 'XKT023': 'legal_designation',
    'XKT024': 'legal_designation', 'XKT029': 'legal_designation', 'XKT030': 'legal_designation',
    'XNT001': 'legal_designation',
    'XKT025': 'physical_hazard_estimate', 'XKT026': 'physical_hazard_estimate',
    'XKT027': 'physical_hazard_estimate', 'XKT028': 'physical_hazard_estimate',
    'XKT020': 'land_condition_survey', 'XST001': 'hazard_history_record',
    'XKT004': 'administrative_reference_area', 'XKT005': 'administrative_reference_area',
    'XGT001': 'facility_location', 'XKT006': 'facility_location', 'XKT007': 'facility_location',
    'XKT010': 'facility_location', 'XKT011': 'facility_location', 'XKT017': 'facility_location',
    'XKT018': 'facility_location',
    'XKT013': 'statistical_area', 'XKT015': 'statistical_area', 'XKT031': 'statistical_area',
}
COVERAGE_NOTE = ('Only the listed official tiles were checked. A tile without features, an outside result or HTTP 404 '
                 'does not establish that no designation or hazard exists; source datasets can be partial by region '
                 'and vintage (see content_notes and contents_url).')
LIMITATIONS = ['geocoded_point_is_not_parcel_or_building_footprint',
               'relations_are_tile_geometry_checks_not_legal_or_engineering_determinations',
               'no_features_or_outside_is_never_safe',
               'no_property_level_conclusions_from_transaction_data',
               'xpt001_nearest_station_points_are_never_used']


def validate_endpoints(value):
    from .layers import CONTRACTS
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ENDPOINTS:
        raise CoreError('invalid_argument', 'bad_endpoints')
    if any(not isinstance(e, str) for e in value) or len(set(value)) != len(value):
        raise CoreError('invalid_argument', 'bad_endpoints')
    for endpoint in value:
        if endpoint in EXCLUDED:
            raise CoreError('invalid_argument', EXCLUDED[endpoint])
    for endpoint in value:
        if endpoint not in CONTRACTS or endpoint not in LAYER_KINDS:
            raise CoreError('invalid_argument', 'unsupported_endpoint')
    return list(value)


def validate_tolerance(value):
    if value is None:
        return DEFAULT_TOLERANCE_M
    if type(value) not in (int, float) or value != value or not 0 <= value <= MAX_TOLERANCE_M:
        raise CoreError('invalid_argument', 'bad_tolerance')
    return value


def plan(lon, lat, endpoints, tolerance_m):
    """[(endpoint, z, [(x, y), ...])]; refuses before any tile request when over budget."""
    planned = []
    for endpoint in endpoints:
        z = spatial.zoom_for(endpoint)
        planned.append((endpoint, z, spatial.tiles_for_point(lon, lat, z, tolerance_m, max_tiles=MAX_TILES_PER_LAYER)))
    if sum(len(tiles) for _, _, tiles in planned) > MAX_TOTAL_TILES:
        raise CoreError('invalid_argument', 'too_many_tiles')
    return planned


def layer_result(endpoint, z, tiles, fetched, error, point, tolerance_m, metadata):
    """fetched: [{'z','x','y','features'}] for successful tiles; error: CoreError or None."""
    kind = LAYER_KINDS[endpoint]
    meta = metadata.get(endpoint, {})
    result = {'endpoint': endpoint, 'content_name': meta.get('content_name'), 'layer_kind': kind,
              'layer_kind_basis': 'plugin_classification', 'layer_kind_note': KIND_NOTES[kind],
              'source_vintage': meta.get('source_vintage'), 'source_links': meta.get('source_links'),
              'commercial_listing': meta.get('commercial_listing'), 'license_review': meta.get('license_review'),
              'content_notes': meta.get('content_notes'), 'terms_url': meta.get('terms_url'),
              'contents_url': meta.get('contents_url'), 'coverage_note': COVERAGE_NOTE,
              'zoom': z, 'tiles': [[z, x, y] for x, y in tiles], 'tiles_fetched_n': len(fetched),
              'tolerance_m': tolerance_m, 'safety_inference_allowed': False}
    warnings = []
    if error is not None:
        if error.error_type == 'not_found':
            result.update(relation='not_covered', status_code=error.status_code)
            warnings.append('not_covered_is_not_safe')
        else:
            result.update(relation='failed', error_type=error.error_type, detail=error.detail,
                          status_code=error.status_code)
            warnings.append('layer_failed_no_conclusion')
    else:
        related = spatial.relate_layer(point, fetched, tolerance_m)
        related.pop('tiles', None)
        related.pop('tolerance_m', None)
        warnings.extend(related.pop('warnings'))
        result.update(related)
        if related['relation'] == 'no_features_in_tile':
            warnings.append('no_features_is_not_safe')
        elif related['relation'] == 'outside':
            warnings.append('outside_is_not_safe')
        if kind == 'legal_designation':
            warnings.append('relation_is_not_a_legal_determination')
    result['warnings'] = warnings
    return result
