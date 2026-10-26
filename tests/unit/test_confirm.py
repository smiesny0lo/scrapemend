"""A version promoted automatically is provisional: its runs don't become the baseline
until a human confirms it."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scrapemend.detect.classify import Failure, Status
from scrapemend.heal.anchors import Anchor
from scrapemend.pipeline import RunSummary
from scrapemend.runner import ConfirmRefused, accept_drift, confirm
from scrapemend.runs import RunReport
from scrapemend.spec import SourceSpec
from scrapemend.state import State

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def spec(version: int) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": "demo",
            "version": version,
            "fetch": {"start": "https://example.org/list"},
            "item": {"container": {"css": "li"}, "follow": {"css": "a"}},
            "fields": {"price": {"strategies": [{"css": f".price-v{version}"}]}},
        }
    )


def report(n: int, version: int, status: Status = "ok", provisional: bool = False) -> RunReport:
    started = T0 + timedelta(hours=n)
    return RunReport(
        run_id=f"run{n}",
        source_id="demo",
        spec_version=version,
        mode="run",
        status=status,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        complete=True,
        summary=RunSummary(
            source_id="demo",
            spec_version=version,
            run_id=f"run{n}",
            records=n,
            fingerprints={"listing": [f"listing-{n}"]},
        ),
        failures=[] if status == "ok" else [Failure(category="drift", message="x")],
        provisional=provisional,
    )


def anchor(n: int) -> Anchor:
    return Anchor(
        key="https://example.org/d/1",
        url="https://example.org/d/1",
        values={"price": str(n)},
        raw={"price": str(n)},
        strategies={"price": 0},
        snapshot_id=None,
        run_id=f"run{n}",
    )


def records(n: int) -> dict[str, dict[str, str | int | None]]:
    return {"https://example.org/d/1": {"price": str(n)}}


def run(state: State, n: int, version: int, status: Status = "ok") -> RunReport:
    """What run_source keeps of a complete run: an ok run of a confirmed version becomes
    the baseline; a drift run's anchors and records, or a provisional version's ok
    run's, are held."""
    provisional = state.specs.is_provisional("demo", version)
    saved = report(n, version, status, provisional)
    state.runs.save(saved)
    if status == "ok" and not provisional:
        state.anchors.replace("demo", [anchor(n)], records(n))
        state.runs.set_baseline("demo", "listing", f"listing-{n}", f"run{n}")
    else:
        state.anchors.hold("demo", f"run{n}", [anchor(n)], records(n))
    return saved


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    """v1 from a spec file with a good run 1; v2 promoted automatically."""
    s = State(tmp_path / "state.db")
    s.specs.register(spec(1))
    run(s, 1, 1)
    s.specs.add(spec(2), "repair", "promoted automatically", provisional=True)
    s.specs.activate("demo", 2)
    yield s
    s.close()


def baseline(state: State) -> tuple[str | None, str | None, object]:
    """The last good run, its listing fingerprint, the known price."""
    good = state.runs.last_good("demo")
    price = state.anchors.records("demo")["https://example.org/d/1"]["price"]
    return good.run_id if good else None, state.runs.baseline("demo", "listing"), price


def test_a_version_promoted_automatically_is_provisional(state: State) -> None:
    assert state.specs.is_provisional("demo", 2)
    assert not state.specs.is_provisional("demo", 1)
    v2 = state.specs.get("demo", 2)
    assert v2 is not None and v2.provisional
    assert [v.provisional for v in state.specs.history("demo")] == [False, True]


def test_a_provisional_versions_runs_never_become_the_baseline_by_themselves(
    state: State,
) -> None:
    run(state, 2, 2)
    run(state, 3, 2)
    assert baseline(state) == ("run1", "listing-1", "1")
    assert state.anchors.held("demo") == "run3"  # the latest, for confirm


def test_confirming_makes_the_latest_good_run_of_the_version_the_baseline(state: State) -> None:
    run(state, 2, 2)
    run(state, 3, 2)
    confirmed = confirm(state, "demo", 2)
    assert confirmed is not None and confirmed.run_id == "run3"
    assert confirmed.accepted_at is not None
    assert baseline(state) == ("run3", "listing-3", "3")
    assert not state.specs.is_provisional("demo", 2)
    run(state, 4, 2)  # an ok run of a confirmed version: the baseline, as for any
    assert baseline(state) == ("run4", "listing-4", "4")


def test_confirming_before_a_good_run_waits_for_the_next(state: State) -> None:
    assert confirm(state, "demo", 2) is None
    assert baseline(state) == ("run1", "listing-1", "1")
    run(state, 2, 2)
    assert baseline(state) == ("run2", "listing-2", "2")


def test_a_held_drift_run_is_left_to_accept_drift(state: State) -> None:
    run(state, 2, 2)
    run(state, 3, 2, "drift")  # replaces run 2's hold
    assert confirm(state, "demo", 2) is None
    assert baseline(state) == ("run1", "listing-1", "1")
    accept_drift(state, "run3")
    assert baseline(state) == ("run3", "listing-3", "3")


def test_accepting_drift_on_a_provisional_version_confirms_it(state: State) -> None:
    # A human who accepts a run's data has checked the version that read it.
    run(state, 2, 2, "drift")
    accept_drift(state, "run2")
    assert not state.specs.is_provisional("demo", 2)
    run(state, 3, 2)
    assert baseline(state) == ("run3", "listing-3", "3")


def test_only_a_provisional_version_can_be_confirmed(state: State) -> None:
    with pytest.raises(ConfirmRefused, match="v1 was not promoted automatically"):
        confirm(state, "demo", 1)
    with pytest.raises(ConfirmRefused, match="demo has no version 9"):
        confirm(state, "demo", 9)
    confirm(state, "demo", 2)
    with pytest.raises(ConfirmRefused, match="v2 is confirmed already"):
        confirm(state, "demo", 2)
