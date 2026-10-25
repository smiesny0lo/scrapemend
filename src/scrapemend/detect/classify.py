"""The failure classifier: turn what a run saw into categories with evidence.

| category       | signal                                              | response              |
|----------------|-----------------------------------------------------|-----------------------|
| blocked        | 401/403, robots.txt disallow, a bot-wall page       | stop + alert, never evade |
| transient      | timeouts, 5xx, 429 that outlasted the retries       | try again later       |
| layout_changed | page structure changed + extraction failures        | fallbacks -> repair   |
| empty          | the item container matches nothing, structure same  | check, then repair    |
| partial        | some fields or pages fail, structure unchanged      | field-level repair    |
| drift          | values parse but their distribution shifted         | alert only            |

Only layout_changed carries structural evidence (a fingerprint change), so only it
may be repaired and promoted automatically.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from scrapemend.detect.fingerprint import CHANGED_BELOW
from scrapemend.fetch import BlockedError, FetchError, RobotsDisallowed, TransientError
from scrapemend.pipeline import (
    DOMAIN_LIMIT,
    MAX_CONSECUTIVE_FAILURES,
    FieldStats,
    RunAborted,
    RunSummary,
)
from scrapemend.spec import SourceSpec
from scrapemend.validate import Violation
from scrapemend.validate.batch import MIN_RATE_RECORDS

Category = Literal["blocked", "transient", "layout_changed", "empty", "partial", "drift"]
Status = Literal["ok", "blocked", "transient", "layout_changed", "empty", "partial", "drift"]
SEVERITY: tuple[Category, ...] = (
    "blocked",
    "transient",
    "layout_changed",
    "empty",
    "partial",
    "drift",
)

FALLBACK_SHARE = 0.5  # a fallback winning this share of records means the primary broke
NULL_JUMP = 0.5  # a field this much emptier than in the last good run is failing
DRIFT_NULL_SHIFT = 0.05  # empty rates further apart than this are drift
DRIFT_COUNT_SHIFT = 0.5
DRIFT_CATEGORY_SHARE = 0.05  # of records a value must have had for its disappearing to count
DRIFT_MAGNITUDE_SHIFT = 0.1  # share of a number field's values that may change magnitude
FAILING_PAGES_SHARE = 0.1
SMALL_SAMPLE = 3  # records needed before "empty in every sampled record" means anything

_BOT_WALL = re.compile(
    r"captcha|are you (a )?(human|robot)|verify (that )?you are (a )?human|unusual traffic"
    r"|automated (requests|traffic|queries)|access denied|bot detection|just a moment",
    re.IGNORECASE,
)


class Failure(BaseModel):
    category: Category
    message: str
    fields: list[str] = Field(default_factory=list)  # spec parts that fail: fields or selectors
    pages: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Diagnosis:
    """How the current spec fares on detail pages known from a good run."""

    pages: list[str]
    broken: list[str]  # had a value before, has none (or a different one) now
    fallback: list[str]  # still found, but no longer by the first strategy
    intact: list[str]


@dataclass
class Evidence:
    spec: SourceSpec
    summary: RunSummary
    complete: bool  # the crawl ran to the end, without --limit or a sample
    error: BaseException | None = None
    structure: Mapping[str, float] = field(default_factory=dict)  # kind -> similarity
    baseline: RunSummary | None = None  # the last good run
    violations: list[Violation] = field(default_factory=list)  # batch-level
    diagnosis: Diagnosis | None = None
    listing_text: str | None = None  # text of the first listing page
    next_link_missing: bool = False  # the listing has no "next" link where it used to


def worst(failures: list[Failure]) -> Status:
    if not failures:
        return "ok"
    return min((f.category for f in failures), key=SEVERITY.index)


def classify(e: Evidence) -> tuple[list[Failure], list[str]]:
    """Failures (most severe first) and warnings that are worth an alert but no more."""
    if e.error is not None:
        return [_aborted(e)], []

    failures: list[Failure] = []
    warnings: list[str] = []
    changed = {kind: s for kind, s in e.structure.items() if s < CHANGED_BELOW}
    structure_note = _structure_note(changed)

    if e.summary.listing_pages and e.summary.items == 0:
        if e.listing_text and _BOT_WALL.search(e.listing_text):
            return [
                Failure(category="blocked", message="the listing page looks like a bot wall")
            ], []
        if "listing" in changed:
            parts = ["item.container"]
            if e.next_link_missing:
                parts.append("fetch.paginate.next")
            message = f"the item container matches nothing; {structure_note}"
            if e.diagnosis is not None:
                parts += e.diagnosis.broken
                message += _diagnosis_note(e.diagnosis)
            return [Failure(category="layout_changed", message=message, fields=parts)], []
        return [Failure(category="empty", message="the item container matched nothing")], []

    failing = _failing_fields(e.spec, e.summary, e.baseline, e.complete)
    fallback = _fallback_fields(e.summary, e.baseline)
    if failing:
        detail = "; ".join(f"{name}: {why}" for name, why in failing.items())
        if changed:
            failures.append(
                Failure(
                    category="layout_changed",
                    message=f"{structure_note}; failing fields: {detail}",
                    fields=list(failing),
                )
            )
        else:
            failures.append(
                Failure(
                    category="partial", message=f"failing fields: {detail}", fields=list(failing)
                )
            )
    if fallback:
        names = ", ".join(f"{n} ({wins}/{e.summary.records})" for n, wins in fallback.items())
        warnings.append(f"fields working only through fallback strategies: {names}")

    if e.next_link_missing and e.complete:
        category: Category = "layout_changed" if "listing" in changed else "partial"
        failures.append(
            Failure(
                category=category,
                message="pagination stopped after the first page; the next-page link is gone",
                fields=["fetch.paginate.next"],
            )
        )
    elif any(v.kind == "min_items" for v in e.violations):
        message = next(v.message for v in e.violations if v.kind == "min_items")
        failures.append(Failure(category="partial", message=message))

    failed_pages = e.summary.failures
    if e.summary.items and len(failed_pages) / e.summary.items > FAILING_PAGES_SHARE:
        failures.append(
            Failure(
                category="partial",
                message=f"{len(failed_pages)} of {e.summary.items} items could not be fetched",
                pages=[f.url for f in failed_pages if f.url][:20],
            )
        )

    if not failures and e.baseline is not None and e.complete:
        failures += _drift(e.spec, e.summary, e.baseline)

    failures.sort(key=lambda f: SEVERITY.index(f.category))
    return failures, warnings


def diagnose(
    spec: SourceSpec,
    before: Mapping[str, Mapping[str, tuple[object, int | None]]],
    now: Mapping[str, Mapping[str, tuple[object, int | None]]],
) -> Diagnosis:
    """Compare (value, winning strategy) per field on the same pages, then and now."""
    broken, fallback, intact = set(), set(), set()
    for url, old_fields in before.items():
        new_fields = now.get(url)
        if new_fields is None:
            continue
        for name, (old_value, old_strategy) in old_fields.items():
            new_value, new_strategy = new_fields[name]
            if old_value is None:
                continue
            if new_value is None or (new_value != old_value and not spec.fields[name].volatile):
                broken.add(name)
            elif old_strategy == 0 and (new_strategy or 0) > 0:
                fallback.add(name)
            else:
                intact.add(name)
    order = list(spec.fields)
    fallback -= broken
    intact -= broken | fallback
    return Diagnosis(
        pages=[url for url in before if url in now],
        broken=sorted(broken, key=order.index),
        fallback=sorted(fallback, key=order.index),
        intact=sorted(intact, key=order.index),
    )


# --- rules -------------------------------------------------------------------------------


def _aborted(e: Evidence) -> Failure:
    error = e.error
    if isinstance(error, BlockedError | RobotsDisallowed):
        return Failure(category="blocked", message=str(error), pages=[error.url])
    if isinstance(error, TransientError):
        return Failure(category="transient", message=str(error), pages=[error.url])
    if isinstance(error, RunAborted):
        if error.listing_status in (404, 410):
            return Failure(
                category="layout_changed",
                message=f"{error.reason}; the listing page moved or is gone",
                fields=["fetch.start"],
            )
        if error.listing_status is not None:
            return Failure(category="transient", message=error.reason)
        recent = e.summary.failures[-MAX_CONSECUTIVE_FAILURES:]
        pages = [f.url for f in recent if f.url]
        if all(f.transient for f in recent):
            return Failure(category="transient", message=error.reason, pages=pages)
        if all(f.url is None for f in recent):
            return Failure(category="layout_changed", message=error.reason, fields=["item.follow"])
        if all(f.status in (404, 410) for f in recent):
            return Failure(
                category="layout_changed",
                message=f"{error.reason}; detail pages are gone",
                fields=["item.follow"],
                pages=pages,
            )
        return Failure(category="partial", message=error.reason, pages=pages)
    if isinstance(error, FetchError):
        return Failure(category="transient", message=str(error), pages=[error.url])
    return Failure(category="transient", message=f"{type(error).__name__}: {error}")


def _failing_fields(
    spec: SourceSpec, summary: RunSummary, baseline: RunSummary | None, complete: bool
) -> dict[str, str]:
    """Fields that fail. Rates are only judged on complete runs or enough records; a
    small sample can still show a field empty everywhere that rarely was before."""
    failing: dict[str, str] = {}
    records = summary.records
    if not records:
        return failing
    judge_rates = complete or records >= MIN_RATE_RECORDS
    for name, field_spec in spec.fields.items():
        stats = summary.fields[name]
        rate = stats.empty / records
        allowed = spec.expect.max_null_rate.get(name)
        before = baseline.null_rate(name) if baseline and name in baseline.fields else None
        empty_everywhere = rate == 1.0 and (
            judge_rates or (records >= SMALL_SAMPLE and before is not None and before <= 0.5)
        )
        if field_spec.required and stats.empty:
            failing[name] = f"required but empty in {stats.empty}/{records} records"
        elif stats.invalid:
            failing[name] = f"invalid in {stats.invalid}/{records} records"
        elif judge_rates and allowed is not None and rate > allowed:
            failing[name] = f"empty in {rate:.0%} of records (allowed {allowed:.0%})"
        elif empty_everywhere:
            failing[name] = "empty in every record"
        elif judge_rates and before is not None and rate - before > NULL_JUMP:
            failing[name] = f"empty in {rate:.0%} of records (was {before:.0%})"
        elif judge_rates and stats.unusable / records > FAILING_PAGES_SHARE:
            failing[name] = f"unusable text in {stats.unusable}/{records} records"
    return failing


def _fallback_fields(summary: RunSummary, baseline: RunSummary | None) -> dict[str, int]:
    out = {}
    for name, stats in summary.fields.items():
        if not summary.records or stats.fallback_wins / summary.records <= FALLBACK_SHARE:
            continue
        was_so_before = (
            baseline is not None
            and name in baseline.fields
            and baseline.records > 0
            and baseline.fields[name].fallback_wins / baseline.records > FALLBACK_SHARE
        )
        if was_so_before:
            continue  # not news
        out[name] = stats.fallback_wins
    return out


def _drift(spec: SourceSpec, summary: RunSummary, baseline: RunSummary) -> list[Failure]:
    """Values that parse, but are distributed unlike the last good run's: far fewer or
    more records; and, with MIN_RATE_RECORDS records in both runs, a field's empty
    rate moving by more than DRIFT_NULL_SHIFT, a common value of a small set (a
    status, a category) gone, or a number field's values changing magnitude. After a
    repair is promoted, that is how a field wrong on some records shows."""
    drift = []
    if baseline.records:
        shift = abs(summary.records - baseline.records) / baseline.records
        if shift > DRIFT_COUNT_SHIFT:
            drift.append(
                Failure(
                    category="drift",
                    message=f"{summary.records} records, last good run had {baseline.records}",
                )
            )
    if min(summary.records, baseline.records) < MIN_RATE_RECORDS:
        return drift  # a rate of so few records is mostly chance
    for name in spec.fields:
        if name not in baseline.fields or name not in summary.fields:
            continue
        before, now = baseline.fields[name], summary.fields[name]
        messages = []
        empty_now, empty_before = summary.null_rate(name), baseline.null_rate(name)
        if abs(empty_now - empty_before) > DRIFT_NULL_SHIFT:
            messages.append(f"{name} empty in {empty_now:.0%} of records (was {empty_before:.0%})")
        messages += _values_gone(name, before, now, baseline.records)
        messages += _magnitudes(name, before.digits, now.digits)
        drift += [Failure(category="drift", message=m, fields=[name]) for m in messages]
    return drift


def _values_gone(name: str, before: FieldStats, now: FieldStats, records: int) -> list[str]:
    """A field with a small set of values: a value common in the last good run gone."""
    known = before.known_values
    if known is None or len(before.counts) != len(before.domain or []):
        return []  # not a small set, or a run from before values were counted
    if now.domain is None:
        return [
            f"{name} reads more than {DOMAIN_LIMIT} different values; "
            f"the last good run had {len(known)}"
        ]
    return [
        f"{name} no longer reads {value!r}, which {count} of the last good run's "
        f"{records} records had"
        for value, count in zip(before.domain or [], before.counts, strict=True)
        if count >= DRIFT_CATEGORY_SHARE * records and value not in now.domain
    ]


def _magnitudes(name: str, before: Mapping[int, int], now: Mapping[int, int]) -> list[str]:
    """A number field: the share of values whose number of digits changed, measured as
    the distance between the two runs' distributions of digits."""
    total_before, total_now = sum(before.values()), sum(now.values())
    if min(total_before, total_now) < MIN_RATE_RECORDS:
        return []
    shares = {
        digits: (before.get(digits, 0) / total_before, now.get(digits, 0) / total_now)
        for digits in sorted(set(before) | set(now))
    }
    shift = sum(abs(was - is_) for was, is_ in shares.values()) / 2
    if shift <= DRIFT_MAGNITUDE_SHIFT:
        return []

    def change(digits: int) -> tuple[float, float]:
        # The size that changed most; on a tie, the one that lost values (the millions gone).
        was, is_ = shares[digits]
        return round(abs(was - is_), 9), was - is_

    digits = max(shares, key=change)
    was, is_ = shares[digits]
    return [
        f"{name}: {shift:.0%} of its values have another number of digits than in the last "
        f"good run ({digits} digits: {is_:.0%}, was {was:.0%})"
    ]


def _structure_note(changed: Mapping[str, float]) -> str:
    if not changed:
        return "page structure unchanged"
    parts = ", ".join(f"{kind} pages {s:.0%} similar" for kind, s in sorted(changed.items()))
    return f"the page structure changed ({parts} to the last good run)"


def _diagnosis_note(d: Diagnosis) -> str:
    note = f". On {len(d.pages)} known detail pages:"
    if d.broken:
        note += f" broken: {', '.join(d.broken)};"
    if d.fallback:
        note += f" only via fallbacks: {', '.join(d.fallback)};"
    if d.intact:
        note += f" still working: {', '.join(d.intact)};"
    return note.rstrip(";")
