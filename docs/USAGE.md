# Usage

14 tools. All codes are strings and keep leading zeros. Every result carries `attribution`, `processing` and the mandated API `credit`; keep them when you present results.

## Tools

| Tool | Purpose | Network |
| --- | --- | --- |
| `reinfolib_quarter` | Legacy: one city, one quarter, 12 columns, 20-row excerpt | XIT001 |
| `reinfolib_transactions` | XIT001 by `area`, `city` or `station`; year with optional quarter; `01`/`02`/`both`; `ja`/`en`. Saves a full raw snapshot and returns the first page | XIT001 |
| `reinfolib_records` | Filter, page and summarize a saved snapshot | local |
| `reinfolib_export` | Export every matched record of a snapshot as JSON or CSV | local |
| `reinfolib_snapshots` | List saved snapshots for the same query | local |
| `reinfolib_catalog` | The 36 documented endpoints, parameters, zoom and status | local |
| `reinfolib_municipalities` | XIT002 municipality list and name candidates | XIT002 |
| `reinfolib_stations` | Packaged N02-25 station directory: name or code to N02_005g candidates | local |
| `reinfolib_history` | Bounded backward probe for the newest published quarter | XIT001 + site notices |
| `reinfolib_diff` | Compare two snapshots of the same query | local |
| `reinfolib_land_values` | XCT001 appraisal background or XPT002 land-price tiles | XCT001/XPT002 |
| `reinfolib_layer` | One raw tile from 33 layer endpoints (GeoJSON or PBF) | tile APIs |
| `reinfolib_geocode` | 国土地理院 address search candidates | GSI, no key |
| `reinfolib_point_layers` | Per-layer overlay at one geocoded point | GSI + tile APIs |

## Transactions

```json
{"station":"003696","year":2024,"quarter":1,"price_classification":"both","limit":20}
{"city":"13102","year":2015,"price_classification":"both"}
```

- Give exactly one of `area` (2 digits), `city` (5 digits) or `station` (6-digit N02_005g group code). Omit `quarter` for a full year; do not pass null.
- `price_classification` defaults to `01`. `02` starts in 2021. `both` omits the upstream parameter; the two sources can contain the same sale, so counts are API-covered records, not unique transactions.
- A station HTTP 400 returns `fallback_required`. The plugin never widens the area on its own.
- HTTP 404 is `not_found`. It is not zero sales and says nothing about publication status.
- Responses over 8 MB return `response_too_large`; nothing is saved or truncated. Use a quarter or a smaller area.
- Records have no building names, exact addresses or property coordinates.

Page, filter and export the same snapshot:

```json
{"snapshot_id":"<32 hex>","offset":20,"limit":20}
{"snapshot_id":"<32 hex>","filters":{"type":"中古マンション等","area_min":"30","area_max":"50","building_year_min":2000}}
{"snapshot_id":"<32 hex>","format":"csv"}
```

- Text filters (`district_name`, `type`, `floor_plan`, `price_category`, `period`) are exact matches. Numeric bounds are inclusive decimal strings; building years are integers. Western years and Meiji–Reiwa era years are parsed.
- Values such as `2000㎡以上` or `戦前` are not compared. `filter_diagnostics` reports `did_not_match_n`, `unparseable_n` and `missing_n` per range field.
- Statistics cover all matched records, grouped by Type, PriceCategory and Period, with exact Decimal medians.
- Exports contain every matched record. CSV is UTF-8 with BOM; cells starting with `=`, `+`, `-` or `@` are escaped. The plugin chooses the output path.

## Municipalities and stations

```json
{"area":"14","name":"緑区"}
{"name":"西新宿五丁目"}
```

Both tools return every candidate and never pick one. Station codes change between N02 versions; a code missing from the API returns 400. The station index is built from the official N02-25 ZIP (SHA-256 `aaf76af133b2e771e538fabc4646d2e443dc1d5a67b221382a28d744e706cc9f`). To rebuild it from a newer ZIP:

```sh
python scripts/build_station_index.py <zip> --dataset N02-25 --sha256 <sha256>
```

## History and diff

```json
{"city":"13102","max_quarters":8}
{"base_snapshot_id":"<32 hex>","target_snapshot_id":"<32 hex>"}
```

- `reinfolib_history` walks back from the newest started quarter (up to 20 quarters, default 90 s total deadline) and stops at the first published quarter. Outcomes: `found`, `none_found_in_range`, `failed`, `budget_exhausted`. 404 results are cached for 6 hours; `use_cache:false` refetches.
- It first reads the official site notices (no key). That feed is an internal site endpoint and returns `unavailable` if its format changes.
- `reinfolib_diff` compares record multisets. `possible_revisions` is a heuristic pairing. Report changes as API records added or removed, not as new sales.
- The `weekly-history-check` blueprint uses these two tools. Nothing is scheduled until you schedule it.

## Land values and layers

```json
{"endpoint":"XKT002","query":{"response_format":"geojson","z":14,"x":14339,"y":6505}}
{"endpoint":"XCT001","query":{"year":2025,"area":"47","division":"09"}}
```

- Each endpoint validates its own zoom, filters and years; `reinfolib_catalog` lists them.
- GeoJSON is returned as received. PBF is returned as base64 unless `decode_pbf: true`, which adds a simplified decoded view.
- `save_artifact: true` (PBF only) also writes the raw tile bytes and a metadata file to the export directory.
- An empty HTTP 200 tile is `no_features`, not safe.
- XPT001 points are nearest-station locations. Do not use them for property-level hazard, school-zone or distance judgments.
- Source vintages and licensing differ by layer and region. `purpose: "commercial"` is refused until regional terms have been reviewed.

## Geocoding and point overlay

```json
{"address":"東京都新宿区西新宿2-8-1"}
{"address":"東京都新宿区西新宿2-8-1","endpoints":["XKT002","XKT026"],"tolerance_m":10}
```

- Geocoding returns every candidate with `matched_level` (house, block, chome, area_or_place). Coordinates are an address representative point, not a parcel.
- Overlay requires block or house precision; otherwise it returns `location_not_reliable` or `ambiguous_location`.
- Each layer reports `inside`, `within_tolerance`, `outside`, `no_features_in_tile`, `not_covered` or `failed`. None of these is a legal determination or proof of safety.
- The GSI search is intended mainly for 地理院地図 and may change without notice.

## Requests

- The API key is read through the Hermes secret scope and sent only as a header to `https://www.reinfolib.mlit.go.jp`.
- Requests share a per-profile, cross-process gate with at least 1 second between requests. This is a local policy; MLIT publishes no numeric limit.
- Up to 3 attempts within 90 seconds. 429 honours Retry-After; 500/502/503/504 and network errors back off. 400/401/403/404 are not retried.
- Redirects are refused and environment proxies are ignored.

## Storage

- Snapshots and exports live under `<HERMES_HOME>/plugin-data/jp-reinfolib/v2/`, mode 0700/0600, and are readable by anyone using the same profile.
- They are deleted 30 days after being written. Early deletion is a terminal command only:

```sh
hermes jp-reinfolib delete snapshot <uuid> --yes
hermes jp-reinfolib delete export <uuid> --yes
```

  It is not a tool or slash command, so gateway chats cannot call it. An agent with a terminal tool can still run it.
- Storage uses POSIX APIs and has not been tested on Windows.

## Updating

```sh
hermes plugins install <source> --ref <40-char sha> --force --enable
hermes gateway restart
```

Use `--force` to replace an installed copy. `hermes plugins remove` also removes the plugin's toolset from every platform list in `config.yaml`.

## Tests

```sh
python -m pytest -q
hermes plugins doctor "$PWD" --ci
hermes plugins validate "$PWD" --json
```

出典：国土交通省 不動産情報ライブラリ https://www.reinfolib.mlit.go.jp/

「このサービスは、国土交通省の不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません」
