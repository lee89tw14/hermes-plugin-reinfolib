---
name: property-transactions
description: Query, page, filter and export Japan property sale records.
version: 0.1.0
author: rukka, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Japan, RealEstate, MLIT]
---

# Japan property transaction records

Use this workflow for MLIT transaction prices by station, municipality, or prefecture. Before web_search, use tool_search to find `reinfolib_transactions` and call it with tool_call. Use `reinfolib_quarter` only for its fixed weekly one-city/one-quarter contract.

## Procedure

0. When the user names a municipality instead of giving a five-digit code, call `reinfolib_municipalities` with the two-digit prefecture `area` and `name`.
   - Completion: use a code only when `match_status` is `unique`. If it is `ambiguous`, show the candidates' `display_name` and ask; never pick one. Ward names such as 緑区 repeat across designated cities.
   - A `designated_city` code (for example 14100) is the whole city; its wards have their own codes. HTTP 404 is `not_found`, not an empty list.
1. Call `reinfolib_transactions` with exactly one of `station`, `city`, or `area`, plus `year`. Omit `quarter` for annual data. Use `price_classification` `01`, `02`, or `both`; `both` overlaps the two sources, and `02` starts in 2021. Use `language` `ja` or `en`.
   - Completion: retain the returned `snapshot_id`, `upstream_count`, `matched_count`, `fetched_at`, and warnings.
   - `station` is the six-digit official N02_005g group code. Do not invent it from a portal ID or station name.
   - Find it with `reinfolib_stations` (local official N02 index). Use a `group_code` only when `match_status` is `unique`; same-name stations in different places are separate candidates, so show operator and line and ask. Pass `group_code`, never a line-level `station_code`.
2. Read additional pages or apply filters with `reinfolib_records` on the same `snapshot_id`. Do not call the API again to page.
   - Completion: stop when `has_more` is false or the requested evidence is available.
   - Report filtered counts separately from upstream counts. Statistics cover all matched records, not only the current page.
3. Use `reinfolib_export` when the user needs the complete matched set.
   - Completion: report the absolute export path, row count, and SHA-256.
4. Include the returned attribution, processing statement, and credit when presenting counts, excerpts, statistics, or exports.

## Interpreting results

- Counts are API-covered records. They are not unique transactions, because 01 and 02 can both contain the same sale.
- Records omit building names, exact addresses, and property coordinates. Do not use this result for unit-level hazard, school-zone, or radius judgments.
- HTTP 404 is `not_found` for that request; it is not zero sales and not unchanged prices.
- A station request returning `fallback_required` performed no automatic geographic fallback. Ask before switching to a city or prefecture, and state the wider scope.
- Exact numeric or building-year filters exclude unparseable or missing values from the matched set; report these limits if counts matter.

## Data lifecycle

To see whether the same query was already fetched, call `reinfolib_snapshots` with the same query arguments; it lists saved `snapshot_id` values newest first with `fetched_at`, without an API request. Calling `reinfolib_transactions` again is a refresh: it always creates a new immutable snapshot and never overwrites an older one. Differences between snapshots are API-covered record changes, not new sales.

Snapshots and exports are stored privately in the active profile's plugin data. Users admitted to the same profile can access them. Snapshots and exports are deleted automatically after 30 days; a snapshot_id older than that returns not_found, so fetch again instead of reusing it. Early deletion is not exposed as a model tool. Do not claim that data was removed unless a deletion was actually run and reported.

「このサービスは、国土交通省の不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません」

## Land background and documented tile retrieval

For land values, zoning, facilities or hazards, use tool_search to find `reinfolib_land_values` or `reinfolib_layer` before web_search. These return raw data, not transaction snapshots.

1. Read `reinfolib_catalog` for the exact endpoint's tile_contract, source_metadata, implementation and live scope. Match its independent zoom and filter bounds; do not reuse another endpoint's schema.
2. Call one allowlisted endpoint with `endpoint` and nested `query`. Tile query requires response_format,z,x,y; XCT001 instead requires year,area,division. XPT002 requires year since 1995 and priceClassification 0/1; XCT001 permits rolling calendar current year minus four through current year. Publication may lag valid calendar inputs.
3. Retain full raw geometry/properties and unknown fields; preserve tile fragments and lines. Report feature_count as returned features, not unique objects. Do not deduplicate split tiles or join layers. An empty HTTP 200 is no_features, never safe; 404 and auth errors remain failures.
4. XPT001 coordinates are nearest_station. Never infer property-level hazard, school-zone or radius coverage from them. PBF stays opaque raw_base64 (decoded=false) unless you pass `decode_pbf: true`; decoded_layers are a simplified, tile-quantised decoding of the PBF, never the GeoJSON response.
5. Preserve source_vintage, attribution, processing and credit. API hazard sources can differ from map-display vintages. Check current source-specific terms, especially regional noncommercial restrictions. purpose=commercial fails closed until regional clearance exists; do not treat the plugin's MIT license as a data license.

Completion: report endpoint/query, fetched_at, source vintage, returned count (or opaque byte count), empty/error semantics and licensing limits. Layer and land-value responses do not create snapshots. Only when the user asks to keep a tile, pass `save_artifact: true` (PBF only) to `reinfolib_layer`: it writes the exact raw PBF bytes plus metadata as a private export; report artifact path, sha256 and that it is raw Mapbox Vector Tile data, not GeoJSON, deleted after 30 days. Transactions records/export tools do not read tiles. No property appraisal, installation or scheduling is performed by this workflow.

## Layers at one address (geocoded overlay)

Use tool_search to find `reinfolib_point_layers` (and `reinfolib_geocode` to preview candidates) before web_search when the user asks which zoning, fire-prevention, flood or landslide layers apply at an address.

1. Call `reinfolib_point_layers` with `address`, up to six tile `endpoints` and an explicit `tolerance_m` (0–100, default 10). The address goes to the 国土地理院 address search without the API key; credit 出典：国土地理院ウェブサイト and say this plugin processed it.
2. `ambiguous_location` lists candidates and picks none: show their titles and ask, then retry with the chosen `candidate_id`. `location_not_reliable` means GSI matched only chome or coarser; ask for a more specific address and do not guess coordinates.
3. The location is a geocoded address representative point (often a block), not a parcel or building footprint. State this with every result.
4. Report every layer separately: `relation` (inside, within_tolerance, outside, no_features_in_tile, not_covered, failed), `layer_kind` (legal designation vs hazard estimate vs facility/statistics; a plugin classification), source vintage, commercial listing and coverage note. within_tolerance means the boundary or feature is within tolerance_m, so inside/outside is not decided.
5. no_features_in_tile, outside, not_covered and failed are never safe and never prove that no designation or hazard exists. A geometric relation is not a legal determination; tell the user to confirm designations with the responsible authority.
6. XPT001 is refused (nearest-station points), XPT002 needs filters and is refused, purpose=commercial fails closed. Do not draw property-level conclusions from transaction records.
