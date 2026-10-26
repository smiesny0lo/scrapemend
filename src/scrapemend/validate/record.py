"""Record validation: does each extracted record satisfy its spec?"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from scrapemend.extract import RecordResult, patterns
from scrapemend.extract.patterns import RegexTimeout, compile_pattern
from scrapemend.spec import FieldSpec

ViolationKind = Literal["missing", "unusable", "format", "min_items", "null_rate"]

# A record with one of these is not fit to be loaded downstream.
DISQUALIFYING: frozenset[ViolationKind] = frozenset({"missing", "format"})


@dataclass(frozen=True)
class Violation:
    field: str | None  # None for run-level problems
    kind: ViolationKind
    message: str


def validate_record(result: RecordResult, fields: Mapping[str, FieldSpec]) -> list[Violation]:
    """Required fields present, found text usable, values in the expected format."""
    violations = []
    for name, spec in fields.items():
        field = result.fields[name]
        if field.error is not None:
            violations.append(Violation(name, "unusable", field.error))
        if field.value is None:
            if spec.required:
                violations.append(Violation(name, "missing", f"required field {name} is empty"))
        elif spec.pattern is not None and isinstance(field.value, str):
            try:  # with Python's re, so a search on page text may time out
                matches = compile_pattern(spec.pattern, "re").fullmatch(field.value)
            except RegexTimeout:
                limit = f"{patterns.TIME_LIMIT:g}s"
                message = f"{name}: pattern {spec.pattern!r} took longer than {limit}"
                violations.append(Violation(name, "format", message))
                continue
            if not matches:
                message = f"{name} {field.value!r} does not match {spec.pattern!r}"
                violations.append(Violation(name, "format", message))
    return violations
