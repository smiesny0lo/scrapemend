"""Regexes with a bounded running time: RE2, and Python's re behind a time limit."""

import time
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from specmend.extract import Document, Extractor, FieldResult, RecordResult, patterns
from specmend.extract.patterns import RegexTimeout, compile_pattern, problem
from specmend.extract.strategies import RegexStrategy, StrategyTimeout
from specmend.spec import FieldSpec, RegexSpec, SourceSpec, load_spec
from specmend.spec.patch import SpecPatch
from specmend.validate import validate_record
from tests.unit.test_patch import DEMO

# Catastrophic backtracking: on a line of words that ends in punctuation, Python's re
# tries every way of splitting the words before it gives up (review attack R).
CATASTROPHIC = r"^(\w+\s?)+$"
SENTENCE = "The contract covers supply and installation over twelve long months, in Dunmere."


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> float:
    """A short time limit, so a test of it doesn't wait a second per search."""
    monkeypatch.setattr(patterns, "TIME_LIMIT", 0.25)
    return 0.25


@pytest.mark.parametrize("engine", ["re", "re2"])
@pytest.mark.parametrize(
    ("regex", "text", "found"),
    [
        (r"Ref\.\s*(TN-\d{4}-\d{4})", "Ref. TN-2026-0001 · Open", "TN-2026-0001"),
        (r"TN-\d+", "Ref. TN-2026", "TN-2026"),  # no group: the whole match
        (r"Status: (\w+)", "no status here", None),
        (r"a(b)?", "a", None),  # a group that took no part
    ],
)
def test_both_engines_find_the_first_value(
    engine: patterns.Engine, regex: str, text: str, found: str | None
) -> None:
    assert compile_pattern(regex, engine).first(text) == found


@pytest.mark.parametrize("engine", ["re", "re2"])
def test_fullmatch(engine: patterns.Engine) -> None:
    pattern = compile_pattern(r"TN-\d{4}-\d{4}", engine)
    assert pattern.fullmatch("TN-2026-0001")
    assert not pattern.fullmatch("TN-2026-0001 ")


def test_re_keeps_pythons_semantics_and_re2_has_its_own() -> None:
    # Hand-written specs run on Python's re as they always did; RE2's \w is ASCII.
    assert compile_pattern(r"(\w+)", "re").first("Kč 12") == "Kč"
    assert compile_pattern(r"(\w+)", "re2").first("Kč 12") == "K"


def test_re2_is_linear_where_re_backtracks_catastrophically() -> None:
    started = time.monotonic()
    assert compile_pattern(CATASTROPHIC, "re2").first(SENTENCE * 100) is None
    assert time.monotonic() - started < 1


def test_re_is_stopped_at_the_time_limit(quick: float) -> None:
    started = time.monotonic()
    with pytest.raises(RegexTimeout):
        compile_pattern(CATASTROPHIC, "re").first(SENTENCE)
    assert time.monotonic() - started < quick + 2
    # The next search gets a fresh worker and works.
    assert compile_pattern(r"(\w+)$", "re").first("Fenwick Vale") == "Vale"


@pytest.mark.parametrize(
    ("regex", "found"),
    [
        (r"(\w)\1", "invalid escape sequence: \\1"),  # a backreference
        (r"Status:(?=\s)\s*(\w+)", "invalid perl operator: (?="),  # a lookahead
        (r"(?<=Ref\. )TN-\d+", "invalid perl operator: (?<"),  # a lookbehind
    ],
)
def test_re2_names_what_it_cannot_compile(regex: str, found: str) -> None:
    assert problem(regex, "re") is None
    message = problem(regex, "re2")
    assert message is not None and found in message


def test_a_regex_strategy_runs_on_its_engine() -> None:
    assert RegexSpec(regex="x").engine == "re"  # hand-written specs: Python's re
    with pytest.raises(ValidationError, match="RE2 can't compile"):
        RegexSpec(regex=r"(\w)\1", engine="re2")
    assert RegexSpec(regex=r"(\w)\1").regex == r"(\w)\1"


def test_a_regex_that_times_out_falls_through_and_is_counted(quick: float) -> None:
    # Like review attack R: the regex comes first, a selector that works second.
    spec = FieldSpec.model_validate(
        {"strategies": [{"regex": CATASTROPHIC, "within": {"css": "p"}}, {"css": "p"}]}
    )
    extractor = Extractor({"description": spec})
    page = Document.parse(f"<p>{SENTENCE}</p>")
    started = time.monotonic()
    results = [extractor.extract(page).fields["description"] for _ in range(6)]
    assert results == [FieldResult(SENTENCE, SENTENCE, 1)] * 6
    assert extractor.timeouts == {("description", 0): 6}
    # It is tried 3 times (MAX_TIMEOUTS); after that it fails at once, for this run.
    assert time.monotonic() - started < 3 * quick + 3


def test_a_regex_strategy_gives_up_after_three_timeouts(quick: float) -> None:
    strategy = RegexStrategy(RegexSpec(regex=CATASTROPHIC))
    page = Document.parse(f"<p>{SENTENCE}</p>")
    for _ in range(3):
        with pytest.raises(StrategyTimeout, match=r"took longer than 0.25s"):
            strategy.extract(page)
    started = time.monotonic()
    with pytest.raises(StrategyTimeout, match="timed out 3 times; not tried again"):
        strategy.extract(page)
    assert time.monotonic() - started < quick


def test_a_patch_from_a_provider_runs_its_regexes_on_re2() -> None:
    hand_written = SpecPatch.model_validate(
        {"fields": {"status": {"strategies": [{"regex": r"Status:\s*(\w+)"}, {"css": "dd"}]}}}
    )
    untrusted = hand_written.with_regex_engine("re2")
    [regex, css] = untrusted.fields["status"].strategies
    assert isinstance(regex, RegexSpec) and regex.engine == "re2"
    assert css == hand_written.fields["status"].strategies[1]
    applied: SourceSpec = untrusted.apply(DEMO, version=2)
    assert applied.fields["status"].strategies[0] == regex  # the engine is part of the spec


def test_a_patch_from_a_provider_with_a_regex_re2_cannot_compile_is_refused() -> None:
    patch = SpecPatch.model_validate(
        {"fields": {"status": {"strategies": [{"css": "dd"}, {"regex": r"(\w)\1"}]}}}
    )
    with pytest.raises(
        ValueError,
        match=r"fields\.status\.strategies\[1\]: RE2 can't compile '\(\\\\w\)\\\\1': invalid "
        r"escape sequence",
    ):
        patch.with_regex_engine("re2")


def test_a_spec_shows_which_regexes_run_on_re2(tmp_path: Path) -> None:
    data = DEMO.model_dump(mode="json")
    data["fields"]["status"]["strategies"] = [{"regex": r"Status:\s*(\w+)", "engine": "re2"}]
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    loaded = load_spec(path).fields["status"].strategies[0]
    assert isinstance(loaded, RegexSpec) and loaded.engine == "re2"


def test_a_validation_pattern_that_times_out_is_a_format_violation(quick: float) -> None:
    fields = {
        "note": FieldSpec.model_validate({"strategies": [{"css": "p"}], "pattern": CATASTROPHIC})
    }
    result = RecordResult(url=None, fields={"note": FieldResult(SENTENCE, SENTENCE, 0)})
    [violation] = validate_record(result, fields)
    assert (violation.field, violation.kind) == ("note", "format")
    assert violation.message == f"note: pattern {CATASTROPHIC!r} took longer than 0.25s"
