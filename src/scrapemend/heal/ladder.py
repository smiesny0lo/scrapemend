"""The healing ladder: retry -> fallbacks -> repair -> verify -> promote or quarantine.

Retries (the fetcher) and fallbacks (the extractor) happen during every run. When a
run still fails with a layout problem, `heal` asks a repair provider for a patch,
verifies it on the pages under the new layout, and feeds the problems of a rejected
patch back for another attempt. Then:

- promote, in heal.mode "auto", if the failure carries structural evidence (the
  page fingerprint changed), the patch passed every check and its confidence is
  high enough. The version is provisional: its runs don't become the last good
  run until a human confirms it (scrapemend.runner.confirm);
- propose: a passed patch otherwise waits for `scrapemend promote`;
- quarantine: no patch passed, so a human has to look, and an alert goes out. A
  patch whose checks couldn't all run (a page didn't load) is kept as unverified:
  `scrapemend promote --force` may promote it, never automatically.

Blocked and transient failures are never repaired (the site refused or failed us,
and that is not a markup problem), and neither is drift (real-world change).
"""

from collections.abc import Callable
from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, Field

from scrapemend.detect.classify import Status
from scrapemend.events import Event, EventKind, Notifier
from scrapemend.fetch import Fetcher, Requests
from scrapemend.heal.anchors import Anchor, split
from scrapemend.heal.context import RepairContext, examples, prune_document, spec_yaml
from scrapemend.heal.pages import Pages, PageSource
from scrapemend.heal.proposals import Proposal
from scrapemend.heal.providers import ProviderError, RepairProvider
from scrapemend.heal.verify import Check, FieldCheck, ShadowPlan, VerificationReport, verify
from scrapemend.pipeline import ValidatedRecord
from scrapemend.runner import run_source
from scrapemend.runs import RunReport
from scrapemend.snapshots import SnapshotStore
from scrapemend.spec import SourceSpec
from scrapemend.spec.patch import SpecPatch
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
    requests: Requests | None = None  # what healing sent, if the fetcher counts


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
    on_shadow: Callable[[ShadowPlan], None] | None = None,
) -> HealOutcome:
    """Try to repair the failure of a run (by default the source's latest run).

    `spec` is the active spec version (possibly with a runtime --start override);
    `mode` defaults to the spec's heal.mode, with "off" treated as "propose".
    `on_shadow` is told what each shadow run will read, before it starts.
    """
    sent = Requests.of(fetcher)
    outcome = _heal(
        spec, state, snapshots, fetcher, provider,
        run_id=run_id, mode=mode, attempts=attempts, notifier=notifier, on_shadow=on_shadow,
    )  # fmt: skip
    return outcome.model_copy(update={"requests": Requests.since(fetcher, sent)})


def _heal(
    spec: SourceSpec,
    state: State,
    snapshots: SnapshotStore,
    fetcher: Fetcher,
    provider: RepairProvider,
    *,
    run_id: str | None,
    mode: Literal["propose", "auto"] | None,
    attempts: int,
    notifier: Notifier | None,
    on_shadow: Callable[[ShadowPlan], None] | None,
) -> HealOutcome:
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
    known = state.anchors.records(spec.id)
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
            candidate, patch = _candidate(spec, state, provider, patch)
        except _Unusable as e:
            report = _unusable(spec, anchors, e.check)
        else:
            report = verify(
                candidate,
                anchors,
                pages,
                baseline=baseline.summary if baseline else None,
                known=known,
                on_shadow=on_shadow,
            )
        proposal = state.proposals.add(
            spec.id, spec.version, failed.run_id, provider.name, patch, report
        )
        proposals.append(proposal)
        if report.verdict != "failed":
            break  # passed, or a check couldn't run, which another attempt can't change
        context = context.with_attempt(patch, report.problems)

    accepted = next((p for p in proposals if p.status == "accepted"), None)
    unverified = next((p for p in proposals if p.status == "unverified"), None)
    if accepted is None:
        if unverified is not None:
            reason = (
                f"proposal {unverified.id} could not be verified: {_not_run(unverified.report)}; "
                "review it, then promote it with --force, or run heal again"
            )
        elif error:
            reason = f"repair provider {provider.name} failed: {error}"
        elif not proposals:
            reason = f"repair provider {provider.name} proposed nothing"
        else:
            reason = f"{len(proposals)} repair(s) failed verification: " + "; ".join(
                proposals[-1].report.problems[:3]
            )
        state.specs.quarantine(spec.id, reason)
        rejected = bool(proposals) and unverified is None
        alert("repair_rejected" if rejected else "quarantined", reason)
        return HealOutcome(
            source_id=spec.id,
            status="quarantined",
            message=reason,
            run_id=failed.run_id,
            proposals=proposals,
        )

    blocker = _auto_blocker(mode, failed, accepted)
    if blocker is None:
        # Verified again exactly as `scrapemend promote` does: on a new PageSource,
        # which sees every page stored with the failed run by now, and never forced.
        fresh = PageSource(
            snapshots, spec.id, failed.run_id, fetcher=fetcher, spec_version=spec.version
        )
        try:
            version = promote(state, accepted, fresh, on_shadow=on_shadow, provisional=True)
        except PromotionRefused as e:
            reason = (
                f"proposal {accepted.id} passed verification, but not again to be promoted: {e}"
            )
            state.specs.quarantine(spec.id, reason)
            alert("quarantined", reason)
            return HealOutcome(
                source_id=spec.id,
                status="quarantined",
                message=reason,
                run_id=failed.run_id,
                proposals=[state.proposals.get(p.id) or p for p in proposals],
            )
        message = (
            f"proposal {accepted.id} passed verification and is now v{version.version}, "
            "provisional: its runs aren't the last good run until you check its data and "
            f"confirm it: scrapemend confirm <spec> {version.version}"
        )
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


class PromotionRefused(ValueError):
    """A proposal can't be promoted: it was rejected, it was made for another
    version than the active one, or verifying it again failed or couldn't run."""

    def __init__(self, message: str, report: VerificationReport | None = None) -> None:
        super().__init__(message)
        self.report = report  # set when verifying it again failed


def promote(
    state: State,
    proposal: Proposal,
    pages: Pages,
    *,
    force: bool = False,
    on_shadow: Callable[[ShadowPlan], None] | None = None,
    provisional: bool = False,
) -> SpecVersion:
    """Verify a proposal again and store it as the new active spec version.

    It is verified again against the current anchors on `pages` (the failed run's,
    see PageSource), because the anchors or the gate may have changed since, or a
    page that didn't load may load now. Every check must pass. `force` promotes it
    when a check couldn't run, never when one failed, and applies a proposal made
    for an older version than the active one to the active version, verified like
    that. The patch is applied to a stored version, so a runtime override (like
    --start) never leaks into the stored spec. `provisional`: promoted automatically,
    so no human has looked at it (see scrapemend.runner.confirm); a promotion by hand
    is confirmed by that human.
    """
    source_id = proposal.source_id
    if proposal.status not in ("accepted", "unverified"):
        raise PromotionRefused(f"proposal {proposal.id} is {proposal.status}, not accepted")
    active = state.specs.active(source_id)
    if active is None:
        raise PromotionRefused(f"{source_id} has no spec versions")
    rebased = active.version != proposal.base_version
    if rebased and not force:
        raise PromotionRefused(
            f"proposal {proposal.id} was made for v{proposal.base_version}, but "
            f"v{active.version} is active: run heal again, or pass --force to apply it to "
            f"v{active.version} instead (it is verified again either way)"
        )
    version = state.specs.next_version(source_id)
    try:
        spec = proposal.patch.apply(active.spec, version)
    except ValueError as e:
        raise PromotionRefused(
            f"proposal {proposal.id} does not fit v{active.version}: {e}"
        ) from None
    baseline = state.runs.last_good(source_id)
    report = verify(
        spec,
        state.anchors.all(source_id),
        pages,
        baseline=baseline.summary if baseline else None,
        known=state.anchors.records(source_id),
        on_shadow=on_shadow,
    )
    state.proposals.reverified(proposal.id, report)
    if report.verdict == "failed":
        raise PromotionRefused(
            f"proposal {proposal.id} no longer passes verification: "
            + "; ".join(report.problems[:3]),
            report,
        )
    if report.verdict == "not run" and not force:
        raise PromotionRefused(
            f"proposal {proposal.id} could not be verified again: {_not_run(report)}; "
            "pass --force to promote it anyway (never past a check that failed)",
            report,
        )
    onto = f", re-based onto v{active.version}" if rebased else ""
    note = f"proposal {proposal.id} by {proposal.provider}{onto}: {proposal.patch.rationale}"
    if report.not_run:
        names = ", ".join(check.name for check in report.not_run)
        note = f"forced past checks that did not run: {names}; {note}"
    stored = state.specs.add(spec, "repair", note.strip()[:500], provisional=provisional)
    state.specs.activate(source_id, version)
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
    # Without item.follow there are no detail pages; an anchor's URL is a listing page.
    if example is not None and example.url and spec.item.follow is not None:
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
    on_run: Callable[[RunReport], None] | None = None,
    on_heal: Callable[[HealOutcome], None] | None = None,
    on_shadow: Callable[[ShadowPlan], None] | None = None,
) -> tuple[RunReport, HealOutcome | None, RunReport | None]:
    """A run that heals itself as far as heal.mode allows.

    If the run fails with a layout problem and heal.mode isn't "off", the ladder
    runs; if it promotes a repair, the source is crawled again with the new
    version. Returns the first run, the healing outcome and the second run.
    `rebase` re-applies runtime overrides (like --start) to a newly promoted spec.
    `on_run` and `on_heal` are told of each step as it ends, `on_shadow` of what a
    shadow run will read before it starts: to report as it goes.
    """
    first = run_source(
        spec, fetcher, snapshots, state,
        limit=limit, keep_invalid=keep_invalid, on_record=on_record, notifier=notifier,
    )  # fmt: skip
    if on_run is not None:
        on_run(first)
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
        on_shadow=on_shadow,
    )
    if on_heal is not None:
        on_heal(outcome)
    if outcome.status != "promoted":
        return first, outcome, None
    active = state.specs.active(spec.id)
    assert active is not None
    repaired = rebase(active.spec) if rebase else active.spec
    second = run_source(
        repaired, fetcher, snapshots, state,
        limit=limit, keep_invalid=keep_invalid, on_record=on_record, notifier=notifier,
    )  # fmt: skip
    if on_run is not None:
        on_run(second)
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


class _Unusable(Exception):
    def __init__(self, check: Check) -> None:
        self.check = check


def _candidate(
    spec: SourceSpec, state: State, provider: RepairProvider, patch: SpecPatch
) -> tuple[SourceSpec, SpecPatch]:
    """The candidate spec a patch makes, and the patch as stored. A provider's regexes
    run on RE2, which no text can make slow; a hand-written patch keeps Python's re."""
    if not getattr(provider, "hand_written", False):
        try:
            patch = patch.with_regex_engine("re2")
        except ValueError as e:
            raise _Unusable(
                Check(
                    name="regexes",
                    status="failed",
                    reason="a provider's regexes run on RE2",
                    problems=[str(e)],
                )
            ) from None
    try:
        return patch.apply(spec, version=state.specs.next_version(spec.id)), patch
    except ValueError as e:
        raise _Unusable(
            Check(
                name="patch",
                status="failed",
                reason="applied to the spec",
                problems=[f"the patch does not fit the spec: {e}"],
            )
        ) from None


def _unusable(spec: SourceSpec, anchors: list[Anchor], check: Check) -> VerificationReport:
    return VerificationReport(
        checks=[check],
        threshold=spec.heal.threshold,
        anchors=len(anchors),
        fields={name: FieldCheck() for name in spec.fields},
    )


def _not_run(report: VerificationReport) -> str:
    return "; ".join(f"{check.name}: {check.reason}" for check in report.not_run)
