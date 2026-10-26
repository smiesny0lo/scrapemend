"""Snapshot retention: the newest runs' pages are kept, and every page something needs."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from specmend.detect.classify import Failure, Status
from specmend.heal.anchors import Anchor
from specmend.heal.verify import Check, CheckStatus, VerificationReport
from specmend.pipeline import RunSummary
from specmend.retention import prune
from specmend.runs import RunMode, RunReport
from specmend.snapshots import FilesystemSnapshotStore, Snapshot
from specmend.spec import SourceSpec
from specmend.spec.patch import SpecPatch
from specmend.state import State

T0 = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


def spec(keep_runs: int = 10) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": "demo",
            "fetch": {"start": "https://example.org/list"},
            "item": {"container": {"css": "li"}, "follow": {"css": "a"}},
            "fields": {"title": {"strategies": [{"css": "h1"}]}},
            "snapshots": {"keep_runs": keep_runs},
        }
    )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.state = State(tmp_path / "state.db")
        self.store = FilesystemSnapshotStore(tmp_path / "snapshots")

    def run(self, n: int, status: Status = "ok", mode: RunMode = "run") -> str:
        """Run n of the source: its report, a snapshot, and, for an ok run, the
        baseline it becomes. Run ids sort by time, as real ones do."""
        run_id = f"run{n:02d}"
        started = T0 + timedelta(hours=n)
        category = "drift" if status == "drift" else "layout_changed"
        self.state.runs.save(
            RunReport(
                run_id=run_id,
                source_id="demo",
                spec_version=1,
                mode=mode,
                status=status,
                started_at=started,
                finished_at=started + timedelta(minutes=1),
                complete=mode == "run",
                summary=RunSummary(source_id="demo", spec_version=1, run_id=run_id),
                failures=[] if status == "ok" else [Failure(category=category, message="x")],
            )
        )
        self.store.put(
            Snapshot(
                source_id="demo",
                run_id=run_id,
                spec_version=1,
                kind="listing",
                url="https://example.org/list",
                status=200,
                body=b"<ul></ul>",
            )
        )
        if status == "drift":
            anchor = Anchor(
                key="k", url=None, values={}, raw={}, strategies={}, snapshot_id=None, run_id=run_id
            )
            self.state.anchors.hold("demo", run_id, [anchor], {"k": {}})
        return run_id

    def propose(self, run_id: str, verdict: CheckStatus) -> int:
        """A proposal for a failed run, verified with the given verdict."""
        report = VerificationReport(checks=[Check(name="shadow run", status=verdict, reason="")])
        proposal = self.state.proposals.add("demo", 1, run_id, "test", SpecPatch(), report)
        return proposal.id

    @property
    def runs(self) -> list[str]:
        return self.store.runs("demo")


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.state.close()


def checks(world: World, first: int, last: int) -> list[str]:
    """Check runs n (sampled, ok): neither a baseline nor a failure."""
    return [world.run(n, mode="check") for n in range(first, last + 1)]


def test_the_newest_runs_are_kept(world: World) -> None:
    good = world.run(1)
    newer = checks(world, 2, 15)
    pruned = prune(spec(keep_runs=10), world.state, world.store)
    assert pruned.deleted == newer[:4]
    assert world.runs == [good, *newer[4:]]
    assert world.store.snapshots("demo", newer[0]) == []


def test_a_dry_run_deletes_nothing(world: World) -> None:
    world.run(1)
    newer = checks(world, 2, 15)
    pruned = prune(spec(keep_runs=10), world.state, world.store, dry_run=True)
    assert pruned.deleted == newer[:4]
    assert len(world.runs) == 15


def test_the_last_good_runs_snapshots_are_never_deleted(world: World) -> None:
    older = world.run(1)
    good = world.run(2)  # the last good run: runs and repairs are compared with it
    newer = checks(world, 3, 6)
    pruned = prune(spec(keep_runs=1), world.state, world.store)
    assert pruned.needed == {good: "the last good run"}
    assert pruned.deleted == [older, *newer[:-1]]
    assert world.runs == [good, newer[-1]]


def test_the_snapshots_of_a_proposal_that_can_still_be_promoted_are_never_deleted(
    world: World,
) -> None:
    # promote verifies a proposal again on its failed run's pages.
    world.run(1)
    failed = [world.run(n, "layout_changed") for n in (2, 3, 4, 5)]
    accepted, unverified, rejected, promoted = failed
    first = world.propose(accepted, "passed")
    second = world.propose(unverified, "not run")
    world.propose(rejected, "failed")  # can never be promoted
    world.state.proposals.mark_promoted(world.propose(promoted, "passed"), 2)
    last = world.run(6, "layout_changed")
    pruned = prune(spec(keep_runs=1), world.state, world.store)
    assert pruned.needed == {
        "run01": "the last good run",
        accepted: f"proposal {first} can still be promoted",
        unverified: f"proposal {second} can still be promoted",
    }
    assert pruned.deleted == [rejected, promoted]
    assert world.runs == ["run01", accepted, unverified, last]


def test_the_last_failed_runs_snapshots_are_never_deleted(world: World) -> None:
    # heal repairs the latest run by default, on its pages: a failed check run too.
    world.run(1)
    older = world.run(2, "layout_changed")
    failed = world.run(3, "partial", mode="check")
    newer = checks(world, 4, 6)
    pruned = prune(spec(keep_runs=2), world.state, world.store)
    assert pruned.needed == {"run01": "the last good run", failed: "the last failed run"}
    assert pruned.deleted == [older, newer[0]]


def test_a_drift_run_that_can_be_accepted_keeps_its_snapshots(world: World) -> None:
    # Accepting it makes it the last good run, whose snapshots are never deleted.
    world.run(1)
    drifted = world.run(2, "drift")
    checks(world, 3, 4)
    pruned = prune(spec(keep_runs=1), world.state, world.store)
    assert pruned.needed == {
        "run01": "the last good run",
        drifted: "the drift run that can be accepted",
    }


def test_one_run_can_be_needed_for_several_reasons(world: World) -> None:
    world.run(1)
    failed = world.run(2, "layout_changed")
    proposal = world.propose(failed, "passed")
    checks(world, 3, 3)
    pruned = prune(spec(keep_runs=1), world.state, world.store)
    assert pruned.needed[failed] == (
        f"proposal {proposal} can still be promoted; the last failed run"
    )


def test_snapshots_keep_runs_must_keep_one(world: World) -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        spec(keep_runs=0)
    default = SourceSpec.model_validate(spec().model_dump(mode="json") | {"snapshots": {}})
    assert default.snapshots.keep_runs == 10
