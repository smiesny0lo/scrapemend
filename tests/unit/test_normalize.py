from datetime import date
from decimal import Decimal
from typing import Any, get_args

import pytest

from specmend.extract.normalize import (
    NORMALIZERS,
    convert,
    parse_date,
    parse_decimal,
    parse_int,
    parse_url,
    strip_currency,
)
from specmend.spec import FieldSpec
from specmend.spec.models import Normalizer


def field(**kwargs: Any) -> FieldSpec:
    return FieldSpec.model_validate({"strategies": [{"css": "x"}], **kwargs})


def test_every_normalizer_name_is_implemented() -> None:
    assert set(NORMALIZERS) == set(get_args(Normalizer))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("€566,600.00", "566,600.00"),
        ("$ 1,234.50", "1,234.50"),
        ("£5", "5"),
        ("1 234,50 Kč", "1 234,50"),
        ("EUR 1.234,56", "1.234,56"),
        ("CHF 1'234.50", "1'234.50"),
        ("1 000,-", "1 000"),
    ],
)
def test_strip_currency(text: str, expected: str) -> None:
    assert strip_currency(text) == expected


# --- numbers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("566,600.00", "566600.00"),
        ("1.234,56", "1234.56"),
        ("1 234,50", "1234.50"),
        ("1\u00a0234,50", "1234.50"),  # no-break space
        ("1'234.50", "1234.50"),
        ("1,234", "1234"),  # 3 digits after a lone comma: thousands
        ("1,234,567", "1234567"),
        ("12,5", "12.5"),  # otherwise a decimal comma
        ("0,123", "0.123"),
        ("1.234", "1.234"),  # a lone dot is a decimal point
        ("1.234.567", "1234567"),
        ("-12.5", "-12.5"),
        ("\u221212", "-12"),  # unicode minus sign
        ("42", "42"),
    ],
)
def test_parse_decimal(text: str, expected: str) -> None:
    assert parse_decimal(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["Not disclosed", "", "12a", "1.2.3,4.5", "-"])
def test_parse_decimal_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="not a number"):
        parse_decimal(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1234", 1234), ("1,234", 1234), ("1.234.567", 1234567), ("1 234", 1234), ("-5", -5)],
)
def test_parse_int(text: str, expected: int) -> None:
    assert parse_int(text) == expected


@pytest.mark.parametrize("text", ["12.5", "1,23", "abc", "1,2345"])
def test_parse_int_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="not a whole number"):
        parse_int(text)


# --- dates ---------------------------------------------------------------------------------


def test_parse_date_with_format() -> None:
    assert parse_date("27 Sep 2026", "%d %b %Y") == date(2026, 9, 27)
    with pytest.raises(ValueError, match="does not match date_format"):
        parse_date("2026-09-27", "%d %b %Y")


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-03",
        "2026-11-03T10:30:00",
        "3 Nov 2026",
        "3 November 2026",
        "Nov 3, 2026",
        "03.11.2026",
        "3. 11. 2026",
    ],
)
def test_parse_date_defaults(text: str) -> None:
    assert parse_date(text) == date(2026, 11, 3)


def test_parse_date_refuses_to_guess_day_and_month() -> None:
    with pytest.raises(ValueError, match="set date_format"):
        parse_date("03/11/2026")


# --- urls -------------------------------------------------------------------------------------


def test_parse_url() -> None:
    base = "http://localhost:8000/notices?page=2"
    assert parse_url("/notices/TN-1", base) == "http://localhost:8000/notices/TN-1"
    assert parse_url("?page=3", base) == "http://localhost:8000/notices?page=3"
    assert parse_url("https://example.org/x", base) == "https://example.org/x"


@pytest.mark.parametrize(
    ("text", "base"),
    [
        ("mailto:a@example.org", None),
        ("javascript:void(0)", "http://example.org"),
        ("/relative", None),
    ],
)
def test_parse_url_rejects(text: str, base: str | None) -> None:
    with pytest.raises(ValueError, match="not an http"):
        parse_url(text, base)


# --- convert -------------------------------------------------------------------------------------


def test_convert_applies_normalizers_then_type() -> None:
    price = field(type="decimal", normalize=["strip_currency"])
    assert convert("€566,600.00", price) == Decimal("566600.00")
    assert convert("  Hello  World ", field(normalize=["collapse_whitespace", "lower"])) == (
        "hello world"
    )


def test_convert_null_values() -> None:
    price = field(type="decimal", null_values=["Not disclosed", "N/A"])
    assert convert(" not   DISCLOSED ", price) is None
    assert convert("n/a", price) is None
    with pytest.raises(ValueError):
        convert("unknown", price)


def test_convert_empty_after_normalizing() -> None:
    with pytest.raises(ValueError, match="nothing left"):
        convert("€", field(type="decimal", normalize=["strip_currency"]))


def test_convert_url_uses_base() -> None:
    link = field(type="url")
    assert convert("/a", link, base_url="http://example.org/b") == "http://example.org/a"
