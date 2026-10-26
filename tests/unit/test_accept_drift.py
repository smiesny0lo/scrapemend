"""Only an ok run becomes the baseline; a drift run does only when a human accepts it."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scrapemend.detect.classify import Failure, Status
from scrapemend.heal.anchors import Anchor
from scrapemend.pipeline import RunSummary
from scrapemend.runner import DriftNotAccepted, accept_drift
from scrapemend.runs import RunReport
from scrapemend.state import State

T0 = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def report(n: int, status: Status = "ok") -> RunReport:
    started = T0 + timedelta(hours=n)
    category = "drift" if status == "drift" else "partial"
    return RunReport(
        run_id=f"run{n}",
        source_id="demo",
        spec_version=1,
        mode="run",
        status=status,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        complete=True,
        summary=RunSummary(
            source_id="demo",
            spec_version=1,
            run_id=f"run{n}",
            records=n,
            fingerprints={"listing": [f"listing-{n}"], "detail": [f"detail-{n}"]},
        ),
        failures=[] if status == "ok" else [Failure(category=category, message="x")],
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


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    """An ok run 1, the baseline, then whatever a test adds."""
    s = State(tmp_path / "state.db")
    run(s, 1, "ok")
    yield s
    s.close()


def run(state: State, n: int, status: Status) -> None:
    """What run_source keeps of a complete run: an ok run becomes the baseline, a
    drift run's anchors and records are held for accept-drift."""
    state.runs.save(report(n, status))
    if status == "ok":
        state.anchors.replace("demo", [anchor(n)], records(n))
        for kind in ("listing", "detail"):
            state.runs.set_baseline("demo", kind, f"{kind}-{n}", f"run{n}")
    elif status == "drift":
        state.anchors.hold("demo", f"run{n}", [anchor(n)], records(n))


def baseline(state: State) -> tuple[str | None, set[str], dict[str, object], str | None]:
    """The last good run, its anchors' runs, the known records, the listing fingerprint."""
    good = state.runs.last_good("demo")
    return (
        good.run_id if good else None,
        {a.run_id for a in state.anchors.all("demo")},
        dict(state.anchors.records("demo")),
        state.runs.baseline("demo", "listing"),
    )


def test_a_drift_run_is_not_the_baseline(state: State) -> None:
    run(state, 2, "drift")
    run(state, 3, "drift")
    assert baseline(state) == ("run1", {"run1"}, records(1), "listing-1")
    assert state.anchors.held("demo") == "run3"  # only the latest drift run's are kept


def test_accepting_a_drift_run_makes_it_the_baseline(state: State) -> None:
    run(state, 2, "drift")
    accepted = accept_drift(state, "run2")
    assert (accepted.status, accepted.accepted_at is not None) == ("drift", True)
    assert baseline(state) == ("run2", {"run2"}, records(2), "listing-2")
    assert state.runs.baseline("demo", "detail") == "detail-2"
    assert state.anchors.held("demo") is None
    stored = state.runs.get("run2")
    assert stored is not None and stored.accepted_at == accepted.accepted_at
    # The next ok run is the baseline after it, as always.
    run(state, 3, "ok")
    assert baseline(state) == ("run3", {"run3"}, records(3), "listing-3")


def test_an_ok_run_after_a_drift_run_ends_what_could_be_accepted(state: State) -> None:
    run(state, 2, "drift")
    run(state, 3, "ok")
    assert state.anchors.held("demo") is None
    with pytest.raises(DriftNotAccepted, match="run2 is older than the last good run, run3"):
        accept_drift(state, "run2")
    assert baseline(state) == ("run3", {"run3"}, records(3), "listing-3")


def test_only_the_latest_drift_run_can_be_accepted(state: State) -> None:
    run(state, 2, "drift")
    run(state, 3, "drift")
    run(state, 4, "layout_changed")  # a later failure doesn't change which drift run is held
    with pytest.raises(
        DriftNotAccepted,
        match="only the latest drift run's records are kept: review run run3 and accept it",
    ):
        accept_drift(state, "run2")
    assert accept_drift(state, "run3").run_id == "run3"


@pytest.mark.parametrize(
    ("run_id", "message"),
    [
        ("nope", "there is no run nope"),
        ("run1", "run run1 is ok, not drift: only a drift run can be accepted"),
        ("run2", "run run2 is layout_changed, not drift: only a drift run can be accepted"),
    ],
)
def test_only_a_drift_run_can_be_accepted(state: State, run_id: str, message: str) -> None:
    run(state, 2, "layout_changed")
    with pytest.raises(DriftNotAccepted, match=message):
        accept_drift(state, run_id)
    assert baseline(state) == ("run1", {"run1"}, records(1), "listing-1")


def test_a_drift_run_is_accepted_once(state: State) -> None:
    run(state, 2, "drift")
    accept_drift(state, "run2")
    with pytest.raises(DriftNotAccepted, match="run run2 was accepted already"):
        accept_drift(state, "run2")


def test_a_drift_run_from_before_they_were_held_cannot_be_accepted(state: State) -> None:
    state.runs.save(report(2, "drift"))  # nothing held: a database from before accept-drift
    with pytest.raises(DriftNotAccepted, match="run2's records were not kept"):
        accept_drift(state, "run2")
