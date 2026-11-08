"""M5 skill text: overlay workflow present, stale 'no geocoding/overlay/decoder' claims removed."""
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / 'skills' / 'property-transactions' / 'SKILL.md'


def test_skill_describes_geocode_and_overlay_rules():
    text = SKILL.read_text(encoding='utf-8')
    for required in ['reinfolib_geocode', 'reinfolib_point_layers', 'candidate_id', 'location_not_reliable',
                     'ambiguous_location', 'tolerance_m', 'not_covered', 'no_features_in_tile', 'never safe',
                     'not a parcel', 'layer_kind', '出典：国土地理院ウェブサイト', 'decode_pbf', 'XPT001']:
        assert required in text, required
    assert 'No property appraisal, geocoding, GIS overlay, decoder selection' not in text
    assert 'PBF is opaque raw_base64 with format=pbf and decoded=false; do not call it GeoJSON' not in text
