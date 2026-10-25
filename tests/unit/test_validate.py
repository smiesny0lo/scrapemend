from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from scrapemend.extract import FieldResult, RecordResult
from scrapemend.pipeline import FieldStats
from scrapemend.spec import FieldSpec
from scrapemend.spec.models import ExpectSpec
from scrapemend.validate import validate_batch, validate_record


def field(**kwargs: Any) -> FieldSpec:
    return FieldSpec.model_validate({"strategies": [{"css": "x"}], **kwargs})


FIELDS = {
    "reference": field(required=True, pattern=r"TN-\d{4}-\d{4}"),
    "title": field(required=True),
    "value": field(type="decimal"),
}


def record(**values: Any) -> RecordResult:
    fields = {}
    for name in FIELDS:
        value = values.get(name)
        if isinstance(value, FieldResult):
            fields[name] = value
        else:
            fields[name] = FieldResult(value, None if value is None else str(value), 0)
    return RecordResult(url="http://example.org/1", fields=fields)


def kinds(result: RecordResult) -> list[tuple[str | None, str]]:
    return [(v.field, v.kind) for v in validate_record(result, FIELDS)]


def test_valid_record() -> None:
    assert kinds(record(reference="TN-2026-0001", title="T", value=Decimal(5))) == []


def test_missing_required_field() -> None:
    assert kinds(record(reference="TN-2026-0001")) == [("title", "missing")]


def test_optional_field_may_be_empty() -> None:
    assert kinds(record(reference="TN-2026-0001", title="T")) == []


def test_unusable_text_is_reported_even_on_optional_fields() -> None:
    unusable = FieldResult(None, "call us", None, "strategy 0: 'call us' is not a number")
    assert kinds(record(reference="TN-2026-0001", title="T", value=unusable)) == [
        ("value", "unusable")
    ]


def test_pattern_must_match_the_whole_value() -> None:
    assert kinds(record(reference="REF TN-2026-0001", title="T")) == [("reference", "format")]


def test_pattern_needs_a_text_type() -> None:
    with pytest.raises(ValidationError, match="pattern only applies"):
        field(type="decimal", pattern=r"\d+")
    with pytest.raises(ValidationError, match="invalid pattern"):
        field(pattern="(unclosed")


# --- batch ---------------------------------------------------------------------------------


def stats(empty: int) -> FieldStats:
    return FieldStats(empty=empty)


EXPECT = ExpectSpec(min_items_per_run=100, max_null_rate={"value": 0.1})


def test_min_items_only_for_complete_runs() -> None:
    assert [v.kind for v in validate_batch(50, {"value": stats(0)}, EXPECT, complete=True)] == [
        "min_items"
    ]
    assert validate_batch(50, {"value": stats(0)}, EXPECT, complete=False) == []


def test_null_rate() -> None:
    [violation] = validate_batch(100, {"value": stats(20)}, EXPECT, complete=True)
    assert (violation.field, violation.kind) == ("value", "null_rate")
    assert violation.message == "value empty in 20% of records (allowed 10%)"
    assert validate_batch(100, {"value": stats(10)}, EXPECT, complete=True) == []


def test_null_rate_needs_enough_records_in_a_sample() -> None:
    # 1 of 5 sampled records empty is 20%, but says little: don't judge samples that small.
    assert (
        validate_batch(
            5, {"value": stats(1)}, ExpectSpec(max_null_rate={"value": 0.1}), complete=False
        )
        == []
    )
    assert validate_batch(
        20, {"value": stats(4)}, ExpectSpec(max_null_rate={"value": 0.1}), complete=False
    )
