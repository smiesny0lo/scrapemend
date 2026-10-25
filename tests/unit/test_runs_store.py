from datetime import UTC, datetime, timedelta
from pathlib import Path

from scrapemend.detect.classify import Failure, Status
from scrapemend.pipeline import RunSummary
from scrapemend.runs import RunMode, RunReport, RunStore
from scrapemend.state import connect

T0 = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def report(
    n: int, status: Status = "ok", mode: RunMode = "run", complete: bool = True
) -> RunReport:
    started = T0 + timedelta(hours=n)
    failures = [] if status == "ok" else [Failure(category="partial", message="x")]
    return RunReport(
        run_id=f"run{n}",
        source_id="demo",
        spec_version=1,
        mode=mode,
        status=status,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        complete=complete,
        summary=RunSummary(source_id="demo", spec_version=1, run_id=f"run{n}", records=n),
        failures=failures,
    )


def test_round_trip(tmp_path: Path) -> None:
    runs = RunStore(connect(tmp_path / "state.db"))
    runs.save(report(1, "partial"))
    assert runs.get("run1") == report(1, "partial")
    assert runs.get("nope") is None


def test_recent_newest_first(tmp_path: Path) -> None:
    runs = RunStore(connect(tmp_path / "state.db"))
    for n in (1, 3, 2):
        runs.save(report(n))
    assert [r.run_id for r in runs.recent("demo")] == ["run3", "run2", "run1"]
    assert [r.run_id for r in runs.recent(limit=1)] == ["run3"]
    assert runs.recent("other") == []
    latest = runs.latest("demo")
    assert latest is not None and latest.run_id == "run3"


def test_last_good_skips_failures_checks_and_partial_runs(tmp_path: Path) -> None:
    runs = RunStore(connect(tmp_path / "state.db"))
    runs.save(report(1))
    runs.save(report(2, "drift"))
    runs.save(report(3, "layout_changed"))
    runs.save(report(4, mode="check"))
    runs.save(report(5, complete=False))
    good = runs.last_good("demo")
    assert good is not None and good.run_id == "run2"


def test_baselines(tmp_path: Path) -> None:
    runs = RunStore(connect(tmp_path / "state.db"))
    assert runs.baseline("demo", "listing") is None
    runs.set_baseline("demo", "listing", "aa", "run1")
    runs.set_baseline("demo", "listing", "bb", "run2")
    assert runs.baseline("demo", "listing") == "bb"


def test_state_survives_reopening(tmp_path: Path) -> None:
    RunStore(connect(tmp_path / "state.db")).save(report(1))
    assert RunStore(connect(tmp_path / "state.db")).get("run1") is not None
