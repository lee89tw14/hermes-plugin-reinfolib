"""Offline behavior. Transport is injected. No sockets."""
from __future__ import annotations

import gzip
import importlib.util
import inspect
import json
import sys
import threading
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import client
import messages
import schema
import service
from client import BODY_CAP, HEADER, Gap

ROOT = Path(__file__).resolve().parents[1]
KEY = "unit-secret-key-xyz"
TODAY = date(2026, 10, 12)
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "xit001_ok.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.t += delay


class Transport:
    def __init__(self, status: int = 200, body: bytes | None = None, headers: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else json.dumps(FIXTURE).encode()
        self.headers = headers or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, timeout, cap):
        self.calls.append((url, dict(headers)))
        return self.status, dict(self.headers), self.body


def _gap() -> tuple[Clock, Gap]:
    clock = Clock()
    return clock, Gap(now=clock.now, sleep=clock.sleep)


def _read(transport: Transport, args: dict | None = None, **kwargs):
    clock, gap = _gap()
    payload = service.quarter(
        args or {"city": "13102", "year": 2015, "quarter": 2},
        today=kwargs.pop("today", TODAY),
        transport=transport,
        gap=gap,
        key=kwargs.pop("key", KEY),
        base_url=kwargs.pop("base_url", "https://www.reinfolib.mlit.go.jp"),
        fetched_at=kwargs.pop("fetched_at", "2026-10-14T02:00:00Z"),
    )
    return payload, transport, clock


def test_fixture_from_live_shape_redacts_place_and_price():
    row = FIXTURE["data"][0]
    assert FIXTURE["status"] == "OK"
    assert row["DistrictName"] == "" and row["TradePrice"] == "0" and row["DistrictCode"] == ""
    assert row["Period"] == "2015年第2四半期"
    assert row["MunicipalityCode"] == "13102"
    payload, transport, _clock = _read(Transport())
    assert payload["ok"] is True
    assert payload["count"] == 1 and payload["truncated"] is False
    assert payload["records"][0]["DistrictName"] == ""
    assert payload["attribution"] == messages.ATTRIBUTION
    assert payload["processing"] == messages.PROCESSING
    assert messages.CREDIT in payload["credit"]
    url = transport.calls[0][0]
    query = parse_qs(urlsplit(url).query)
    assert query["priceClassification"] == ["01"]
    assert query["language"] == ["ja"]
    assert query["city"] == ["13102"]
    assert KEY not in url
    assert transport.calls[0][1][HEADER] == KEY


def test_refuses_before_http():
    cases = [
        ({"city": "13102", "year": 2015, "quarter": 2, "area": "1"}, "unknown_argument"),
        ({"city": "13102", "year": True, "quarter": 2}, "bad_year"),
        ({"city": "13102", "year": 2005, "quarter": 1}, "bad_quarter"),
        ({"city": "13102", "year": 2026, "quarter": 2}, "future_quarter"),
        ({"city": "13102", "year": 2015, "quarter": 2, "price_classification": "02"}, "bad_price_classification"),
        ({"year": 2015, "quarter": 2}, "missing_argument"),
    ]
    for args, detail in cases:
        today = date(2026, 2, 15) if detail == "future_quarter" else TODAY
        payload, transport, _clock = _read(Transport(), args, today=today)
        assert payload["ok"] is False and payload["detail"] == detail, args
        assert payload["message"] == messages.ERRORS["invalid_argument"]
        assert transport.calls == []


def test_empty_key_and_bad_origin_send_nothing():
    payload, transport, _clock = _read(Transport(), key="")
    assert payload["detail"] == "missing_key" and transport.calls == []
    payload, transport, _clock = _read(Transport(), base_url="http://evil.example")
    assert payload["detail"] == "bad_origin" and transport.calls == []
    payload, transport, _clock = _read(Transport(), base_url="https://www.reinfolib.mlit.go.jp/ex-api")
    assert payload["detail"] == "bad_origin" and transport.calls == []
    payload, transport, _clock = _read(Transport(), base_url="http://127.0.0.1:9")
    assert payload["ok"] is True
    assert transport.calls[0][0].startswith("http://127.0.0.1:9/ex-api/external/XIT001?")


def test_http_errors_use_fixed_text_and_do_not_retry():
    secret_body = json.dumps({"statusCode": 401, "message": KEY}).encode()
    payload, transport, _clock = _read(Transport(401, secret_body))
    assert payload["error_type"] == "auth" and payload["status_code"] == 401
    assert KEY not in json.dumps(payload)
    assert len(transport.calls) == 1
    payload, _transport, _clock = _read(Transport(404, b'{"message":"not the live text"}'))
    assert payload["error_type"] == "not_found" and payload["status_code"] == 404
    assert payload["message"] == messages.ERRORS["not_found"]
    assert "not the live text" not in json.dumps(payload)
    assert "unchanged" in payload["message"]
    payload, transport, _clock = _read(Transport(429, b'{"message":"slow down ' + KEY.encode() + b'"}'))
    assert payload["error_type"] == "rate_limited" and payload["status_code"] == 429
    assert KEY not in json.dumps(payload) and len(transport.calls) == 1
    payload, transport, _clock = _read(Transport(502, b"upstream blew up"))
    assert payload["error_type"] == "upstream" and payload["status_code"] == 502
    assert "blew up" not in json.dumps(payload) and len(transport.calls) == 1
    payload, _transport, _clock = _read(Transport(302, b'{"status":"OK","data":[]}'))
    assert payload["error_type"] == "redirect" and payload["status_code"] == 302


def test_bad_bodies_are_not_success():
    empty = json.dumps({"status": "OK", "data": []}).encode()
    payload, _transport, _clock = _read(Transport(200, empty))
    assert payload["ok"] is False and payload["detail"] == "empty_data"
    wrapped = dict(FIXTURE)
    wrapped["statusCode"] = 200
    payload, _transport, _clock = _read(Transport(200, json.dumps(wrapped).encode()))
    assert payload["detail"] == "not_ok"
    row = dict(FIXTURE["data"][0])
    row["Period"] = "2015年第3四半期"
    payload, _transport, _clock = _read(Transport(200, json.dumps({"status": "OK", "data": [row]}).encode()))
    assert payload["ok"] is False and payload["detail"] == "mismatched_record"
    assert "records" not in payload
    rows = []
    for _ in range(21):
        rows.append(dict(FIXTURE["data"][0]))
    payload, _transport, _clock = _read(Transport(200, json.dumps({"status": "OK", "data": rows}).encode()))
    assert payload["count"] == 21 and payload["truncated"] is True and len(payload["records"]) == 20


def test_gzip_short_and_huge():
    raw = gzip.compress(json.dumps(FIXTURE).encode())
    payload, _transport, _clock = _read(Transport(200, raw, {"content-encoding": "gzip"}))
    assert payload["ok"] is True and payload["count"] == 1
    payload, _transport, _clock = _read(Transport(200, b"{", {"content-length": "50"}))
    assert payload["detail"] == "short_body"
    payload, _transport, _clock = _read(Transport(200, b"x" * (BODY_CAP + 1)))
    assert payload["detail"] == "too_big"
    payload, _transport, _clock = _read(Transport(200, b"abc", {"content-encoding": "br"}))
    assert payload["detail"] == "bad_encoding"


def test_secret_gate_finite_forms():
    def row_with(name: str) -> dict:
        row = dict(FIXTURE["data"][0])
        row["DistrictName"] = name
        body = json.dumps({"status": "OK", "data": [row]}).encode()
        payload, _transport, _clock = _read(Transport(200, body))
        return payload

    assert row_with(KEY)["error_type"] == "internal"
    assert KEY not in json.dumps(row_with(KEY))
    assert row_with(urllib_quote(KEY))["error_type"] == "internal"
    escaped = "".join("\\u%04x" % ord(ch) for ch in KEY)
    assert row_with(escaped)["error_type"] == "internal"
    assert row_with(KEY[3:15])["error_type"] == "internal"
    # One decode does not recover a twice-encoded key. That limit is documented, not promised.
    assert "twice-encoded" in (ROOT / "README.md").read_text(encoding="utf-8").lower() or "twice-encoded" in (ROOT / "README.md").read_text(encoding="utf-8")


def urllib_quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")


def test_gap_is_one_second_after_the_call_returns():
    clock, gap = _gap()
    transport = Transport()
    service.quarter(
        {"city": "13102", "year": 2015, "quarter": 2},
        today=TODAY, transport=transport, gap=gap, key=KEY,
        base_url="https://www.reinfolib.mlit.go.jp",
    )
    service.quarter(
        {"city": "13102", "year": 2015, "quarter": 2},
        today=TODAY, transport=transport, gap=gap, key=KEY,
        base_url="https://www.reinfolib.mlit.go.jp",
    )
    assert clock.slept == [schema.MIN_GAP_SECONDS]
    assert schema.MIN_GAP_SECONDS == 1


def test_timeout_and_network_are_fixed():
    def boom(url, headers, timeout, cap):
        raise TimeoutError("slow")
    payload, _transport, _clock = _read(boom)
    assert payload["error_type"] == "timeout"
    assert "slow" not in json.dumps(payload)

    def down(url, headers, timeout, cap):
        raise OSError("no route " + KEY)
    payload, _transport, _clock = _read(down)
    assert payload["error_type"] == "upstream" and payload["detail"] == "network"
    assert KEY not in json.dumps(payload)


def test_eight_callers_do_not_overlap_inside_the_gap():
    gap = Gap(now=lambda: 0.0, sleep=lambda _delay: None)
    state = {"current": 0, "peak": 0}
    guard = threading.Lock()

    def call():
        with guard:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        with guard:
            state["current"] -= 1
        return None

    threads = [threading.Thread(target=lambda: gap.run(call)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["peak"] == 1 and state["current"] == 0


def test_plugin_registers_only_the_read_tool_and_no_cron():
    path = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("jp_reinfolib_plugin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = []
            self.commands = []
            self.skills = []
            self.blueprints = []

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_command(self, name, handler, description=""):
            self.commands.append((name, handler))

        def register_skill(self, name, path, description=""):
            assert Path(path).is_file()
            self.skills.append(name)

        def register_automation_blueprint(self, key, **kwargs):
            self.blueprints.append((key, kwargs))
            return object()

    ctx = Ctx()
    module.register(ctx)
    assert [tool["name"] for tool in ctx.tools] == [
        "reinfolib_transactions", "reinfolib_records", "reinfolib_export", "reinfolib_catalog",
        "reinfolib_municipalities", "reinfolib_stations", "reinfolib_snapshots", "reinfolib_history", "reinfolib_diff", "reinfolib_layer", "reinfolib_land_values", "reinfolib_geocode", "reinfolib_point_layers", "reinfolib_quarter",
    ]
    assert ctx.skills == ["property-transactions", "weekly-city-check"]
    assert ctx.blueprints[0][0] == "weekly-city-check"
    prompt = ctx.blueprints[0][1]["prompt_template"]
    assert "reinfolib_quarter" in prompt and "reinfolib_watch" not in prompt
    assert "[SILENT]" in prompt
    assert "Never answer [SILENT]" not in prompt
    assert "not_found" in prompt and "ok true" in prompt
    handler = ctx.commands[0][1]
    assert inspect.iscoroutinefunction(handler)
    older = Ctx()
    older.register_automation_blueprint = None
    module.register(older)
    assert older.tools and older.blueprints == []


def test_source_has_no_watcher():
    banned = ["create_job(", "plugin_data", "reinfolib_watch", "seen-", "watch_failure", "get_write_denied_error"]
    for path in ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, (path.name, word)
    assert not (ROOT / "store.py").exists()
    assert not (ROOT / "gates.py").exists()


def test_bad_city_does_not_echo_the_name_and_names_the_shape():
    payload = service.quarter(
        {"city": "新宿区", "year": 2025, "quarter": 2},
        today=TODAY, key=KEY, base_url="https://www.reinfolib.mlit.go.jp",
    )
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "新宿区" not in dumped
    assert payload["detail"] == "bad_city"
    assert payload["field"] == "city" and payload["problem"] == "shape"
    assert "13104" in payload["expected"]
    assert payload["allowed_fields"] == ["city", "year", "quarter", "price_classification", "language"]
    params = schema.tool_parameters()["properties"]["city"]
    assert "13104" in params["description"] and "name" in params["description"]


def test_designated_city_is_refused_before_a_request():
    transport = Transport()
    payload, seen, _clock = _read(transport, {"city": "14100", "year": 2015, "quarter": 2})
    dumped = json.dumps(payload, ensure_ascii=False)
    assert seen.calls == []
    assert "14100" not in dumped
    assert payload["error_type"] == "bad_city"
    assert payload["field"] == "city" and payload["problem"] == "designated_city"
    assert "14101" in payload["expected"]
    assert "区のコードを使う" in payload["expected"]
    parsed, detail = schema.validate_arguments({"city": "14101", "year": 2015, "quarter": 2}, TODAY)
    assert detail is None and parsed["city"] == "14101"


def test_median_by_type_uses_each_type():
    condo = dict(FIXTURE["data"][0])
    condo["Type"] = "中古マンション等"
    condo["TradePrice"] = "10"
    land = dict(FIXTURE["data"][0])
    land["Type"] = "宅地(土地)"
    land["TradePrice"] = "30"
    body = json.dumps({"status": "OK", "data": [condo, land]}, ensure_ascii=False).encode()
    payload, _transport, _clock = _read(Transport(200, body))
    assert payload["median_trade_price"] == "20"
    assert payload["median_trade_price_by_type"] == {"中古マンション等": "10", "宅地(土地)": "30"}


def test_one_odd_field_does_not_drop_the_quarter():
    row = dict(FIXTURE["data"][0])
    row["Area"] = "2000㎡以上"
    row["MunicipalityCode"] = "14101"
    row["TradePrice"] = "10"
    body = json.dumps({"status": "OK", "data": [row]}, ensure_ascii=False).encode()
    payload, _transport, _clock = _read(Transport(200, body), {"city": "13102", "year": 2015, "quarter": 2})
    assert payload["ok"] is True and payload["count"] == 1
    assert payload["records"][0]["Area"] == {"kind": "not_digits"}
    assert "2000" not in json.dumps(payload, ensure_ascii=False)
    assert payload["records"][0]["MunicipalityCode"] == {"kind": "other_municipality", "code": "14101"}
    assert payload["median_trade_price"] == "10"
    assert payload["fetched_at"] == "2026-10-14T02:00:00Z"
    assert payload["count_by_type"]["中古マンション等"] == 1


def test_median_uses_digit_prices_only():
    rows = []
    for price in ("10", "30", "20", "nope"):
        row = dict(FIXTURE["data"][0])
        row["TradePrice"] = price
        rows.append(row)
    body = json.dumps({"status": "OK", "data": rows}, ensure_ascii=False).encode()
    payload, _transport, _clock = _read(Transport(200, body))
    assert payload["ok"] is True and payload["count"] == 4
    assert payload["median_trade_price"] == "20"
    assert schema.trade_median([{"TradePrice": "10"}, {"TradePrice": "11"}]) == "10.5"
    assert schema.trade_median([{"TradePrice": "10"}, {"TradePrice": "20"}]) == "15"
    assert payload["records"][3]["TradePrice"] == {"kind": "not_digits"}


def test_slash_parse_and_async_does_not_block_on_bad_input():
    assert schema.parse_slash("13102 2015 2")["year"] == 2015
    assert schema.parse_slash("13102 02015 2") is None
    assert schema.parse_slash("13102 2015 02") is None
    assert schema.parse_slash("schedule") is None
    parsed, detail = schema.classify_slash("14100 2015 2")
    assert parsed is None and detail == "designated_city"


def test_public_numbers_match_the_table():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8") + readme
    assert "reinfolib_quarter" in text and "reinfolib_watch" not in text
    assert "1 second" in readme
    assert "30 seconds" in readme
    assert "8000000" in readme
    assert "20" in readme
    assert "2005" in readme
    assert "Monday" in readme and "09:00" in readme
    assert "hermes tools" in text
    assert "hermes cron remove" in readme
    assert "does not create a cron job" in readme
    assert messages.CREDIT in text
    assert "\u3000" in readme
    assert "Nothing new" not in text
    assert "unschedule" not in readme


@pytest.fixture(autouse=True)
def _no_live_env(monkeypatch):
    monkeypatch.delenv("REINFOLIB_API_KEY", raising=False)
    monkeypatch.delenv("REINFOLIB_BASE_URL", raising=False)
    monkeypatch.delenv("REINFOLIB_CITY", raising=False)
