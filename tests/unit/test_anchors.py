from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from specmend.extract import FieldResult, RecordResult
from specmend.heal.anchors import (
    Anchor,
    AnchorPicker,
    AnchorStore,
    AnchorValue,
    encode,
    identity,
    key_of,
    split,
)
from specmend.pipeline import ValidatedRecord
from specmend.state import connect
from specmend.validate import Violation


def record(n: int, valid: bool = True, **values: Any) -> ValidatedRecord:
    fields = {"reference": FieldResult(f"TN-{n:04d}", f"TN-{n:04d}", 0)}
    for name, value in values.items():
        fields[name] = FieldResult(value, str(value), 1)
    violations = () if valid else (Violation("reference", "missing", "x"),)
    result = RecordResult(url=f"https://example.org/notices/{n}", fields=fields)
    return ValidatedRecord(result, violations, snapshot_id=f"src/run/{n:05d}")


def test_encode() -> None:
    assert encode(Decimal("566600.00")) == encode(Decimal("566600.0")) == "566600"
    assert encode(Decimal("12.50")) == "12.5"
    assert encode(Decimal("0.00")) == "0"
    assert encode(date(2026, 9, 27)) == "2026-09-27"
    assert (encode("x"), encode(7), encode(None)) == ("x", 7, None)


def test_anchor_from_record() -> None:
    anchor = Anchor.from_record(
        record(1, value=Decimal("5.00")), "https://example.org/notices/1", "run1"
    )
    assert anchor.values == {"reference": "TN-0001", "value": "5"}
    assert anchor.raw == {"reference": "TN-0001", "value": "5.00"}
    assert anchor.strategies == {"reference": 0, "value": 1}
    assert (anchor.snapshot_id, anchor.run_id) == ("src/run/00001", "run1")


def test_key_of() -> None:
    r = record(1)
    assert key_of(r, "url") == "https://example.org/notices/1"
    assert key_of(r, "reference") == "TN-0001"
    assert key_of(r, "missing") is None


def picked(n: int, records: list[ValidatedRecord]) -> list[str]:
    picker = AnchorPicker(n, "reference", "run")
    for r in records:
        picker.add(r)
    return [a.key for a in picker.anchors()]


def test_picks_the_same_records_whatever_their_order() -> None:
    records = [record(i) for i in range(200)]
    first = picked(10, records)
    assert len(first) == 10
    assert picked(10, list(reversed(records))) == first
    # a run that also saw other records still picks the ones it shares, if they rank low
    assert set(picked(10, records[:150])) & set(first) == {k for k in first if int(k[3:]) < 150}


def test_skips_invalid_duplicate_and_keyless_records() -> None:
    records = [record(1, valid=False), record(2), record(2)]
    assert picked(10, records) == ["TN-0002"]
    assert picked(0, records) == []


def test_keeps_the_values_of_every_valid_record() -> None:
    picker = AnchorPicker(1, "reference", "run")
    for r in [record(1, valid=False), record(2, value=Decimal("5.00")), record(2), record(3)]:
        picker.add(r)
    assert len(picker.anchors()) == 1
    assert picker.records() == {
        "TN-0002": {"reference": "TN-0002", "value": "5"},
        "TN-0003": {"reference": "TN-0003"},
    }


def test_store_replaces_the_records_with_the_anchors(tmp_path: Path) -> None:
    store = AnchorStore(connect(tmp_path / "state.db"))
    records: dict[str, dict[str, AnchorValue]] = {
        "k1": {"title": "A", "count": 3, "value": None},
        "k2": {"title": "B"},
    }
    store.replace("demo", [], records)
    assert store.records("demo") == records
    store.replace("demo", [])  # a good run that kept no records leaves none of the old ones
    assert store.records("demo") == {}
    assert store.records("other") == {}


def test_store_replaces_and_keeps_order(tmp_path: Path) -> None:
    store = AnchorStore(connect(tmp_path / "state.db"))
    anchors = [Anchor.from_record(record(i), f"k{i}", "run1") for i in range(3)]
    store.replace("demo", anchors)
    assert store.all("demo") == anchors
    store.replace("demo", anchors[:1])
    assert store.all("demo") == anchors[:1]
    assert store.all("other") == []


def test_split_shows_the_first_and_holds_out_the_rest() -> None:
    anchors = [Anchor.from_record(record(i), f"k{i}", "run1") for i in range(10)]
    shown, held_out = split(anchors, 3)
    assert (shown, held_out) == (anchors[:3], anchors[3:])


def url_record(origin: str, n: int) -> ValidatedRecord:
    fields = {"title": FieldResult(f"Notice {n}", f"Notice {n}", 0)}
    result = RecordResult(url=f"{origin}/notices/TN-{n:04d}", fields=fields)
    return ValidatedRecord(result, (), snapshot_id=None)


def test_anchors_do_not_depend_on_the_host_or_port() -> None:
    # The same site served from two places keeps the same anchors: they depend only on
    # the records' keys, with the scheme and host left out of a URL, and a fixed seed.
    picked = []
    for origin in ("http://127.0.0.1:8772", "https://tenders.example.org"):
        picker = AnchorPicker(10, "url", "run")
        for n in range(200):
            picker.add(url_record(origin, n))
        picked.append([urlsplit(a.key).path for a in picker.anchors()])
    assert picked[0] == picked[1]
    assert len(set(picked[0])) == 10


def test_identity_leaves_out_the_scheme_and_host() -> None:
    assert identity("http://127.0.0.1:8772/notices/TN-1?lang=en") == "/notices/TN-1?lang=en"
    assert identity("https://example.org/notices/TN-1") == "/notices/TN-1"
    assert identity("TN-2026-0001") == "TN-2026-0001"  # a key field's value stays as it is
