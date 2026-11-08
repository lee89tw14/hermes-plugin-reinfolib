"""Fifteen offline checks of the read-only quarter read."""
from __future__ import annotations

import builtins
import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import client
import messages
import schema
import service
from client import HEADER, Gap

TODAY = date(2026, 10, 12)
KEY = "unit-secret-key-xyz"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.t += delay


class Rec:
    def __init__(self, status: int = 200, body: bytes | None = None, headers: dict | None = None) -> None:
        self.calls: list[tuple[str, dict, float, int]] = []
        self.status = status
        self.body = body if body is not None else b""
        self.headers = headers or {}

    def __call__(self, url: str, headers: dict, timeout: float, cap: int):
        self.calls.append((url, dict(headers), timeout, cap))
        return self.status, self.headers, self.body


def _ok_body(city: str = "13102", year: int = 2015, quarter: int = 2) -> bytes:
    row = {column["name"]: column["sample"] for column in schema.COLUMNS}
    row["MunicipalityCode"] = city
    row["Period"] = f"{year}年第{quarter}四半期"
    return json.dumps({"status": "OK", "data": [row]}, ensure_ascii=False).encode()


def _read(args, transport, gap, key=KEY):
    return service.quarter(args, today=TODAY, transport=transport, gap=gap, key=key, base_url="https://www.reinfolib.mlit.go.jp")


def test_cities_do_not_share_a_failure():
    gap = Gap(Clock().now, Clock().sleep)
    missing = Rec(404, b'{"message":"no data here"}')
    first = _read({"city": "01999", "year": 2015, "quarter": 2}, missing, gap)
    found = Rec(200, _ok_body())
    second = _read({"city": "13102", "year": 2015, "quarter": 2}, found, gap)
    assert first["ok"] is False and first["error_type"] == "not_found" and first["status_code"] == 404
    assert second["ok"] is True and second["city"] == "13102" and second["count"] == 1
    assert "no data here" not in json.dumps(first)


def test_hermes_home_does_not_select_the_city(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "hermes-home-a")
    gap = Gap(Clock().now, Clock().sleep)
    one = _read({"city": "13102", "year": 2015, "quarter": 2}, Rec(200, _ok_body("13102")), gap)
    monkeypatch.setenv("HERMES_HOME", "hermes-home-b")
    two = _read({"city": "14101", "year": 2015, "quarter": 2}, Rec(200, _ok_body("14101")), gap)
    assert one["city"] == "13102" and two["city"] == "14101"


def test_blueprint_kwargs_have_no_toolset_or_mcp():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("jp_reinfolib_audit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    seen = {}

    class Ctx:
        def register_tool(self, **kwargs):
            return None

        def register_command(self, *args, **kwargs):
            return None

        def register_skill(self, *args, **kwargs):
            return None

        def register_automation_blueprint(self, key, **kwargs):
            seen[key] = kwargs
            return True

    module.register(Ctx())
    assert set(seen) == {"weekly-city-check", "weekly-history-check"}
    for kwargs in seen.values():
        blob = json.dumps(kwargs)
        assert "enabled_toolsets" not in kwargs
        assert "mcp" not in blob.lower()
        assert "reinfolib_watch" not in blob
    assert "reinfolib_quarter" in seen["weekly-city-check"]["prompt_template"]


def test_query_is_only_the_five_fields():
    gap = Gap(Clock().now, Clock().sleep)
    rec = Rec(200, _ok_body())
    refused = _read({"city": "13102", "year": 2015, "quarter": 2, "extra": "1"}, rec, gap)
    assert refused["detail"] == "unknown_argument" and rec.calls == []
    ok = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, gap)
    assert ok["ok"] is True
    query = parse_qs(urlsplit(rec.calls[0][0]).query)
    assert set(query) == {"year", "quarter", "city", "priceClassification", "language"}


def test_price_class_02_sends_nothing():
    rec = Rec(200, _ok_body())
    result = _read({"city": "13102", "year": 2015, "quarter": 2, "price_classification": "02"}, rec, Gap(Clock().now, Clock().sleep))
    assert result["error_type"] == "invalid_argument" and result["detail"] == "bad_price_classification"
    assert rec.calls == [] and result["status_code"] is None


def test_upstream_sentence_is_not_in_the_reply():
    body = b'{"message":"subscription key missing from the caller"}'
    rec = Rec(401, body)
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    dumped = json.dumps(result, ensure_ascii=False)
    assert result["message"] == messages.ERRORS["auth"]
    assert "subscription key" not in dumped
    assert len(rec.calls) == 1


def test_one_bad_row_fails_the_whole_body():
    good = json.loads(_ok_body())
    bad = json.loads(_ok_body())
    bad["data"][0]["Period"] = "2015年第3四半期"
    body = {"status": "OK", "data": good["data"] + bad["data"]}
    rec = Rec(200, json.dumps(body).encode())
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    assert result["ok"] is False and result["detail"] == "mismatched_record"
    assert "records" not in result


def test_read_does_not_open_for_write(monkeypatch):
    real = builtins.open

    def guard(file, mode="r", *args, **kwargs):
        if any(ch in str(mode) for ch in "wxa"):
            raise AssertionError(f"write mode {mode}")
        return real(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, Rec(200, _ok_body()), Gap(Clock().now, Clock().sleep))
    assert result["ok"] is True


def test_fresh_gap_does_not_inherit_another_gap(monkeypatch):
    clock = Clock()
    shared = Gap(clock.now, clock.sleep)
    monkeypatch.setattr(client, "DEFAULT_GAP", shared)
    rec = Rec(200, _ok_body())
    service.quarter(
        {"city": "13102", "year": 2015, "quarter": 2},
        today=TODAY, transport=rec, key=KEY, base_url="https://www.reinfolib.mlit.go.jp",
    )
    service.quarter(
        {"city": "13102", "year": 2015, "quarter": 2},
        today=TODAY, transport=rec, key=KEY, base_url="https://www.reinfolib.mlit.go.jp",
    )
    assert clock.slept == [1.0]
    fresh = Gap(clock.now, clock.sleep)
    other = Rec(200, _ok_body())
    service.quarter(
        {"city": "13102", "year": 2015, "quarter": 2},
        today=TODAY, transport=other, gap=fresh, key=KEY, base_url="https://www.reinfolib.mlit.go.jp",
    )
    assert clock.slept == [1.0]


def test_slash_rejects_fullwidth_and_extra_tokens_before_http():
    assert schema.parse_slash("１３１０２ 2015 2") is None
    assert schema.parse_slash("13102 2015 2 extra") is None
    assert schema.parse_slash("13102 2015 2")["city"] == "13102"


def test_tool_does_not_coerce_a_year_string():
    rec = Rec(200, _ok_body())
    result = _read({"city": "13102", "year": "2015", "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    assert result["detail"] == "bad_year" and rec.calls == []


def test_redirect_is_one_request():
    rec = Rec(302, b"", {"location": "http://127.0.0.1:9/elsewhere"})
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    assert result["error_type"] == "redirect" and result["status_code"] == 302
    assert result["message"] == messages.ERRORS["redirect"]
    assert len(rec.calls) == 1
    assert "elsewhere" not in json.dumps(result)


def test_429_is_one_request():
    rec = Rec(429, b'{"message":"slow down"}')
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    assert result["error_type"] == "rate_limited" and result["status_code"] == 429
    assert "slow down" not in json.dumps(result)
    assert len(rec.calls) == 1


def test_key_inside_the_url_is_not_sent():
    rec = Rec(200, _ok_body())
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep), key="13102")
    assert result["error_type"] == "config" and result["detail"] == "key_in_url"
    assert rec.calls == []
    assert HEADER not in json.dumps(result)


def test_other_municipality_code_is_marked_and_district_code_is_absent():
    body = json.loads(_ok_body())
    body["data"][0]["MunicipalityCode"] = "13103"
    body["data"][0]["DistrictCode"] = "131020170"
    rec = Rec(200, json.dumps(body).encode())
    result = _read({"city": "13102", "year": 2015, "quarter": 2}, rec, Gap(Clock().now, Clock().sleep))
    assert result["ok"] is True
    assert result["records"][0]["MunicipalityCode"] == {"kind": "other_municipality", "code": "13103"}
    assert "131020170" not in json.dumps(result)
    assert "DistrictCode" not in json.dumps(result)
