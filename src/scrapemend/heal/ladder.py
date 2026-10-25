"""The healing ladder: retry -> fallbacks -> repair -> verify -> promote or quarantine.

Retries (the fetcher) and fallbacks (the extractor) happen during every run. When a
run still fails with a layout problem, `heal` asks a repair provider for a patch,
verifies it on the pages under the new layout, and feeds the problems of a rejected
patch back for another attempt. Then:

- promote, in heal.mode "auto", if the failure carries structural evidence (the
  page fingerprint changed), the patch passed and its confidence is high enough;
- propose: a passed patch otherwise waits for `scrapemend promote`;
- quarantine: no patch passed, so a human has to look, and an alert goes out.

Blocked and transient failures are never repaired (the site refused or failed us,
and that is not a markup problem), and neither is drift (real-world change).
"""

from collections.abc import Callable
from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, Field

from scrapemend.detect.classify import Status
from scrapemend.events import Event, EventKind, Notifier
from scrapemend.fetch import Fetcher
from scrapemend.heal.anchors import Anchor, split
from scrapemend.heal.context import RepairContext, examples, prune_document, spec_yaml
from scrapemend.heal.pages import PageSource
from scrapemend.heal.proposals import Proposal
from scrapemend.heal.providers import ProviderError, RepairProvider
from scrapemend.heal.verify import FieldCheck, VerificationReport, verify
from scrapemend.pipeline import ValidatedRecord
from scrapemend.runner import run_source
from scrapemend.runs import RunReport
from scrapemend.snapshots import SnapshotStore
from scrapemend.spec import SourceSpec
from scrapemend.spec.store import SpecVersion
from scrapemend.state import State

HEALABLE: tuple[Status, ...] = ("layout_changed", "empty", "partial")
MIN_AUTO_CONFIDENCE = 0.5
SHOWN_ANCHORS = 3  # anchors a provider sees; the rest are held out to catch overfitting
DEFAULT_ATTEMPTS = 2

NOT_REPAIRED = {
    "blocked": "the site refused us; scrapemend never works around blocks",
    "transient": "the site failed us; run again later",
    "drift": "drift is real-world change, so it is reported, not repaired",
}

HealStatus = Literal["promoted", "proposed", "quarantined", "skipped"]


class HealOutcome(BaseModel):
    source_id: str
    status: HealStatus
    message: str
    run_id: str | None = None
    proposals: list[Proposal] = Field(default_factory=list)
    version: int | None = None  # the newly active version, when promoted


def heal(
    spec: SourceSpec,
    state: State,
    snapshots: SnapshotStore,
    fetcher: Fetcher,
    provider: RepairProvider,
    *,
    run_id: str | None = None,
    mode: Literal["propose", "auto"] | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    notifier: Notifier | None = None,
) -> HealOutcome:
    """Try to repair the failure of a run (by default the source's latest run).

    `spec` is the active spec version (possibly with a runtime --start override);
    `mode` defaults to the spec's heal.mode, with "off" treated as "propose".
    """
    failed = state.runs.get(run_id) if run_id else state.runs.latest(spec.id)
    if failed is None or failed.source_id != spec.id:
        return _skip(spec, "no failed run to repair")
    if failed.good:
        return _skip(spec, f"run {failed.run_id} is {failed.status}: nothing to repair", failed)
    if failed.status in NOT_REPAIRED:
        return _skip(spec, f"not repaired: {NOT_REPAIRED[failed.status]}", failed)
    if failed.spec_version != spec.version:
        return _skip(
            spec,
            f"run {failed.run_id} used v{failed.spec_version}, the active spec is "
            f"v{spec.version}: run again to see whether it still fails",
            failed,
        )
    mode = mode or ("auto" if spec.heal.mode == "auto" else "propose")

    def alert(kind: EventKind, message: str) -> None:
        if notifier is not None:
            event = Event(kind=kind, source_id=spec.id, message=message, run_id=failed.run_id)
            notifier.notify(event)

    anchors = state.anchors.all(spec.id)
    if not anchors:
        reason = "no good run yet, so no anchors to verify a repair against"
        state.specs.quarantine(spec.id, reason)
        alert("quarantined", reason)
        return HealOutcome(
            source_id=spec.id, status="quarantined", message=reason, run_id=failed.run_id
        )

    baseline = state.runs.last_good(spec.id)
    pages = PageSource(
        snapshots, spec.id, failed.run_id, fetcher=fetcher, spec_version=spec.version
    )
    context = build_context(spec, failed, baseline, anchors, pages, snapshots)

    proposals: list[Proposal] = []
    error = None
    for _ in range(attempts):
        try:
            patch = provider.propose(context)
        except ProviderError as e:
            error = str(e)
            break
        if patch is None or patch.is_empty:
            break
        try:
            candidate = patch.apply(spec, version=state.specs.next_version(spec.id))
        except ValueError as e:
            report = _unusable(spec, anchors, f"the patch does not fit the spec: {e}")
        else:
            report = verify(
                candidate, anchors, pages, baseline=baseline.summary if baseline else None
            )
        proposal = state.proposals.add(
            spec.id, spec.version, failed.run_id, provider.name, patch, report
        )
        proposals.append(proposal)
        if report.accepted:
            break
        context = context.with_attempt(patch, report.problems)

    accepted = next((p for p in proposals if p.status == "accepted"), None)
    if accepted is None:
        if error:
            reason = f"repair provider {provider.name} failed: {error}"
        elif not proposals:
            reason = f"repair provider {provider.name} proposed nothing"
        else:
            reason = f"{len(proposals)} repair(s) failed verification: " + "; ".join(
                proposals[-1].report.problems[:3]
            )
        state.specs.quarantine(spec.id, reason)
        alert("quarantined" if not proposals else "repair_rejected", reason)
        return HealOutcome(
            source_id=spec.id,
            status="quarantined",
            message=reason,
            run_id=failed.run_id,
            proposals=proposals,
        )

    blocker = _auto_blocker(mode, failed, accepted)
    if blocker is None:
        version = promote(state, accepted)
        message = f"proposal {accepted.id} passed verification and is now v{version.version}"
        alert("repair_promoted", message)
        return HealOutcome(
            source_id=spec.id,
            status="promoted",
            message=message,
            run_id=failed.run_id,
            proposals=proposals,
            version=version.version,
        )
    message = (
        f"proposal {accepted.id} passed verification ({blocker}); "
        f"promote it with: scrapemend promote <spec> {accepted.id}"
    )
    alert("repair_proposed", message)
    return HealOutcome(
        source_id=spec.id,
        status="proposed",
        message=message,
        run_id=failed.run_id,
        proposals=proposals,
    )


def promote(state: State, proposal: Proposal) -> SpecVersion:
    """Store a verified proposal as a new spec version and make it active.

    The patch is applied to the stored version it was made for, so a runtime
    override (like --start) never leaks into the stored spec.
    """
    if proposal.status != "accepted":
        raise ValueError(f"proposal {proposal.id} is {proposal.status}, not accepted")
    base = state.specs.get(proposal.source_id, proposal.base_version)
    if base is None:
        raise ValueError(f"{proposal.source_id} v{proposal.base_version} is not stored")
    version = state.specs.next_version(proposal.source_id)
    spec = proposal.patch.apply(base.spec, version)
    note = f"proposal {proposal.id} by {proposal.provider}: {proposal.patch.rationale}".strip()
    stored = state.specs.add(spec, "repair", note[:500])
    state.specs.activate(proposal.source_id, version)
    state.proposals.mark_promoted(proposal.id, version)
    return stored


def rollback(state: State, source_id: str, to: int | None = None) -> SpecVersion:
    """Make an older version active again: by default the one before the active one."""
    active = state.specs.active(source_id)
    if active is None:
        raise ValueError(f"{source_id} has no spec versions")
    if to is None:
        older = [v.version for v in state.specs.history(source_id) if v.version < active.version]
        if not older:
            raise ValueError(f"v{active.version} is the oldest version of {source_id}")
        to = older[-1]
    state.specs.activate(source_id, to)
    version = state.specs.get(source_id, to)
    assert version is not None
    return version


def build_context(
    spec: SourceSpec,
    failed: RunReport,
    baseline: RunReport | None,
    anchors: list[Anchor],
    pages: PageSource,
    snapshots: SnapshotStore,
) -> RepairContext:
    shown, _ = split(anchors, SHOWN_ANCHORS)
    old_listing = None
    if baseline is not None:
        for snapshot_id in snapshots.snapshots(spec.id, baseline.run_id):
            snapshot = snapshots.get(snapshot_id)
            if snapshot.kind == "listing":
                old_listing = prune_document(snapshot.document(), listing=True)
                break
    listings = pages.listing_pages()
    example = shown[0] if shown else None
    old_detail = new_detail = None
    if example is not None and example.url:
        if example.snapshot_id:
            with suppress(KeyError):  # the old snapshot may have been cleaned up
                old_detail = prune_document(snapshots.get(example.snapshot_id).document())
        new_detail = prune_document(pages.page(example.url))
    return RepairContext(
        source_id=spec.id,
        spec_yaml=spec_yaml(spec),
        failures=failed.failures,
        old_listing=old_listing,
        new_listing=prune_document(listings[0], listing=True) if listings else None,
        old_detail=old_detail,
        new_detail=new_detail,
        examples=examples(shown),
    )


def run_and_heal(
    spec: SourceSpec,
    state: State,
    snapshots: SnapshotStore,
    fetcher: Fetcher,
    provider: RepairProvider,
    *,
    on_record: Callable[[ValidatedRecord], None] | None = None,
    keep_invalid: bool = False,
    limit: int | None = None,
    notifier: Notifier | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    rebase: Callable[[SourceSpec], SourceSpec] | None = None,
) -> tuple[RunReport, HealOutcome | None, RunReport | None]:
    """A run that heals itself as far as heal.mode allows.

    If the run fails with a layout problem and heal.mode isn't "off", the ladder
    runs; if it promotes a repair, the source is crawled again with the new
    version. Returns the first run, the healing outcome and the second run.
    `rebase` re-applies runtime overrides (like --start) to a newly promoted spec.
    """
    first = run_source(
        spec, fetcher, snapshots, state,
        limit=limit, keep_invalid=keep_invalid, on_record=on_record, notifier=notifier,
    )  # fmt: skip
    if first.status not in HEALABLE or spec.heal.mode == "off":
        return first, None, None
    outcome = heal(
        spec,
        state,
        snapshots,
        fetcher,
        provider,
        run_id=first.run_id,
        notifier=notifier,
        attempts=attempts,
    )
    if outcome.status != "promoted":
        return first, outcome, None
    active = state.specs.active(spec.id)
    assert active is not None
    repaired = rebase(active.spec) if rebase else active.spec
    second = run_source(
        repaired, fetcher, snapshots, state,
        limit=limit, keep_invalid=keep_invalid, on_record=on_record, notifier=notifier,
    )  # fmt: skip
    return first, outcome, second


def _auto_blocker(mode: str, failed: RunReport, proposal: Proposal) -> str | None:
    """Why an accepted proposal may not be promoted automatically, if it may not."""
    if mode != "auto":
        return "heal.mode is propose"
    if failed.status != "layout_changed":
        return f"a {failed.status} failure has no structural evidence"
    if proposal.patch.confidence < MIN_AUTO_CONFIDENCE:
        return f"its confidence {proposal.patch.confidence:.2f} is below {MIN_AUTO_CONFIDENCE}"
    return None


def _skip(spec: SourceSpec, message: str, run: RunReport | None = None) -> HealOutcome:
    return HealOutcome(
        source_id=spec.id, status="skipped", message=message, run_id=run.run_id if run else None
    )


def _unusable(spec: SourceSpec, anchors: list[Anchor], problem: str) -> VerificationReport:
    return VerificationReport(
        accepted=False,
        threshold=spec.heal.threshold,
        anchors=len(anchors),
        anchors_checked=0,
        fields={name: FieldCheck() for name in spec.fields},
        problems=[problem],
    )
