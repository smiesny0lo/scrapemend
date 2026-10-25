from decimal import Decimal
from typing import Any

import pytest

from scrapemend.detect.classify import Diagnosis, Evidence, Failure, classify, diagnose, worst
from scrapemend.extract import FieldResult
from scrapemend.fetch import BlockedError, RobotsDisallowed, TransientError
from scrapemend.pipeline import FieldStats, ItemFailure, RunAborted, RunSummary
from scrapemend.spec import SourceSpec
from scrapemend.validate import Violation

SPEC = SourceSpec.model_validate(
    {
        "id": "demo",
        "fetch": {"start": "https://example.org/list", "paginate": {"next": {"css": "a.next"}}},
        "item": {"container": {"css": "li"}, "follow": {"css": "a"}},
        "fields": {
            "title": {"required": True, "strategies": [{"css": "h1"}, {"label": "Title"}]},
            "category": {"strategies": [{"css": ".cat"}]},
            "value": {"type": "decimal", "volatile": True, "strategies": [{"css": ".v"}]},
        },
        "expect": {"min_items_per_run": 100, "max_null_rate": {"value": 0.2}},
    }
)


def summary(
    records: int = 200, items: int | None = None, pages: int = 10, **fields: FieldStats
) -> RunSummary:
    stats = {name: FieldStats(filled=records, winners={0: records}) for name in SPEC.fields}
    stats.update(fields)
    return RunSummary(
        source_id="demo",
        spec_version=1,
        run_id="r1",
        listing_pages=pages,
        items=records if items is None else items,
        records=records,
        fields=stats,
    )


def evidence(**kwargs: Any) -> Evidence:
    options: dict[str, Any] = {"spec": SPEC, "summary": summary(), "complete": True}
    options.update(kwargs)
    return Evidence(**options)


def categories(e: Evidence) -> list[str]:
    return [f.category for f in classify(e)[0]]


def test_healthy_run() -> None:
    assert classify(evidence(structure={"listing": 0.95, "detail": 1.0}, baseline=summary())) == (
        [],
        [],
    )


# --- the site refused or failed us ----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        BlockedError("https://example.org", "HTTP 403"),
        RobotsDisallowed("https://example.org", "disallowed by robots.txt"),
    ],
)
def test_blocked(error: Exception) -> None:
    assert categories(evidence(error=error)) == ["blocked"]


def test_bot_wall_page_is_blocked_even_with_status_200() -> None:
    e = evidence(summary=summary(records=0, pages=1), listing_text="Please verify you are human")
    assert categories(e) == ["blocked"]


def test_transient() -> None:
    assert categories(
        evidence(error=TransientError("https://example.org", "HTTP 503 after 4 attempts"))
    ) == ["transient"]


def test_failure_streak_of_timeouts_is_transient() -> None:
    s = summary(records=3)
    s.failures = [
        ItemFailure(url=f"https://example.org/{i}", reason="ReadTimeout", transient=True)
        for i in range(5)
    ]
    assert categories(evidence(summary=s, error=RunAborted("5 items in a row failed"))) == [
        "transient"
    ]


def test_failure_streak_of_404s_means_detail_pages_moved() -> None:
    s = summary(records=3)
    s.failures = [
        ItemFailure(url=f"https://example.org/{i}", reason="HTTP 404", status=404) for i in range(5)
    ]
    [failure] = classify(evidence(summary=s, error=RunAborted("5 items in a row failed")))[0]
    assert (failure.category, failure.fields) == ("layout_changed", ["item.follow"])


def test_items_without_links() -> None:
    s = summary(records=0)
    s.failures = [ItemFailure(url=None, reason="item has no detail link") for _ in range(5)]
    [failure] = classify(evidence(summary=s, error=RunAborted("5 items in a row failed")))[0]
    assert (failure.category, failure.fields) == ("layout_changed", ["item.follow"])


def test_listing_page_gone() -> None:
    [failure] = classify(
        evidence(error=RunAborted("listing page returned HTTP 404", listing_status=404))
    )[0]
    assert (failure.category, failure.fields) == ("layout_changed", ["fetch.start"])


# --- nothing found --------------------------------------------------------------------


def test_container_matches_nothing_after_a_redesign() -> None:
    diagnosis = Diagnosis(
        pages=["https://example.org/1"], broken=["title"], fallback=["value"], intact=[]
    )
    e = evidence(
        summary=summary(records=0, pages=1),
        structure={"listing": 0.14},
        diagnosis=diagnosis,
        next_link_missing=True,
    )
    [failure] = classify(e)[0]
    assert failure.category == "layout_changed"
    assert failure.fields == ["item.container", "fetch.paginate.next", "title"]
    assert "listing pages 14% similar" in failure.message
    assert "broken: title" in failure.message and "only via fallbacks: value" in failure.message


def test_container_matches_nothing_on_an_unchanged_page() -> None:
    e = evidence(summary=summary(records=0, pages=1), structure={"listing": 0.97})
    assert categories(e) == ["empty"]


def test_no_items_and_no_baseline_is_empty() -> None:
    assert categories(evidence(summary=summary(records=0, pages=1))) == ["empty"]


# --- fields ---------------------------------------------------------------------------


def test_failing_required_field_after_a_redesign() -> None:
    s = summary(title=FieldStats(filled=150, empty=50, invalid=50, winners={0: 150}))
    [failure] = classify(evidence(summary=s, structure={"detail": 0.2}))[0]
    assert (failure.category, failure.fields) == ("layout_changed", ["title"])
    assert "required but empty in 50/200 records" in failure.message


def test_failing_field_without_structural_change_is_partial() -> None:
    s = summary(category=FieldStats(empty=200))
    [failure] = classify(evidence(summary=s, structure={"detail": 0.95}))[0]
    assert (failure.category, failure.fields) == ("partial", ["category"])
    assert "empty in every record" in failure.message


def test_null_rate_above_expectation() -> None:
    s = summary(value=FieldStats(filled=140, empty=60, winners={0: 140}))
    [failure] = classify(evidence(summary=s))[0]
    assert (failure.category, failure.fields) == ("partial", ["value"])


def test_field_much_emptier_than_last_time() -> None:
    s = summary(category=FieldStats(filled=40, empty=160, winners={0: 40}))
    assert classify(evidence(summary=s, baseline=summary()))[0][0].fields == ["category"]


def test_small_samples_do_not_judge_rates() -> None:
    # 1 of 5 sampled values missing is 20%, over the allowed 20%? No: too few to say.
    s = summary(records=5, value=FieldStats(filled=3, empty=2, winners={0: 3}))
    assert classify(evidence(summary=s, complete=False, baseline=summary())) == ([], [])


def test_small_sample_still_catches_a_field_empty_everywhere() -> None:
    s = summary(records=5, category=FieldStats(empty=5))
    assert classify(evidence(summary=s, complete=False, baseline=summary()))[0][0].fields == [
        "category"
    ]
    # ...unless there is no good run saying the field is normally filled.
    assert classify(evidence(summary=s, complete=False)) == ([], [])


def test_fallbacks_taking_over_is_a_warning() -> None:
    s = summary(title=FieldStats(filled=200, winners={1: 200}))
    failures, warnings = classify(evidence(summary=s, baseline=summary()))
    assert failures == []
    assert warnings == ["fields working only through fallback strategies: title (200/200)"]


def test_fallbacks_that_always_won_are_not_news() -> None:
    usual = summary(title=FieldStats(filled=200, winners={1: 200}))
    assert classify(evidence(summary=usual, baseline=usual)) == ([], [])


# --- the run as a whole ---------------------------------------------------------------


def test_pagination_stopped_early() -> None:
    e = evidence(summary=summary(records=20, pages=1), baseline=summary(), next_link_missing=True)
    [failure, *_] = classify(e)[0]
    assert (failure.category, failure.fields) == ("partial", ["fetch.paginate.next"])


def test_too_few_items() -> None:
    e = evidence(
        summary=summary(records=60),
        violations=[Violation(None, "min_items", "60 records, expected at least 100")],
    )
    assert classify(e)[0] == [
        Failure(category="partial", message="60 records, expected at least 100")
    ]


def test_many_detail_pages_failing() -> None:
    s = summary(records=150, items=200)
    s.failures = [
        ItemFailure(url=f"https://example.org/{i}", reason="HTTP 500", status=500)
        for i in range(50)
    ]
    assert categories(evidence(summary=s)) == ["partial"]


def test_drift() -> None:
    s = summary(records=150, category=FieldStats(filled=80, empty=70, winners={0: 80}))
    found = classify(evidence(summary=s, baseline=summary(records=400)))[0]
    assert [f.category for f in found] == ["drift", "drift"]
    assert "150 records, last good run had 400" in found[0].message


def drift(**fields: FieldStats) -> list[str]:
    """The drift messages of a complete 200-record run against a last good run of 200,
    with these field stats now, and the defaults then."""
    now = summary()
    now.fields.update(fields)
    found = classify(evidence(summary=now, baseline=summary()))[0]
    assert {f.category for f in found} <= {"drift"}
    return [f.message for f in found]


@pytest.mark.parametrize(("empty", "drifted"), [(10, False), (11, True), (39, True)])
def test_a_field_more_than_5_points_emptier_is_drift(empty: int, drifted: bool) -> None:
    # Review attack S2: status went from 0% to 19.5% empty, which used to be ok.
    status = FieldStats(filled=200 - empty, empty=empty, winners={0: 200 - empty})
    expected = [f"category empty in {empty / 200:.0%} of records (was 0%)"] if drifted else []
    assert drift(category=status) == expected


def statuses(**counts: int) -> FieldStats:
    """A field with a small set of values, as a run counts them."""
    filled = sum(counts.values())
    return FieldStats(
        filled=filled, winners={0: filled}, domain=list(counts), counts=list(counts.values())
    )


def test_a_common_category_disappearing_is_drift() -> None:
    before = statuses(Open=100, Closed=61, Awarded=39)
    after = statuses(Open=139, Closed=61)  # every Awarded now reads Open
    found = classify(evidence(summary=summary(category=after), baseline=summary(category=before)))[
        0
    ]
    assert [f.message for f in found] == [
        "category no longer reads 'Awarded', which 39 of the last good run's 200 records had"
    ]
    assert found[0].fields == ["category"]


def test_a_rare_category_disappearing_is_not_drift() -> None:
    before = statuses(Open=100, Closed=91, Withdrawn=9)  # 4.5% of records
    after = statuses(Open=109, Closed=91)
    assert classify(
        evidence(summary=summary(category=after), baseline=summary(category=before))
    ) == (
        [],
        [],
    )


def test_a_field_with_few_values_reading_many_is_drift() -> None:
    before = statuses(Open=100, Closed=61, Awarded=39)
    after = FieldStats(filled=200, winners={0: 200}, domain=None)  # more than 20 values
    found = classify(evidence(summary=summary(category=after), baseline=summary(category=before)))[
        0
    ]
    assert [f.message for f in found] == [
        "category reads more than 20 different values; the last good run had 3"
    ]


def amounts(**digits: int) -> FieldStats:
    """A number field: how many values have how many digits before the point."""
    filled = sum(digits.values())
    by_digits = {int(name.removeprefix("d")): n for name, n in digits.items()}
    return FieldStats(filled=filled, empty=200 - filled, winners={0: filled}, digits=by_digits)


def test_numbers_shifting_in_magnitude_is_drift() -> None:
    # Review attack S1: every value over 1M lost its millions, 29 of 184 values.
    before = amounts(d5=94, d6=61, d7=29)
    after = amounts(d5=94, d6=90)
    found = classify(evidence(summary=summary(value=after), baseline=summary(value=before)))[0]
    assert [f.message for f in found] == [
        "value: 16% of its values have another number of digits than in the last good run "
        "(7 digits: 0%, was 16%)"
    ]


def test_numbers_shifting_a_little_are_not_drift() -> None:
    before = amounts(d5=94, d6=61, d7=29)
    after = amounts(d5=94, d6=71, d7=19)  # 10 of 184 values: 5%
    assert classify(evidence(summary=summary(value=after), baseline=summary(value=before))) == (
        [],
        [],
    )


def test_rates_need_enough_records_to_drift() -> None:
    # One record of ten is ten points; with this few records, that is chance.
    s = summary(records=10, category=FieldStats(filled=8, empty=2, winners={0: 8}))
    assert classify(evidence(summary=s, baseline=summary(records=10))) == ([], [])


def test_field_stats_count_values_and_digits() -> None:
    stats = FieldStats()
    for value in ["Open", "Closed", "Open", None]:
        stats.add(FieldResult(value, value, 0 if value else None), [])
    assert (stats.domain, stats.counts, stats.empty) == (["Open", "Closed"], [2, 1], 1)
    numbers = FieldStats()
    for amount in ["0.50", "21400.00", "1234567.00", "99999", "-120"]:
        numbers.add(FieldResult(Decimal(amount), amount, 0), [])
    assert numbers.digits == {0: 1, 5: 2, 7: 1, 3: 1}


def test_drift_needs_a_complete_run() -> None:
    assert classify(evidence(summary=summary(records=10), baseline=summary(), complete=False)) == (
        [],
        [],
    )


def test_worst() -> None:
    drift = Failure(category="drift", message="")
    blocked = Failure(category="blocked", message="")
    assert worst([]) == "ok"
    assert worst([drift, blocked]) == "blocked"


# --- diagnosis ------------------------------------------------------------------------


def test_diagnose_compares_the_same_pages() -> None:
    before: dict[str, dict[str, tuple[object, int | None]]] = {
        "u1": {"title": ("A", 0), "category": ("Roads", 0), "value": (5, 0)},
        "u2": {"title": ("B", 0), "category": ("Roads", 0), "value": (None, None)},
    }
    now: dict[str, dict[str, tuple[object, int | None]]] = {
        "u1": {
            "title": ("A", 1),
            "category": (None, None),
            "value": (6, 0),
        },  # volatile change is fine
        "u2": {"title": ("B", 1), "category": ("Water", 0), "value": (None, None)},  # wrong element
    }
    d = diagnose(SPEC, before, now)
    assert (d.broken, d.fallback, d.intact) == (["category"], ["title"], ["value"])
    assert d.pages == ["u1", "u2"]
