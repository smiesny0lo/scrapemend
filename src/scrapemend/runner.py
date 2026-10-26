"""One judged run of a source: crawl, validate, compare with the last good run,
classify, record, notify."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from scrapemend.detect.classify import Diagnosis, Evidence, classify, diagnose, worst
from scrapemend.detect.fingerprint import similarity
from scrapemend.events import Event, EventKind, Notifier
from scrapemend.extract import FieldResult
from scrapemend.extract.select import find_link
from scrapemend.fetch import Fetcher, FetchError
from scrapemend.heal.anchors import Anchor, AnchorPicker, encode
from scrapemend.pipeline import RunAborted, RunSummary, SourceRun, ValidatedRecord
from scrapemend.runs import RunMode, RunReport, RunStore
from scrapemend.snapshots import SnapshotStore
from scrapemend.spec import SourceSpec
from scrapemend.state import State
from scrapemend.validate import validate_batch

PROBE_PAGES = 3  # known detail pages re-checked when the listing yields nothing

FieldValues = dict[str, tuple[object, int | None]]  # field -> (value, winning strategy)


def run_source(
    spec: SourceSpec,
    fetcher: Fetcher,
    snapshots: SnapshotStore,
    state: State,
    *,
    mode: RunMode = "run",
    limit: int | None = None,
    keep_invalid: bool = False,
    on_record: Callable[[ValidatedRecord], None] | None = None,
    notifier: Notifier | None = None,
) -> RunReport:
    """Crawl a source and judge the result.

    Valid records are passed to `on_record` as they arrive (invalid ones too with
    `keep_invalid`). Only a complete ok run of mode "run" becomes the new baseline
    and refreshes the source's anchors and known records. A drift run doesn't: the
    runs after it are compared with the same last good run, and keep drifting, until
    a human accepts its drift (accept_drift), for which its anchors are held.
    """
    runs = state.runs
    started = datetime.now(UTC)
    previous = runs.latest(spec.id)
    baseline_report = runs.last_good(spec.id)
    baseline = baseline_report.summary if baseline_report else None

    crawl = SourceRun(spec, fetcher, snapshots, limit=limit)
    picker = AnchorPicker(spec.heal.anchors, spec.item.key, crawl.summary.run_id)
    error: BaseException | None = None
    try:
        for record in crawl.records():
            picker.add(record)
            if on_record is not None and (record.valid or keep_invalid):
                on_record(record)
    except (FetchError, RunAborted) as e:
        error = e
    summary = crawl.summary
    complete = error is None and limit is None and mode == "run"

    diagnosis = None
    next_link_missing = False
    listing_text = None
    if error is None and crawl.first_listing is not None:
        listing_text = crawl.first_listing.text
        paginate = spec.fetch.paginate
        stopped_early = summary.listing_pages == 1 and (limit is None or summary.items < limit)
        if paginate and baseline and baseline.listing_pages > 1 and stopped_early:
            next_link_missing = find_link(paginate.next, crawl.first_listing) is None
        # Detail pages known from a good run show which fields broke. Without detail
        # pages there is nothing to probe: the item container is what broke.
        if summary.items == 0 and spec.item.follow is not None:
            known = state.anchors.all(spec.id)
            if known:
                diagnosis = _diagnose_anchors(crawl, known)
            elif baseline_report is not None:
                diagnosis = _diagnose(crawl, snapshots, baseline_report)

    # After the probes, so probed detail pages count as structural evidence too.
    structure = _structure(runs, spec.id, summary)
    violations = validate_batch(
        summary.valid_records, summary.fields, spec.expect, complete=complete
    )
    failures, warnings = classify(
        Evidence(
            spec=spec,
            summary=summary,
            complete=complete,
            error=error,
            structure=structure,
            baseline=baseline,
            violations=violations,
            diagnosis=diagnosis,
            listing_text=listing_text,
            next_link_missing=next_link_missing,
        )
    )
    report = RunReport(
        run_id=summary.run_id,
        source_id=spec.id,
        spec_version=spec.version,
        mode=mode,
        status=worst(failures),
        started_at=started,
        finished_at=datetime.now(UTC),
        complete=complete,
        summary=summary,
        failures=failures,
        warnings=warnings,
        structure=structure,
        diagnosis=diagnosis,
        error=str(error) if error else None,
    )
    runs.save(report)
    if complete and report.status == "ok":
        _set_fingerprints(runs, summary)
        state.anchors.replace(spec.id, picker.anchors(), picker.records())
    elif complete and report.status == "drift":
        state.anchors.hold(spec.id, summary.run_id, picker.anchors(), picker.records())
    if notifier is not None:
        _notify(notifier, report, previous)
    return report


class DriftNotAccepted(ValueError):
    """A run can't be accepted: it isn't the latest drift run since the last good run."""


def accept_drift(state: State, run_id: str) -> RunReport:
    """Make a drift run the baseline, because a human checked that its data is right:
    the runs after it, and repairs, are compared with it. Its anchors and records
    must still be held, which they are for the latest drift run since the last good
    run. The run keeps its status; it is marked accepted."""
    report = state.runs.get(run_id)
    if report is None:
        raise DriftNotAccepted(f"there is no run {run_id}")
    if report.status != "drift":
        raise DriftNotAccepted(
            f"run {run_id} is {report.status}, not drift: only a drift run can be accepted"
        )
    if report.accepted_at is not None:
        raise DriftNotAccepted(f"run {run_id} was accepted already")
    good = state.runs.last_good(report.source_id)
    if good is not None and good.started_at > report.started_at:
        raise DriftNotAccepted(f"run {run_id} is older than the last good run, {good.run_id}")
    held = state.anchors.held(report.source_id)
    if held != run_id:
        if held is None:
            raise DriftNotAccepted(f"run {run_id}'s records were not kept, so it can't be accepted")
        raise DriftNotAccepted(
            f"only the latest drift run's records are kept: review run {held} and accept it"
        )
    state.anchors.accept(report.source_id, run_id)
    _set_fingerprints(state.runs, report.summary)
    return state.runs.accept(run_id, datetime.now(UTC))


def _set_fingerprints(runs: RunStore, summary: RunSummary) -> None:
    """Make a run's page fingerprints the ones later runs' pages are compared with."""
    for kind, prints in summary.fingerprints.items():
        if prints:
            runs.set_baseline(summary.source_id, kind, prints[0], summary.run_id)


def _structure(runs: RunStore, source_id: str, summary: RunSummary) -> dict[str, float]:
    """Per page kind, the lowest similarity of this run's pages to the baseline."""
    structure = {}
    for kind, prints in summary.fingerprints.items():
        baseline = runs.baseline(source_id, kind)
        if baseline and prints:
            structure[kind] = min(similarity(fp, baseline) for fp in prints)
    return structure


def _diagnose(crawl: SourceRun, snapshots: SnapshotStore, baseline: RunReport) -> Diagnosis | None:
    """Re-check detail pages from the last good run: what did the spec read there
    then (from the stored snapshot), and what does it read now (fetched fresh)?"""
    before: dict[str, FieldValues] = {}
    for snapshot_id in snapshots.snapshots(baseline.source_id, baseline.run_id):
        snapshot = snapshots.get(snapshot_id)
        if snapshot.kind != "detail" or snapshot.status != 200:
            continue
        before[snapshot.url] = _values(crawl.extractor.extract(snapshot.document()).fields)
        if len(before) == PROBE_PAGES:
            break
    if not before:
        return None
    now = {url: _values(record.fields) for url, record in crawl.probe(before)}
    return diagnose(crawl.spec, before, now)


def _diagnose_anchors(crawl: SourceRun, anchors: list[Anchor]) -> Diagnosis | None:
    """Re-check anchor pages, whose good values are known."""
    known = [a for a in anchors if a.url][:PROBE_PAGES]
    before: dict[str, FieldValues] = {
        str(a.url): {name: (a.values[name], a.strategies.get(name)) for name in a.values}
        for a in known
    }
    if not before:
        return None
    now = {url: _values(record.fields) for url, record in crawl.probe(before)}
    return diagnose(crawl.spec, before, now)


def _values(fields: Mapping[str, FieldResult]) -> FieldValues:
    # Encoded like anchor values, so values from either source compare equal.
    return {name: (encode(f.value), f.strategy) for name, f in fields.items()}


def _notify(notifier: Notifier, report: RunReport, previous: RunReport | None) -> None:
    def send(kind: EventKind, message: str) -> None:
        notifier.notify(
            Event(
                kind=kind,
                source_id=report.source_id,
                message=message,
                run_id=report.run_id,
                status=report.status,
            )
        )

    if report.status == "drift":
        for failure in report.failures:
            send("drift", failure.message)
    elif not report.good:
        first = report.failures[0]
        fields = f" (failing: {', '.join(first.fields)})" if first.fields else ""
        send("run_failed", f"{first.category}: {first.message}{fields}")
    elif previous is not None and not previous.good:
        send("run_recovered", f"good again after {previous.status} in run {previous.run_id}")
    for warning in report.warnings:
        send("warning", warning)
