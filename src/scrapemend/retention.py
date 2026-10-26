"""Snapshot retention: a source keeps the snapshots of its newest `snapshots.keep_runs`
runs, and of every older run something still needs:

- the last good run: runs and repairs are compared with it, and repair providers
  are shown its pages;
- the run held for accept-drift or confirm, which would make it the last good run:
  a drift run, or a provisional version's run;
- the failed run of a proposal that can still be promoted (accepted or unverified):
  promote verifies it again on that run's pages;
- the last failed run: heal repairs it by default, on its pages.

The snapshots of every other older run are deleted. Runs prune after they end
(see runner.run_source), and `scrapemend prune` does the same on demand.
"""

from dataclasses import dataclass, field

from scrapemend.snapshots import SnapshotStore
from scrapemend.spec import SourceSpec
from scrapemend.state import State

PROMOTABLE = ("accepted", "unverified")


@dataclass(frozen=True)
class Pruned:
    deleted: list[str] = field(default_factory=list)  # runs whose snapshots went, oldest first
    needed: dict[str, str] = field(default_factory=dict)  # older runs kept anyway, and why


def prune(
    spec: SourceSpec, state: State, snapshots: SnapshotStore, *, dry_run: bool = False
) -> Pruned:
    """Delete the snapshots of the source's runs older than its newest
    `snapshots.keep_runs`, unless they are needed. `dry_run` only says which."""
    runs = snapshots.runs(spec.id)
    older = runs[: -spec.snapshots.keep_runs]
    reasons = needed(state, spec.id)
    deleted = [run_id for run_id in older if run_id not in reasons]
    if not dry_run:
        for run_id in deleted:
            snapshots.delete_run(spec.id, run_id)
    return Pruned(deleted, {run_id: reasons[run_id] for run_id in older if run_id in reasons})


def needed(state: State, source_id: str) -> dict[str, str]:
    """The runs whose snapshots must stay, and why."""
    reasons: dict[str, list[str]] = {}
    good = state.runs.last_good(source_id)
    if good is not None:
        reasons.setdefault(good.run_id, []).append("the last good run")
    held = state.anchors.held(source_id)
    if held is not None:
        report = state.runs.get(held)
        if report is not None and report.status == "ok":
            why = f"the run that confirming v{report.spec_version} makes the last good run"
        else:
            why = "the drift run that can be accepted"
        reasons.setdefault(held, []).append(why)
    for proposal in reversed(state.proposals.for_source(source_id)):  # oldest first
        if proposal.status in PROMOTABLE:
            reasons.setdefault(proposal.run_id, []).append(
                f"proposal {proposal.id} can still be promoted"
            )
    failed = state.runs.last_failed(source_id)
    if failed is not None:
        reasons.setdefault(failed.run_id, []).append("the last failed run")
    return {run_id: "; ".join(why) for run_id, why in reasons.items()}
