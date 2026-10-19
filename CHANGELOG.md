# Changelog

## 0.1.1

The first sentence Tool Search shows now uses the words a person would ask with. A sentence that starts by describing the mechanism is not found, and the model falls through to the built-in web_search.

The `weekly-city-check` skill's description now tells the model to find `reinfolib_quarter` with tool_search before web_search. Tests check that the skill stays in the model's skill list when Tool Search is left at its default.

A year or quarter written as 10.0 or fullwidth digits, a fullwidth city code, a fullwidth price class `01`, and language in any letter case are accepted. The refusal of an unknown argument now says to remove it and gives an example (13102, 2015, and 2). A municipality name was already refused as not a code.

## 0.1.0

Read-only access to MLIT transaction prices (XIT001, price class `01`, language `ja`) through one tool, `reinfolib_quarter`.

Included:

- One GET per call, for a city code, year, and quarter you pass in.
- A fixed JSON result: count, a 20-row excerpt, the quarter label, the source line, and the processing line.
- HTTP 404 as "no data for this request", which is not a count of zero and not a statement that prices are unchanged.
- An Automation Blueprint, `jp-reinfolib:weekly-city-check`, only when Hermes has `register_automation_blueprint`. The blueprint does not run until you schedule it.
- A bundled skill with the same weekly steps for builds that do not have that registrar.

Not included, and why:

- No watcher, no cron job created by this plugin, and no saved file. The previous shape stored a failure per city and created a cron job. Audits kept finding harm in that state: a failure record crossed cities, the job survived uninstall, the job's toolsets could gain MCP servers, and a different `HERMES_HOME` made the printed commands look at another home. Those bugs came from the plugin owning the job and the file. Version 0.1 does not.
- No contract prices (`02`), land prices, tiles, or a municipality list.
- No unit conversion. A recorded `TradePrice` stays a digit string, or `{"kind": "not_digits"}` when it is not. `median_trade_price` is a separate summary of the digit prices.
- No claim that a row is new. The plugin does not remember a previous count.

`requires_hermes` is `>=0.21.4` because the read tool loads there. The blueprint is registered only when `register_automation_blueprint` exists, and skipped when it does not. `agent.secret_scope.get_secret` is on that same floor.

Weekly prompt:

- Hermes prepends a cron instruction that tells the model to answer exactly `[SILENT]` when there is nothing to report, and then does not deliver that run. The blueprint used to say "Never answer [SILENT]", which fights that instruction. A week whose reads are all HTTP 404 may now be exactly `[SILENT]`. A week with `ok` true is one short message and is not `[SILENT]`. A failure is named and is not `[SILENT]`. Canary builds from 2026-10-06 already have `register_automation_blueprint` even when the version number is below 0.21.6, so public text no longer says that every build before v0.21.6 skips the blueprint.

Designated cities:

- The 20 city-level codes on the Ministry of Internal Affairs sheet 「R6.1.1政令指定都市」 (2024-01-01) are refused before a request. `error_type` is `bad_city`, `problem` is `designated_city`, and the expected text says to use a ward code (example 14101). The submitted code is not copied. A ward code such as 14101 is still accepted.

City code, excerpt, and odd rows:

- `city` is described as a 5-digit national local-government code. Example: 13104 is Shinjuku-ku. A municipality name is `bad_city`. The submitted text is not copied. Every argument error includes `field`, `problem`, `expected`, and `allowed_fields`.
- A successful result includes `fetched_at` (UTC, second resolution), `count_by_type` over the whole quarter, `median_trade_price`, and `median_trade_price_by_type`. Medians use non-empty digit `TradePrice` strings. An even count uses the mean of the two middle values, with `.5` when the sum is odd. A type with no digit price is left out of the by-type median.
- The XIT001 manual types area and price columns as strings. A non-digit value is `{"kind": "not_digits"}` and is not copied. A 5-digit municipality code other than the one requested is `{"kind": "other_municipality", "code": "..."}`. Those fields do not discard the quarter. `Period` and `PriceCategory` still have to match.
- The weekly blueprint says a name is not a code, and that publication lags the quarter so many weeks have no new rows.

Missing key:

- `detail` `missing_key` names `REINFOLIB_API_KEY`. A bad origin and a key that would appear in the URL keep the shorter config sentence.

Tool result:

- Hermes `registry.dispatch` keeps a string and replaces any other handler result with `tool_result_contract`. `reinfolib_quarter` was returning a dict, so the model never saw `count` or `records`. The handler now returns the same JSON string the slash command returns. An HTTP 404 for a well-formed city code with no published rows uses that same fixed sentence, and the two cases are not separated.

Credential read:

- One Hermes process can serve more than one profile, and `os.environ` can hold another profile's key. `REINFOLIB_API_KEY` and `REINFOLIB_BASE_URL` are read with `get_secret` inside the call, not from `os.environ`. An unset key is still `error_type` `config` and the same fixed sentence. `UnscopedSecretError` is not caught. The HTTP opener passes an empty `ProxyHandler`, so a proxy named in the environment does not receive the key header.
