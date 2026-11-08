# jp-reinfolib

Hermes Agent plugin for the MLIT 不動産情報ライブラリ API (XIT001/XIT002 transactions, XCT001/XPT002 land values, 33 tile layers), with a 国土地理院 address-search geocoder. 14 tools, standard library only, MIT.

Based on the original `jp-reinfolib` 0.1.1 by TakeshiTGAL (https://github.com/TakeshiTGAL/hermes-plugin-reinfolib); the legacy `reinfolib_quarter` tool, slash command and weekly blueprint are kept unchanged.

- Tools and examples: `docs/USAGE.md`
- Endpoint index used at runtime: `reinfolib/data/endpoint-inventory.json`
- Changes: `CHANGELOG.md`

Install from a Git source and pin a commit:

```bash
hermes plugins install <git-url-or-owner/repo> --ref <40-char commit sha> --enable
hermes gateway restart
```

Tests: `python -m pytest -q`.

## Legacy tool: `reinfolib_quarter`

The rest of this page describes the original `reinfolib_quarter` tool. Statements below about a single tool, no saved files and `REINFOLIB_BASE_URL` apply to that tool only; see `docs/USAGE.md` for the others.

Read one city's MLIT transaction prices for one year and one quarter. The tool is `reinfolib_quarter`. It calls XIT001 only (`priceClassification` `01`, language `ja`). It does not create a cron job, and it does not write a file.

「このサービスは、国土交通省の不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません」

A reply that includes a count or an excerpt also includes:

出典：国土交通省　不動産情報ライブラリ
（https://www.reinfolib.mlit.go.jp/）

「不動産取引価格情報」（国土交通省）をもとに件数と抜粋を作成

The only site link is that top page. The product name and the tool name do not use the Japanese name 不動産情報ライブラリ as a product name. Site terms article 3 (7) do not allow that name for a product, service, or application unless MLIT approves it. The identifier `jp-reinfolib` is the host token. MLIT has not been asked about this.

## Quick start

Set `REINFOLIB_API_KEY` in the Hermes process environment or in the profile's `.env`; the plugin reads it through the Hermes secret scope. An empty value is not sent. The key is the header `Ocp-Apim-Subscription-Key` and is not placed in the URL.

`HERMES_HOME` selects the Hermes home you are talking to. A command with no `HERMES_HOME` and no `-p` uses that process's default home. This plugin does not take `-p` or `--profile`. Those are Hermes flags.

```bash
hermes plugins install <owner/repo>
hermes plugins enable jp-reinfolib
```

Ask for `reinfolib_quarter` with `city`, `year`, and `quarter`. `city` is five ASCII digits. The first two digits are `01`–`47`. Example: `13104` is Shinjuku-ku. A designated city needs a ward code, not the city code. Example: `14101` is Tsurumi-ku, Yokohama, not `14100`. Those 20 city codes are the rows without 区 on the Ministry of Internal Affairs sheet 「R6.1.1政令指定都市」 in the 2024-01-01 workbook https://www.soumu.go.jp/main_content/000925835.xlsx. The API uses the first five digits of that six-digit code. A designated-city code is refused before any request, with `error_type` `bad_city` and `problem` `designated_city`. The submitted code is not copied. A municipality name is refused as `invalid_argument` / `bad_city` before any request, and the name is not copied into the error. The error names `field`, `problem`, `expected`, and `allowed_fields`. This plugin does not look names up. MLIT XIT002 lists municipality codes. Years run from 2005 through the current year. 2005 allows quarters 3 and 4 only. A quarter whose first month is still ahead of today is refused before any request. `price_classification` defaults to `01` and `language` defaults to `ja`. Any other value, or any other argument, is refused before any request, in the same `field` / `problem` / `expected` / `allowed_fields` shape.

Slash form, which does not schedule anything:

```text
/jp-reinfolib 13102 2015 2
```

## What a call returns

`ok` true means `count` is the full length of the official rows and `records` is the first 20 of those rows, keeping only these 12 columns: Type, Region, MunicipalityCode, Prefecture, Municipality, DistrictName, TradePrice, PricePerUnit, Area, UnitPrice, Period, PriceCategory. Official columns that are not in that list are dropped, including FloorPlan, BuildingYear, Structure, Use, Purpose, Remarks, and DistrictCode. `truncated` is true when `count` is greater than 20. `period` is `{year}年第{quarter}四半期`. `fetched_at` is UTC when this result was built, `YYYY-MM-DDTHH:MM:SSZ`. It is not the upstream publication time.

`count_by_type` counts `Type` over every row in the quarter, not over the 20-row excerpt. `median_trade_price` is computed from `TradePrice` values that are non-empty ASCII digit strings, read as integers, over the whole quarter. Those values are sorted. An odd count uses the middle value. An even count uses the mean of the two middle values. When that mean is a half, the string ends in `.5`. It is not a binary float. Empty prices and values that are not digit strings are left out. If none remain, `median_trade_price` is null. `median_trade_price_by_type` uses that same rule once per `Type`. A type with no digit price is left out.

The XIT001 manual types `Area`, `TradePrice`, `PricePerUnit`, and `UnitPrice` as strings. The published example for `Area` is `80`. A value that is not an ASCII digit string is returned as `{"kind": "not_digits"}` and the original text is not copied. One such field does not discard the quarter. A `MunicipalityCode` of the right 5-digit shape that is not the requested code is `{"kind": "other_municipality", "code": "..."}`. The manual does not say that a designated-city code returns ward codes. A code that is not 5 digits is `{"kind": "not_code"}`, and that text is not copied. `Period` or `PriceCategory` that does not match the request still discards the whole body. There is no unit conversion.

`error_type` `not_found` with `status_code` 404 means no data for that city, year, and quarter. It is not zero rows and it is not unchanged prices. The same 404 is what a ward code of the right shape but no published rows returns, so those two cases are not separated. A designated-city code is refused before that request.

Other failures use a fixed English sentence per `error_type`, plus `status_code` when the server sent one. The upstream body is not copied. `detail` is one of a fixed set of words (`bad_city`, `future_quarter`, `empty_data`, and the rest listed in the tool result). When `detail` is `missing_key`, the sentence names `REINFOLIB_API_KEY`. A bad origin and a key that would appear in the URL stay on the shorter config sentence.

Each request stops if no bytes arrive for 30 seconds. There is no separate deadline for the whole call, because a call is one GET. Nothing is retried. The decoded body cap is 8000000 bytes. gzip is decoded only when `Content-Encoding` is `gzip`.

After a GET returns, the same process waits 1 second before it starts another. The first GET does not wait. Another process does not share that wait. The wait is not stored in a file.

Plain `http` is refused before the key is sent, except for `127.0.0.1` and `localhost`. On that loopback path the key is not encrypted. Redirects are not followed, so a 301, 302, 303, 307, or 308 does not send the key to the next host. A proxy set in the environment is not used, so the key is not sent to it.

`REINFOLIB_BASE_URL`, when set, is an origin only: scheme, host, and optional port. The path is always `/ex-api/external/XIT001`.

## Who can call it

The tool is on for every surface, including gateways. Anyone you allow on a gateway can call it with your key. There is no separate user list and no approval prompt. A read still runs under cron, yolo, approvals off, and unattended sessions. There is no daily cap. MLIT has not published a numeric request cap.

Stop the tool with `hermes tools`. That switch does not stop `/jp-reinfolib`. Stop the slash command with `allow_admin_from`, `user_allowed_commands`, `group_user_allowed_commands`, or by disabling the plugin. A room's `group_user_allowed_commands` can also apply to DMs.

While this plugin is enabled, its `weekly-city-check` skill adds one line to the skills index of every session on every platform. That line stays on a platform where `hermes tools` turned the tool off; the skill can still be read there, but `reinfolib_quarter` is unavailable. The model in any session, including one a gateway admits a guest to, can load the skill. The skill is instructions only, sends nothing, and does not create a job. `hermes plugins disable jp-reinfolib` removes the tool, `/jp-reinfolib`, the blueprint, and the skill together; a job you already scheduled stays until `hermes cron remove`.

## Weekly check

The Automation Blueprint `jp-reinfolib:weekly-city-check` is registered only when Hermes has `register_automation_blueprint`. Otherwise registration is skipped. The city slot asks for a 5-digit code, not a municipality name. Example: `13104` is Shinjuku-ku. A designated city needs a ward code, not the city code. The default is Monday at 09:00, once a week, in Hermes's timezone or the computer's clock if none is set. It calls only `reinfolib_quarter`, for the latest started quarter and the quarter before it. Publication lags the quarter, so the latest started quarter is often HTTP 404, and many weeks both reads have no data. It does not say that rows are new, because this plugin does not remember a previous count. Hermes puts a cron instruction in front of the job prompt. That instruction says to answer exactly `[SILENT]` when there is nothing to report, and Hermes then does not deliver the run. If every read is HTTP 404, this check may answer `[SILENT]` and the message may not arrive. If a read has data, the run is one short message with `period`, `count`, and `truncated`. A failure is not answered as `[SILENT]`.

This plugin does not create a cron job. The job exists only after you schedule the blueprint. Scheduling it twice creates two jobs. `hermes plugins remove` does not remove a job you already scheduled. Stop that job with `hermes cron remove` on its id (and Hermes's `-p` when the profile is not the default). The blueprint registrar takes no toolset list, so a job you create from it follows Hermes's cron rules and may include MCP servers you have enabled.

When `register_automation_blueprint` is missing, the tool still loads. The bundled skill `weekly-city-check` states the same steps and does not create a job.

A scheduled run is one model turn. A turn that calls the tool is at least two model requests.

## Update

See `docs/USAGE.md` (Updating). Pinned installs are not changed by `hermes plugins update`.

## Key handling

The key is compared, as text and as any 12-character span, against the JSON that would be returned, including one percent-decode and one `\uXXXX` decode. If it matches, the whole result is replaced with `error_type` `internal` and a fixed sentence. A twice-encoded value is outside that check.

## Checked on the live API

On 2026-10-12, city `13102`, year 2015, quarter 2 returned HTTP 200 and 176 rows after gzip. City `01999` for 2015 quarter 2, and city `13102` for 2026 quarter 3, returned HTTP 404. Those calls were at least 1 second apart. Upstream HTTP 429 was not returned. A year outside 2005 through the current year is refused before a request, so it has no upstream status code. The success fixture in this repository keeps the live column names and drops district, price, and area values.

Not checked: contract prices (`02`), land prices, tile APIs, the top-page notices, and a slash command sent from a real gateway room. Rows added later inside a quarter you already read are not something this plugin tracks. Site terms article 5 (3) say that can happen.

## License

MIT. The plugin is standard library only. `python_dependencies` is empty.
