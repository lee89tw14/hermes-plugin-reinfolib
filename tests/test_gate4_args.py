"""Five argument shapes. No recorded session had an argument mistake."""
import json
from datetime import date

import schema
import service
from client import Gap

TODAY = date(2026, 10, 12)
KEY = "unit-secret-key-xyz"


class _Clock:
    def now(self) -> float:
        return 0.0

    def sleep(self, _delay: float) -> None:
        return None


class _Rec:
    def __init__(self, body: bytes) -> None:
        self.calls: list[str] = []
        self.body = body

    def __call__(self, url: str, headers: dict, timeout: float, cap: int):
        self.calls.append(url)
        return 200, {}, self.body


def _body(city: str = "13102") -> bytes:
    row = {column["name"]: column["sample"] for column in schema.COLUMNS}
    row["MunicipalityCode"] = city
    row["Period"] = "2015年第2四半期"
    return json.dumps({"status": "OK", "data": [row]}, ensure_ascii=False).encode()


def _read(args, transport):
    return service.quarter(
        args,
        today=TODAY,
        transport=transport,
        gap=Gap(_Clock().now, _Clock().sleep),
        key=KEY,
        base_url="https://www.reinfolib.mlit.go.jp",
    )


def test_integral_float_fullwidth_city_and_language_case():
    transport = _Rec(_body())
    result = _read({
        "city": "１３１０２",
        "year": 2015.0,
        "quarter": "２",
        "language": "JA",
    }, transport)
    assert result["ok"] is True
    assert result["city"] == "13102"
    assert result["year"] == 2015
    assert result["quarter"] == 2
    sent = transport.calls[0]
    assert "city=13102" in sent
    assert "year=2015" in sent
    assert "quarter=2" in sent
    assert "language=ja" in sent


def test_prefecture_name_is_not_a_city_code():
    transport = _Rec(_body())
    result = _read({"city": "東京都", "year": 2015, "quarter": 2}, transport)
    text = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["detail"] == "bad_city"
    assert "13104" in result["expected"]
    assert "東京都" not in text
    assert transport.calls == []


def test_extra_argument_hides_the_key():
    transport = _Rec(_body())
    result = _read({"city": "13102", "year": 2015, "quarter": 2, "tile": "01"}, transport)
    text = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["detail"] == "unknown_argument"
    assert "13102" in result["expected"]
    assert "tile" not in text
    assert transport.calls == []


def test_future_year_is_not_clamped():
    transport = _Rec(_body())
    result = _read({"city": "13102", "year": 2027, "quarter": 2}, transport)
    text = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["detail"] == "bad_year"
    assert "2005" in result["expected"]
    assert "2027" not in text
    assert transport.calls == []


def test_ascii_year_string_stays_refused():
    transport = _Rec(_body())
    result = _read({"city": "13102", "year": "2015", "quarter": 2}, transport)
    assert result["detail"] == "bad_year"
    assert transport.calls == []
