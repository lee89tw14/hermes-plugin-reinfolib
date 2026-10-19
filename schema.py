"""One table for arguments, upstream columns, and returned columns.

Validation walks this table. Tests are generated from it. A type with no
accept and reject examples fails those tests.
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
from datetime import date
from typing import Any, Callable

if __package__:
    from .messages import ATTRIBUTION, CREDIT, ERRORS, MISSING_KEY, PRICE_CATEGORY, PROCESSING, TOP_URL
else:
    from messages import ATTRIBUTION, CREDIT, ERRORS, MISSING_KEY, PRICE_CATEGORY, PROCESSING, TOP_URL

YEAR_MIN = 2005
RECORD_CAP = 20
TEXT_MAX = 200
DIGIT_MAX = 24
MIN_GAP_SECONDS = 1
TIMEOUT_SECONDS = 30
BODY_CAP = 8_000_000

Ctx = dict[str, Any]
Check = Callable[[Any, Ctx], bool]


def _ascii_digits(value: str) -> bool:
    return bool(value) and all("0" <= ch <= "9" for ch in value)


# City-level codes only. First 5 digits of the 6-digit code on the Ministry of
# Internal Affairs sheet "R6.1.1政令指定都市" (2024-01-01):
# https://www.soumu.go.jp/main_content/000925835.xlsx
# Tokyo 13100 is the special-ward area and is not in these 20.
DESIGNATED_CITY_CODES = frozenset({
    "01100", "04100", "11100", "12100", "14100", "14130", "14150", "15100",
    "22100", "22130", "23100", "26100", "27100", "27140", "28100", "33100",
    "34100", "40100", "40130", "43100",
})


def check_city(value: Any, _ctx: Ctx) -> bool:
    if not isinstance(value, str) or len(value) != 5 or not _ascii_digits(value):
        return False
    prefix = int(value[:2])
    return 1 <= prefix <= 47


def is_designated_city(value: Any) -> bool:
    return isinstance(value, str) and value in DESIGNATED_CITY_CODES


def check_year(value: Any, ctx: Ctx) -> bool:
    today: date = ctx["today"]
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return YEAR_MIN <= value <= today.year


def check_quarter(value: Any, _ctx: Ctx) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 1 <= value <= 4


def check_price_class(value: Any, _ctx: Ctx) -> bool:
    return value == "01"


def check_language(value: Any, _ctx: Ctx) -> bool:
    return value == "ja"


def check_text(value: Any, _ctx: Ctx) -> bool:
    if not isinstance(value, str) or len(value) > TEXT_MAX:
        return False
    return all(unicodedata.category(ch) not in {"Cc", "Cf"} for ch in value)


def check_digits(value: Any, _ctx: Ctx) -> bool:
    if not isinstance(value, str) or len(value) > DIGIT_MAX:
        return False
    return all("0" <= ch <= "9" for ch in value)


def check_period(value: Any, ctx: Ctx) -> bool:
    return value == f"{ctx['year']}年第{ctx['quarter']}四半期"


def check_price_category(value: Any, _ctx: Ctx) -> bool:
    return value == PRICE_CATEGORY


# type name -> check, examples, JSON schema type. Examples are the test generator's input.
TYPES: dict[str, dict[str, Any]] = {
    "city_code": {
        "check": check_city,
        "json": "string",
        "accept": ["13102", "01000", "47000"],
        "reject": ["1310", "131021", "99999", "00101", "13102 ", " 13102", "１３１０２", "13a02", "", None, 13102, True],
    },
    "year": {
        "check": check_year,
        "json": "integer",
        "accept": [2005, 2015, 2026],
        "reject": [True, False, 2004, 2027, "2015", 2015.0, None],
    },
    "quarter": {
        "check": check_quarter,
        "json": "integer",
        "accept": [1, 2, 3, 4],
        "reject": [0, 5, True, False, "2", 2.0, None],
    },
    "price_class": {
        "check": check_price_class,
        "json": "string",
        "accept": ["01"],
        "reject": ["02", "1", "01 ", 1, None, True],
    },
    "language": {
        "check": check_language,
        "json": "string",
        "accept": ["ja"],
        "reject": ["en", "JA", "ja ", None, True],
    },
    "text": {
        "check": check_text,
        "json": "string",
        "accept": ["", "商業地", "1972年"],
        "reject": [None, 1, "x" * (TEXT_MAX + 1), "a\nb", "a\u200bb"],
    },
    "digits": {
        "check": check_digits,
        "json": "string",
        "accept": ["", "0", "85000000"],
        "reject": [None, 80, "80.0", "-1", "８０", "80 ", True],
    },
    "period": {
        "check": check_period,
        "json": "string",
        "accept": ["2015年第2四半期"],
        "reject": ["2015年第3四半期", "2015年 第2四半期", None, 2015],
    },
    "price_category": {
        "check": check_price_category,
        "json": "string",
        "accept": [PRICE_CATEGORY],
        "reject": ["成約価格情報", PRICE_CATEGORY + " ", None, True],
    },
}

# Frozen clock for the examples above. 2026-10-12 is inside Q4, so 2026 is in range.
EXAMPLE_TODAY = date(2026, 10, 12)
EXAMPLE_CTX: Ctx = {"today": EXAMPLE_TODAY, "city": "13102", "year": 2015, "quarter": 2}

ARGUMENTS: tuple[dict[str, Any], ...] = (
    {"name": "city", "type": "city_code", "required": True},
    {"name": "year", "type": "year", "required": True},
    {"name": "quarter", "type": "quarter", "required": True},
    {"name": "price_classification", "type": "price_class", "required": False},
    {"name": "language", "type": "language", "required": False},
)

# Returned columns. Upstream keys that are not listed here are dropped.
# MunicipalityCode, Period, and PriceCategory must match the request.
COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "Type", "type": "text", "sample": "中古マンション等"},
    {"name": "Region", "type": "text", "sample": ""},
    {"name": "MunicipalityCode", "type": "city_code", "equals": "city", "sample": "13102"},
    {"name": "Prefecture", "type": "text", "sample": "東京都"},
    {"name": "Municipality", "type": "text", "sample": "中央区"},
    {"name": "DistrictName", "type": "text", "sample": ""},
    {"name": "TradePrice", "type": "digits", "sample": "0"},
    {"name": "PricePerUnit", "type": "digits", "sample": ""},
    {"name": "Area", "type": "digits", "sample": "0"},
    {"name": "UnitPrice", "type": "digits", "sample": ""},
    {"name": "Period", "type": "period", "sample": "2015年第2四半期"},
    {"name": "PriceCategory", "type": "price_category", "sample": PRICE_CATEGORY},
)

# year, quarter, today. 2005 Q1–Q2 and a not-yet-started quarter are refused.
PAIRS: tuple[dict[str, Any], ...] = (
    {"today": date(2026, 10, 12), "year": 2015, "quarter": 2, "ok": True},
    {"today": date(2026, 10, 12), "year": 2005, "quarter": 3, "ok": True},
    {"today": date(2026, 10, 12), "year": 2005, "quarter": 4, "ok": True},
    {"today": date(2026, 10, 12), "year": 2026, "quarter": 4, "ok": True},
    {"today": date(2026, 10, 12), "year": 2005, "quarter": 1, "ok": False},
    {"today": date(2026, 10, 12), "year": 2005, "quarter": 2, "ok": False},
    {"today": date(2026, 2, 15), "year": 2026, "quarter": 2, "ok": False},
    {"today": date(2026, 2, 15), "year": 2026, "quarter": 1, "ok": True},
)

ARG_DETAILS = {
    "city": "bad_city",
    "year": "bad_year",
    "quarter": "bad_quarter",
    "price_classification": "bad_price_classification",
    "language": "bad_language",
}


def quarter_started(year: int, quarter: int, today: date) -> bool:
    start_month = (quarter - 1) * 3 + 1
    if year == 2005 and quarter < 3:
        return False
    if year == today.year and start_month > today.month:
        return False
    return True


def _check(type_name: str, value: Any, ctx: Ctx) -> bool:
    return bool(TYPES[type_name]["check"](value, ctx))


def _relax(name: str, value: Any) -> Any:
    """Shapes a model can mistype, and that map to one value. Names are not codes."""
    if name in {"year", "quarter"}:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            folded = unicodedata.normalize("NFKC", value).strip()
            if any(ord(ch) > 127 for ch in value) and folded.isdigit():
                return int(folded)
        return value
    if name == "city" and isinstance(value, str):
        folded = unicodedata.normalize("NFKC", value).strip()
        if folded.isdigit() and len(folded) == 5:
            return folded
    if name == "language" and isinstance(value, str) and value.strip().lower() == "ja":
        return "ja"
    if name == "price_classification" and isinstance(value, str):
        folded = unicodedata.normalize("NFKC", value).strip()
        if folded == "01":
            return "01"
    return value


def validate_arguments(raw: Any, today: date) -> tuple[dict[str, Any] | None, str | None]:
    """Return (parsed, detail). detail is a finite word, or None when parsed is set."""
    if not isinstance(raw, dict):
        return None, "not_object"
    known = {row["name"] for row in ARGUMENTS}
    unknown = [key for key in raw if key not in known]
    if unknown:
        return None, "unknown_argument"
    parsed: dict[str, Any] = {}
    for row in ARGUMENTS:
        if row["name"] not in raw:
            if row["required"]:
                return None, "missing_argument"
            continue
        value = _relax(row["name"], raw[row["name"]])
        if row["name"] == "city" and is_designated_city(value):
            return None, "designated_city"
        ctx = {"today": today, "city": parsed.get("city", ""), "year": parsed.get("year", 0), "quarter": parsed.get("quarter", 0)}
        if not _check(row["type"], value, ctx):
            return None, ARG_DETAILS[row["name"]]
        parsed[row["name"]] = value
    parsed.setdefault("price_classification", "01")
    parsed.setdefault("language", "ja")
    if parsed["year"] == 2005 and parsed["quarter"] < 3:
        return None, "bad_quarter"
    if not quarter_started(parsed["year"], parsed["quarter"], today):
        return None, "future_quarter"
    return parsed, None


def _cell(column: dict[str, Any], value: Any, ctx: Ctx) -> tuple[Any, str | None]:
    """One field, or a whole-body detail when the row is not this request.

    The XIT001 manual types Area and the price columns as strings. A non-digit
    string is marked and kept. It is not copied. A municipality code of the
    right shape that is not the requested code is marked and kept. Period and
    price category must still match, or the whole body is refused.
    """
    type_name = column["type"]
    if type_name in {"period", "price_category"}:
        if not _check(type_name, value, ctx):
            return None, "mismatched_record"
        return value, None
    if column["name"] == "MunicipalityCode":
        if _check("city_code", value, ctx) and value == ctx["city"]:
            return value, None
        if _check("city_code", value, ctx):
            return {"kind": "other_municipality", "code": value}, None
        return {"kind": "not_code"}, None
    if type_name == "digits":
        if _check("digits", value, ctx):
            return value, None
        return {"kind": "not_digits"}, None
    if type_name == "text":
        if _check("text", value, ctx):
            return value, None
        return {"kind": "not_text"}, None
    if not _check(type_name, value, ctx):
        return None, "bad_record"
    return value, None


def project_body(payload: bytes, parsed: dict[str, Any], today: date) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        body = json.loads(payload.decode("utf-8"))
    except Exception:
        return None, "not_json"
    if not isinstance(body, dict):
        return None, "not_object"
    if "statusCode" in body or body.get("status") != "OK":
        return None, "not_ok"
    data = body.get("data")
    if not isinstance(data, list):
        return None, "not_object"
    if len(data) == 0:
        return None, "empty_data"
    ctx = {"today": today, "city": parsed["city"], "year": parsed["year"], "quarter": parsed["quarter"]}
    records: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            return None, "bad_record"
        projected: dict[str, str] = {}
        for column in COLUMNS:
            if column["name"] not in item:
                return None, "bad_record"
            value, problem = _cell(column, item[column["name"]], ctx)
            if problem is not None:
                return None, problem
            projected[column["name"]] = value
        records.append(projected)
    return records, None


def trade_median(records: list[dict[str, Any]]) -> str | None:
    """Middle TradePrice. Empty and non-digit prices are left out.

    Odd count: the middle value. Even count: the mean of the two middle
    values. A half is written as a trailing .5. The result is not a float.
    """
    values: list[int] = []
    for row in records:
        price = row.get("TradePrice")
        if isinstance(price, str) and price != "" and check_digits(price, EXAMPLE_CTX):
            values.append(int(price))
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return str(values[mid])
    left, right = values[mid - 1], values[mid]
    if (left + right) % 2 == 0:
        return str((left + right) // 2)
    return str((left + right) // 2) + ".5"


def median_by_type(records: list[dict[str, Any]]) -> dict[str, str]:
    """Same median as trade_median, once per Type. A type with no digit price is left out."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        kind = row.get("Type")
        if isinstance(kind, str):
            grouped.setdefault(kind, []).append(row)
    found: dict[str, str] = {}
    for kind, rows in grouped.items():
        median = trade_median(rows)
        if median is not None:
            found[kind] = median
    return found


def count_by_type(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in records:
        kind = row.get("Type")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def dumps(payload: dict[str, Any]) -> str:
    """The string Hermes shows the model. A dict is discarded at dispatch."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


ARG_GUIDE: dict[str, dict[str, str]] = {
    "not_object": {
        "field": "arguments",
        "problem": "not_object",
        "expected": "One JSON object with city, year, and quarter.",
    },
    "unknown_argument": {
        "field": "unknown",
        "problem": "unknown_field",
        "expected": (
            "Only city, year, quarter, price_classification, and language are accepted. "
            "Remove any other argument. Example: 13102, 2015, and 2."
        ),
    },
    "missing_argument": {
        "field": "city",
        "problem": "missing",
        "expected": "city, year, and quarter are required.",
    },
    "bad_city": {
        "field": "city",
        "problem": "shape",
        "expected": (
            "5 ASCII digits for a ward, city that is not designated, town, or village. "
            "Example: 13104 is Shinjuku-ku. A municipality name is not a code. "
            "A designated city needs a ward code, not the city code. "
            "This plugin does not look names up."
        ),
    },
    "bad_year": {
        "field": "year",
        "problem": "range",
        "expected": "An integer from 2005 through the current year. 2005 starts at quarter 3.",
    },
    "bad_quarter": {
        "field": "quarter",
        "problem": "range",
        "expected": "An integer 1, 2, 3, or 4. For 2005, only 3 and 4.",
    },
    "future_quarter": {
        "field": "quarter",
        "problem": "not_started",
        "expected": "A quarter whose first month is not after today's month.",
    },
    "bad_price_classification": {
        "field": "price_classification",
        "problem": "value",
        "expected": "The only accepted value is 01.",
    },
    "bad_language": {
        "field": "language",
        "problem": "value",
        "expected": "The only accepted value is ja.",
    },
}


def missing_field(raw: Any, detail: str | None) -> str | None:
    if detail != "missing_argument" or not isinstance(raw, dict):
        return None
    for row in ARGUMENTS:
        if row["required"] and row["name"] not in raw:
            return row["name"]
    return None


def fail(
    error_type: str,
    *,
    detail: str | None = None,
    status_code: int | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error_type": error_type,
        "status_code": status_code,
        "message": MISSING_KEY if error_type == "config" and detail == "missing_key" else ERRORS[error_type],
        "credit": CREDIT,
    }
    if detail is not None:
        payload["detail"] = detail
    if error_type == "bad_city":
        payload["field"] = "city"
        payload["problem"] = "designated_city"
        payload["expected"] = (
            "Use a ward code, not the city code. Example: 14101 is Tsurumi-ku, Yokohama. "
            "区のコードを使う（例: 14101 = 横浜市鶴見区）。"
        )
        payload["allowed_fields"] = [row["name"] for row in ARGUMENTS]
    elif error_type == "invalid_argument":
        guide = ARG_GUIDE.get(detail or "", {})
        payload["field"] = field or guide.get("field", "unknown")
        payload["problem"] = guide.get("problem", "rejected")
        payload["expected"] = guide.get("expected", "See the tool parameters.")
        payload["allowed_fields"] = [row["name"] for row in ARGUMENTS]
    return payload


def success(parsed: dict[str, Any], records: list[dict[str, Any]], *, fetched_at: str) -> dict[str, Any]:
    shown = records[:RECORD_CAP]
    return {
        "ok": True,
        "city": parsed["city"],
        "year": parsed["year"],
        "quarter": parsed["quarter"],
        "period": f"{parsed['year']}年第{parsed['quarter']}四半期",
        "price_classification": parsed["price_classification"],
        "fetched_at": fetched_at,
        "count": len(records),
        "truncated": len(records) > RECORD_CAP,
        "count_by_type": count_by_type(records),
        "median_trade_price": trade_median(records),
        "median_trade_price_by_type": median_by_type(records),
        "attribution": ATTRIBUTION,
        "attribution_url": TOP_URL,
        "processing": PROCESSING,
        "credit": CREDIT,
        "records": shown,
    }


ARG_TEXT = {
    "city": (
        "5-digit code for a ward, or for a city that is not a designated city. "
        "Example: 13104 is Shinjuku-ku. Not a municipality name. "
        "A designated city needs a ward code, not the city code. Example: 14101, not 14100."
    ),
    "year": "Calendar year as an integer. Transaction prices start in 2005, quarter 3.",
    "quarter": "Integer 1, 2, 3, or 4.",
    "price_classification": "Optional. Only 01 (transaction prices). Defaults to 01.",
    "language": "Optional. Only ja. Defaults to ja.",
}


def tool_parameters() -> dict[str, Any]:
    properties = {}
    required = []
    for row in ARGUMENTS:
        properties[row["name"]] = {
            "type": TYPES[row["type"]]["json"],
            "description": ARG_TEXT[row["name"]],
        }
        if row["required"]:
            required.append(row["name"])
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def classify_slash(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Three ASCII tokens. A name is bad_city. The submitted text is not returned."""
    parts = (raw or "").split()
    if len(parts) != 3:
        return None, "missing_argument"
    city, year_s, quarter_s = parts
    if is_designated_city(city):
        return None, "designated_city"
    if not check_city(city, EXAMPLE_CTX):
        return None, "bad_city"
    if not (_ascii_digits(year_s) and len(year_s) == 4 and year_s[0] != "0"):
        return None, "bad_year"
    if len(quarter_s) != 1 or quarter_s not in {"1", "2", "3", "4"}:
        return None, "bad_quarter"
    return {
        "city": city,
        "year": int(year_s),
        "quarter": int(quarter_s),
        "price_classification": "01",
        "language": "ja",
    }, None


def parse_slash(raw: str) -> dict[str, Any] | None:
    parsed, _detail = classify_slash(raw)
    return parsed


_U_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _u_once(text: str) -> str:
    return _U_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def _forms(text: str) -> tuple[str, ...]:
    percent = urllib.parse.unquote(text)
    escaped = _u_once(text)
    return (text, percent, escaped, _u_once(percent), urllib.parse.unquote(escaped))


def _strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_strings(key))
            found.extend(_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_strings(item))
    return found


def contains_secret(value: Any, key: str) -> bool:
    """True when key text, or any 12-character span of it, survives one decode."""
    if not key:
        return False
    blobs = _strings(value)
    blobs.append(json.dumps(value, ensure_ascii=False))
    blobs.append(json.dumps(value, ensure_ascii=True))
    spans = [key]
    if len(key) >= 12:
        spans.extend(key[index:index + 12] for index in range(len(key) - 11))
    for blob in blobs:
        for form in _forms(blob):
            if any(span in form for span in spans):
                return True
    return False


def seal(payload: dict[str, Any], key: str) -> dict[str, Any]:
    if contains_secret(payload, key):
        return fail("internal", detail="secret")
    return payload
