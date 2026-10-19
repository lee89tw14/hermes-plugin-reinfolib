---
name: weekly-city-check
description: Before web_search, use tool_search to find reinfolib_quarter and tool_call to read MLIT real estate transaction prices for one city quarter. Weekly XIT001 read. Does not create a cron job.
---

When someone asks for municipal real-estate transaction prices, use tool_search to find reinfolib_quarter before web_search, then call it with tool_call. Write tool_search queries in English (for example: "real estate prices").

# Weekly city transaction check

Use this when someone wants a weekly look at one city's transaction prices from XIT001.

The plugin tool is `reinfolib_quarter`. Call that tool only. Do not call any other tool for this check.

1. Take the city as five ASCII digits, not the municipality name. The first two digits are 01 through 47. Example: 13104 is Shinjuku-ku. A designated city needs a ward code, not the city code. Example: 14101, not 14100. If the call is `bad_city`, say to use a ward code. This plugin does not look names up. Publication lags the quarter, so the latest started quarter is often HTTP 404.
2. Use the computer's local date. A quarter starts in January, April, July, or October. Skip a quarter whose first month is still ahead of today. The year 2005 has quarters 3 and 4 only.
3. Call `reinfolib_quarter` for the latest started quarter, then once more for the quarter before it. Pass `city`, `year`, and `quarter`. `price_classification` is `01`. `language` is `ja`.
4. Read the JSON. `ok` true means `count` is the full length and `records` is at most 20 of these columns only: Type, Region, MunicipalityCode, Prefecture, Municipality, DistrictName, TradePrice, PricePerUnit, Area, UnitPrice, Period, PriceCategory. Say `period`, `count`, and `truncated`. Include `attribution` and `processing` unchanged.
5. `error_type` `not_found` (HTTP 404) means no data for that city, year, and quarter. It is not zero rows and it is not unchanged prices. A city code of the right shape with no published rows is the same 404, and those two cases are not separated.
6. Any other `error_type` is a failed check. Name `error_type` and `status_code`. Do not describe a failure as no change.
7. Do not say the rows are new. This plugin does not remember a previous count. If every call is `not_found`, Hermes may suppress delivery when the answer is exactly `[SILENT]` and nothing else. If any call has `ok` true, send one short message and do not use `[SILENT]`. A failure is not `[SILENT]`.

The same steps are the Automation Blueprint `jp-reinfolib:weekly-city-check` (default Monday 09:00) only when Hermes has `register_automation_blueprint`. Scheduling that blueprint is what creates the cron job. This plugin does not create one. When that method is missing, these steps are the procedure and the blueprint is skipped.

「このサービスは、国土交通省の不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません」
