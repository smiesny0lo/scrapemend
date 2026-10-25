"""Normalizers and type conversion: turn the text a strategy found into a typed value.

Conversion raises ValueError with a readable message when the text isn't a valid
value of the field's type; the engine then tries the field's next strategy.
"""

import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

from scrapemend.extract.document import squash
from scrapemend.spec.models import FieldSpec, Normalizer

Value = str | int | Decimal | date | None
Encoded = str | int | None  # a value as it is stored and compared


def encode(value: Value) -> Encoded:
    """A value in the form it is stored and compared in: decimals normalized, so
    566600.00 and 566600.0 agree, and dates in ISO format."""
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if isinstance(value, date):
        return value.isoformat()
    return value


# --- normalizers --------------------------------------------------------------------

# Currency written as letters rather than a symbol, at the start or end of the text.
_CURRENCY_WORD = r"(?:[A-Z]{3}|Kč|Kc|zł|kr|Ft)\.?"
_CURRENCY_WORDS = re.compile(rf"^\s*{_CURRENCY_WORD}\s*|\s*{_CURRENCY_WORD}\s*$")


def strip_currency(text: str) -> str:
    """Remove currency symbols (€ $ £ …), codes (EUR, CZK, …) and a trailing ",-"."""
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Sc")
    text = _CURRENCY_WORDS.sub("", text).strip()
    return re.sub(r"[,.]-$", "", text).strip()


NORMALIZERS: dict[Normalizer, Callable[[str], str]] = {
    "strip": str.strip,
    "collapse_whitespace": squash,
    "lower": str.lower,
    "strip_currency": strip_currency,
}


# --- numbers --------------------------------------------------------------------------

_DIGIT_GROUPING = re.compile(r"[\s\u00a0\u2009\u202f']")  # 1 234, 1'234 (incl. nbsp, thin space)
_NUMBER = re.compile(r"[+-]?[\d.,]*\d")


def parse_decimal(text: str) -> Decimal:
    """Parse a number written with either decimal mark.

    - Both "," and "." present: the rightmost is the decimal mark (1,234.56 / 1.234,56).
    - Only ",": thousands if repeated or followed by exactly 3 digits (1,234 / 1,234,567),
      otherwise the decimal mark (12,5 / 0,123).
    - Only ".": thousands if repeated (1.234.567), otherwise the decimal mark (1.234).
    """
    s = _DIGIT_GROUPING.sub("", text).replace("\u2212", "-")  # unicode minus sign
    if not _NUMBER.fullmatch(s):
        raise ValueError(f"{text!r} is not a number")
    if "," in s and "." in s:
        mark = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if mark == "," else ",", "").replace(mark, ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        is_thousands = s.count(",") > 1 or (len(tail) == 3 and head.lstrip("+-") not in ("", "0"))
        s = s.replace(",", "") if is_thousands else f"{head}.{tail}"
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{text!r} is not a number") from None


def parse_int(text: str) -> int:
    """Parse a whole number; groups of three may be split by spaces, "," or "." (1.234.567)."""
    s = _DIGIT_GROUPING.sub("", text).replace("\u2212", "-")
    if re.fullmatch(r"[+-]?\d{1,3}(?:([.,])\d{3}(?:\1\d{3})*)?|[+-]?\d+", s):
        return int(s.replace(",", "").replace(".", ""))
    raise ValueError(f"{text!r} is not a whole number")


# --- dates ----------------------------------------------------------------------------

# Tried when a field has no date_format. Formats where day and month could be swapped
# (03/11/2026) are left out on purpose: set date_format for those.
DEFAULT_DATE_FORMATS = (
    "%d %b %Y",  # 3 Nov 2026
    "%d %B %Y",  # 3 November 2026
    "%b %d, %Y",  # Nov 3, 2026
    "%B %d, %Y",  # November 3, 2026
    "%d.%m.%Y",  # 03.11.2026
    "%d. %m. %Y",  # 3. 11. 2026
)


def parse_date(text: str, date_format: str | None = None) -> date:
    if date_format:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            raise ValueError(f"{text!r} does not match date_format {date_format!r}") from None
    for parse in (date.fromisoformat, lambda t: datetime.fromisoformat(t).date()):
        try:
            return parse(text)
        except ValueError:
            pass
    for fmt in DEFAULT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{text!r} is not a recognised date; set date_format")


# --- urls -------------------------------------------------------------------------------


def parse_url(text: str, base_url: str | None = None) -> str:
    """Resolve a link against the page it was found on; only http(s) URLs are valid."""
    url = urljoin(base_url, text) if base_url else text
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{text!r} is not an http(s) URL")
    return url


# --- conversion -------------------------------------------------------------------------


def convert(text: str, field: FieldSpec, base_url: str | None = None) -> Value:
    """Normalize text and convert it to the field's type."""
    if squash(text).casefold() in {v.casefold() for v in field.null_values}:
        return None
    for name in field.normalize:
        text = NORMALIZERS[name](text)
    if not text:
        raise ValueError("nothing left after normalizing")
    match field.type:
        case "str":
            return text
        case "int":
            return parse_int(text)
        case "decimal":
            return parse_decimal(text)
        case "date":
            return parse_date(text, field.date_format)
        case "url":
            return parse_url(text, base_url)
