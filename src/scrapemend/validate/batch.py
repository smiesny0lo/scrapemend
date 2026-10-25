"""Batch expectations: does the run as a whole look like a healthy scrape?"""

from collections.abc import Mapping
from typing import Protocol

from scrapemend.spec.models import ExpectSpec
from scrapemend.validate.record import Violation

MIN_RATE_RECORDS = 20  # fewer records than this, and a rate is mostly chance


class FieldCounts(Protocol):
    @property
    def empty(self) -> int: ...


def validate_batch(
    records: int,
    fields: Mapping[str, FieldCounts],
    expect: ExpectSpec,
    *,
    complete: bool,
) -> list[Violation]:
    """Check min_items_per_run and max_null_rate.

    The item minimum only means something for a complete run (not one cut short by
    --limit or a sample check). Null rates are checked for complete runs, or from
    MIN_RATE_RECORDS records up, where a rate stops being dominated by chance.
    """
    violations = []
    if complete and records < expect.min_items_per_run:
        violations.append(
            Violation(
                None,
                "min_items",
                f"{records} records, expected at least {expect.min_items_per_run}",
            )
        )
    if records >= MIN_RATE_RECORDS or (complete and records > 0):
        for name, allowed in expect.max_null_rate.items():
            rate = fields[name].empty / records
            if rate > allowed:
                violations.append(
                    Violation(
                        name,
                        "null_rate",
                        f"{name} empty in {rate:.0%} of records (allowed {allowed:.0%})",
                    )
                )
    return violations
