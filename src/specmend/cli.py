"""Command-line interface."""

import difflib
import io
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import typer
from pydantic import ValidationError

from specmend import __version__, retention
from specmend.detect.classify import Status
from specmend.events import Notifier, Notifiers, make_notifier
from specmend.extract import Extractor
from specmend.extract.strategies import UnknownStrategy
from specmend.fetch import (
    DEFAULT_USER_AGENT,
    BlockedError,
    HttpFetcher,
    Requests,
    RobotsDisallowed,
)
from specmend.heal.context import spec_yaml
from specmend.heal.ladder import (
    HealOutcome,
    PromotionRefused,
    heal,
    promote,
    rollback,
    run_and_heal,
)
from specmend.heal.pages import PageSource
from specmend.heal.providers import PatchProvider, ProviderError, RepairProvider, make_provider
from specmend.heal.verify import ShadowPlan, VerificationReport
from specmend.pipeline import ValidatedRecord
from specmend.runner import ConfirmRefused, DriftNotAccepted, accept_drift, confirm, run_source
from specmend.runs import RunReport
from specmend.snapshots import FilesystemSnapshotStore
from specmend.spec import SourceSpec, SpecError, load_patch, load_spec
from specmend.spec.store import SpecConflict, SpecVersion
from specmend.state import State

app = typer.Typer(
    name="specmend",
    help="Self-healing scraper engine.",
    no_args_is_help=True,
    add_completion=False,
)

# Exit codes: 0 good, 1 usage or spec error, 2 the site refused or failed us,
# 3 the data is broken (layout changed, nothing found, fields failing, quarantined).
EXIT_CODES: dict[Status, int] = {
    "ok": 0,
    "drift": 0,
    "blocked": 2,
    "transient": 2,
    "layout_changed": 3,
    "empty": 3,
    "partial": 3,
}


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"specmend {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Self-healing scraper engine."""
    _utf8_output()


@app.command()
def version() -> None:
    """Show the version."""
    typer.echo(f"specmend {__version__}")


# --- shared options ----------------------------------------------------------------------

SpecArgument = Annotated[Path, typer.Argument(help="Path to a source spec YAML file.")]
DbOption = Annotated[
    Path, typer.Option("--db", envvar="SPECMEND_DB", help="SQLite file for specmend's state.")
]
SnapshotsOption = Annotated[
    Path, typer.Option(envvar="SPECMEND_SNAPSHOT_DIR", help="Where raw pages are stored.")
]
UserAgentOption = Annotated[
    str, typer.Option(envvar="SPECMEND_USER_AGENT", help="User-Agent header to send.")
]
StartOption = Annotated[
    str | None, typer.Option(help="Start URL to use instead of the spec's fetch.start.")
]
NotifyOption = Annotated[
    list[str] | None,
    typer.Option(
        "--notify",
        envvar="SPECMEND_NOTIFY",
        help="Send alerts to: stdout, log. Repeat for several.",
    ),
]
ProviderOption = Annotated[
    str,
    typer.Option(
        envvar="SPECMEND_REPAIR_PROVIDER",
        help="Repair provider: none, anthropic, or a plugin's name.",
    ),
]
ModelOption = Annotated[
    str | None,
    typer.Option(envvar="SPECMEND_ANTHROPIC_MODEL", help="Model for the anthropic provider."),
]
DEFAULT_DB = Path("specmend.db")
DEFAULT_SNAPSHOTS = Path("snapshots")


@app.command()
def validate(spec: SpecArgument) -> None:
    """Check a spec file and report every problem with its line number."""
    source = _buildable(_load(spec))
    fields = ", ".join(source.fields)
    typer.echo(f"{spec}: ok ({source.id} v{source.version}, {len(source.fields)} fields: {fields})")


@app.command()
def run(
    spec: SpecArgument,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write records to this JSONL file instead of stdout."),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Stop after this many items.")] = None,
    keep_invalid: Annotated[
        bool, typer.Option(help="Also write records that failed validation.")
    ] = False,
    force: Annotated[bool, typer.Option(help="Run even if the source is quarantined.")] = False,
    patch: Annotated[
        Path | None,
        typer.Option(help="If the run breaks, try this hand-written repair (YAML)."),
    ] = None,
    provider: ProviderOption = "none",
    model: ModelOption = None,
    start: StartOption = None,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
    notify: NotifyOption = None,
) -> None:
    """Scrape a source with its active spec version, write one JSON record per line,
    judge the run, and heal as far as the spec's heal.mode allows.

    A report goes to stderr. Exit codes: 0 good, 2 blocked or unreachable (healing
    included), 3 the data is broken (layout changed, nothing found, failing fields,
    quarantined).
    """
    state = State(db)
    active = _active(state, spec)
    reason = state.specs.state(active.source_id)
    if reason is not None and reason.quarantine_reason and not force:
        typer.echo(f"{active.source_id} is quarantined: {reason.quarantine_reason}", err=True)
        typer.echo("repair it (specmend heal / promote / rollback) or pass --force", err=True)
        raise typer.Exit(3)
    source = _with_start(active.spec, start)
    repairer = _provider(provider, patch, model)
    output = out.open("w", encoding="utf-8", newline="\n") if out else sys.stdout

    def write(record: ValidatedRecord) -> None:
        output.write(json.dumps(record.record, default=_json_value, ensure_ascii=False) + "\n")
        output.flush()

    store = FilesystemSnapshotStore(snapshots)
    try:
        with _fetcher(source, user_agent) as fetcher, _interruptible():
            first, healing, second = run_and_heal(
                source,
                state,
                store,
                fetcher,
                repairer,
                on_record=write,
                keep_invalid=keep_invalid,
                limit=limit,
                notifier=_notifier(notify),
                rebase=lambda s: _with_start(s, start),
                on_run=lambda report: _print_report(report, snapshots),
                on_heal=_print_heal,
                on_shadow=_print_plan("heal"),
            )
    finally:
        if out:
            output.close()
    if healing is not None and healing.status == "blocked":
        raise typer.Exit(2)  # the site refused us: more urgent than the broken data
    final = second or first
    raise typer.Exit(EXIT_CODES[final.status])


@app.command()
def check(
    spec: SpecArgument,
    sample: Annotated[int, typer.Option(min=1, help="Items to sample.")] = 5,
    start: StartOption = None,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
    notify: NotifyOption = None,
) -> None:
    """Scrape a small sample with the active spec version and report whether it still
    works. Writes no records, never changes the baseline, never heals."""
    state = State(db)
    source = _with_start(_active(state, spec).spec, start)
    store = FilesystemSnapshotStore(snapshots)
    with _fetcher(source, user_agent) as fetcher, _interruptible():
        report = run_source(
            source, fetcher, store, state, mode="check", limit=sample, notifier=_notifier(notify)
        )
        _print_report(report, snapshots)
    raise typer.Exit(EXIT_CODES[report.status])


@app.command(name="heal")
def heal_command(
    spec: SpecArgument,
    patch: Annotated[
        Path | None,
        typer.Option(help="Verify this hand-written repair (YAML) instead of asking a provider."),
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run", help="The failed run to repair (default: the latest).")
    ] = None,
    mode: Annotated[
        Literal["propose", "auto"] | None,
        typer.Option(help="propose: wait for `promote`; auto: promote if verified."),
    ] = None,
    attempts: Annotated[int, typer.Option(min=1, max=5, help="Repair attempts.")] = 2,
    provider: ProviderOption = "none",
    model: ModelOption = None,
    start: StartOption = None,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
    notify: NotifyOption = None,
) -> None:
    """Repair the latest failed run: get a patch, verify it on the current pages, and
    promote it or leave it for `specmend promote` (exit 3 if nothing passed, 2 if
    the site refused us)."""
    state = State(db)
    source = _with_start(_active(state, spec).spec, start)
    repairer = _provider(provider, patch, model)
    store = FilesystemSnapshotStore(snapshots)
    with _fetcher(source, user_agent) as fetcher, _interruptible():
        outcome = heal(
            source,
            state,
            store,
            fetcher,
            repairer,
            run_id=run_id,
            mode=mode,
            attempts=attempts,
            notifier=_notifier(notify),
            on_shadow=_print_plan("heal"),
        )
    _print_heal(outcome)
    if outcome.status == "blocked":
        raise typer.Exit(2)
    if outcome.status == "quarantined":
        raise typer.Exit(3)
    if outcome.status == "skipped" and outcome.run_id is not None:
        latest = state.runs.get(outcome.run_id)
        raise typer.Exit(0 if latest is not None and latest.good else 1)


@app.command()
def proposals(spec: SpecArgument, db: DbOption = DEFAULT_DB) -> None:
    """List repair proposals for a source, newest first."""
    state = State(db)
    found = state.proposals.for_source(_load(spec).id)
    if not found:
        typer.echo("no proposals yet")
        return
    typer.echo(
        "{:>4}  {:<10} {:>4} {:>5} {:>5}  {:<12} {}".format(
            "ID", "STATUS", "BASE", "SCORE", "CONF", "PROVIDER", "CHANGES"
        )
    )
    for p in found:
        changes = ", ".join(p.patch.changes())
        typer.echo(
            f"{p.id:>4}  {p.status:<10} v{p.base_version:<3} {p.report.score:>5.2f} "
            f"{p.patch.confidence:>5.2f}  {p.provider:<12} {changes}"
        )


@app.command()
def diff(
    spec: SpecArgument,
    proposal: Annotated[int | None, typer.Argument(help="A proposal id.")] = None,
    from_version: Annotated[int | None, typer.Option("--from", help="A spec version.")] = None,
    to_version: Annotated[int | None, typer.Option("--to", help="A spec version.")] = None,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Show what a proposal would change, or the difference between two versions."""
    state = State(db)
    source_id = _load(spec).id
    if proposal is not None:
        found = state.proposals.get(proposal)
        if found is None or found.source_id != source_id:
            _fail(f"{source_id} has no proposal {proposal}")
        base = state.specs.get(source_id, found.base_version)
        if base is None:
            _fail(f"{source_id} v{found.base_version} is not stored")
        new = found.patch.apply(base.spec, state.specs.next_version(source_id))
        old_label, new_label = f"v{base.version}", f"proposal {proposal}"
        old, new_spec = base.spec, new
        if found.patch.rationale:
            typer.echo(f"# {found.patch.rationale.strip()}\n")
    else:
        active = state.specs.active(source_id)
        start = state.specs.get(source_id, from_version) if from_version else None
        end = state.specs.get(source_id, to_version) if to_version else active
        if start is None or end is None:
            _fail("give a proposal id, or --from (and optionally --to) stored versions")
        old_label, new_label = f"v{start.version}", f"v{end.version}"
        old, new_spec = start.spec, end.spec
    lines = difflib.unified_diff(
        spec_yaml(old).splitlines(keepends=True),
        spec_yaml(new_spec).splitlines(keepends=True),
        fromfile=old_label,
        tofile=new_label,
    )
    typer.echo("".join(lines).rstrip() or "no differences")


@app.command(name="promote")
def promote_command(
    spec: SpecArgument,
    proposal: Annotated[int, typer.Argument(help="The id of a verified proposal.")],
    force: Annotated[
        bool,
        typer.Option(
            help="Promote even if a check could not run (never if one failed), and apply a "
            "proposal made for an older version to the active one. Verified again either way."
        ),
    ] = False,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Verify a proposal again and make it the active spec version (exit 3 if a check
    fails, or can't run without --force; 2 if the site refused us). Pages are read
    from the failed run's snapshots where possible; a page that didn't load before is
    fetched again."""
    state = State(db)
    source_id = _load(spec).id
    found = state.proposals.get(proposal)
    if found is None or found.source_id != source_id:
        _fail(f"{source_id} has no proposal {proposal}")
    if found.status not in ("accepted", "unverified"):
        _fail(f"proposal {proposal} is {found.status}: only verified proposals can be promoted")
    active = state.specs.active(source_id)
    if active is None:
        _fail(f"{source_id} has no spec versions")
    store = FilesystemSnapshotStore(snapshots)
    with _fetcher(_buildable(active.spec), user_agent) as fetcher, _interruptible():
        pages = PageSource(
            store, source_id, found.run_id, fetcher=fetcher, spec_version=found.base_version
        )
        try:
            version = promote(
                state,
                found,
                pages,
                force=force,
                on_shadow=_print_plan("promote"),
                on_verified=_print_verified,  # before it is promoted, with --force too
            )
        except PromotionRefused as e:
            _fail(str(e), 1 if e.report is None else 3)
        except (BlockedError, RobotsDisallowed) as e:
            _fail(
                f"promote: blocked: the site refused us ({e}); nothing was promoted. "
                "specmend never works around a block: find out why before you try again",
                2,
            )
    verified = state.proposals.get(proposal)
    assert verified is not None
    onto = f" (re-based onto v{active.version})" if found.base_version != active.version else ""
    skipped = ", ".join(f"{c.name} ({c.reason})" for c in verified.report.not_run)
    how = (
        f"promoted with --force although these checks did not run: {skipped}"
        if skipped
        else "every check passed; promoted"
    )
    typer.echo(
        f"{source_id}: proposal {proposal}{onto} verified again; {how}; "
        f"v{version.version} is now active"
    )


@app.command(name="rollback")
def rollback_command(
    spec: SpecArgument,
    to: Annotated[
        int | None, typer.Option(help="The version to go back to (default: the previous).")
    ] = None,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Make an older spec version active again."""
    state = State(db)
    source_id = _load(spec).id
    try:
        version = rollback(state, source_id, to)
    except (ValueError, KeyError) as e:
        _fail(str(e).strip("'\""))
    typer.echo(f"{source_id}: v{version.version} ({version.origin}) is active again")


@app.command()
def versions(spec: SpecArgument, db: DbOption = DEFAULT_DB) -> None:
    """List the stored spec versions of a source."""
    state = State(db)
    source_id = _load(spec).id
    history = state.specs.history(source_id)
    if not history:
        typer.echo("no versions yet: run the spec once")
        return
    current = state.specs.state(source_id)
    for v in history:
        mark = "*" if current and v.version == current.active_version else " "
        note = f"provisional (confirm it: specmend confirm); {v.note}" if v.provisional else v.note
        typer.echo(f"{mark} v{v.version:<3} {v.origin:<6} {v.created_at:%Y-%m-%d %H:%M}  {note}")
    if current and current.quarantine_reason:
        typer.echo(f"quarantined: {current.quarantine_reason}")


@app.command()
def show(
    spec: SpecArgument,
    version: Annotated[int | None, typer.Option(help="A stored version (default: active).")] = None,
    db: DbOption = DEFAULT_DB,
) -> None:
    """Print a spec version as YAML (the active one by default)."""
    state = State(db)
    source_id = _load(spec).id
    found = state.specs.get(source_id, version) if version else state.specs.active(source_id)
    if found is None:
        _fail(f"{source_id} has no version {version}" if version else "no versions yet")
    typer.echo(f"# {source_id} v{found.version} ({found.origin}): {found.note}")
    typer.echo(spec_yaml(found.spec).rstrip())


@app.command(name="prune")
def prune_command(
    spec: SpecArgument,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only say which runs' snapshots would go.")
    ] = False,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
) -> None:
    """Delete the snapshots of a source's older runs: all but the newest
    snapshots.keep_runs, never those of the last good run, a proposal that can still
    be promoted, or the last failed run. Every run does this when it ends, too."""
    state = State(db)
    source = _active(state, spec).spec
    pruned = retention.prune(source, state, FilesystemSnapshotStore(snapshots), dry_run=dry_run)
    keep = source.snapshots.keep_runs
    newest = "the newest run" if keep == 1 else f"the newest {keep} runs"
    typer.echo(
        f"{source.id}: keeps the snapshots of {newest} (snapshots.keep_runs: {keep}) "
        "and of older runs still needed"
    )
    label = "would delete" if dry_run else "deleted"
    for run_id in pruned.deleted:
        typer.echo(f"  {label:<12}  {run_id}")
    if not pruned.deleted:
        typer.echo("  nothing to delete")
    for run_id, why in pruned.needed.items():
        typer.echo(f"  {'kept':<12}  {run_id}: {why}")


@app.command(name="accept-drift")
def accept_drift_command(
    run_id: Annotated[str, typer.Argument(help="A drift run whose data you checked.")],
    db: DbOption = DEFAULT_DB,
) -> None:
    """Make a drift run the last good run, once you have checked that its data is
    right: later runs and repairs are compared with it. Until then every run is
    compared with the last good run before it, and keeps drifting and alerting."""
    state = State(db)
    try:
        report = accept_drift(state, run_id)
    except DriftNotAccepted as e:
        _fail(str(e))
    typer.echo(
        f"{report.source_id}: drift run {run_id} accepted: later runs and repairs are "
        f"compared with its {report.summary.valid_records} records"
    )


@app.command(name="confirm")
def confirm_command(
    spec: SpecArgument,
    version: Annotated[int, typer.Argument(help="A version heal promoted automatically.")],
    db: DbOption = DEFAULT_DB,
) -> None:
    """Confirm a spec version that heal promoted automatically, once you have checked
    its data: its latest good run becomes the last good run, and later runs and
    repairs are compared with it. Until then they are compared with the last good run
    before the promotion."""
    state = State(db)
    source_id = _load(spec).id
    try:
        report = confirm(state, source_id, version)
    except ConfirmRefused as e:
        _fail(str(e))
    if report is None:
        typer.echo(
            f"{source_id}: v{version} confirmed: its next good run becomes the last good run"
        )
        return
    typer.echo(
        f"{source_id}: v{version} confirmed: its run {report.run_id} is the last good run now, "
        f"with its {report.summary.valid_records} records"
    )


@app.command()
def runs(
    source_id: Annotated[str | None, typer.Argument(help="Only runs of this source.")] = None,
    limit: Annotated[int, typer.Option(min=1, help="How many runs to show.")] = 20,
    db: DbOption = DEFAULT_DB,
) -> None:
    """List recent runs, newest first."""
    reports = State(db).runs.recent(source_id, limit=limit)
    if not reports:
        typer.echo("no runs yet")
        return
    header = ("RUN", "SOURCE", "VER", "MODE", "STATUS", "RECORDS")
    typer.echo("{:<23} {:<16} {:>3} {:<5} {:<15} {:>7}  STARTED".format(*header))
    for r in reports:
        started = r.started_at.strftime("%Y-%m-%d %H:%M:%S")
        typer.echo(
            f"{r.run_id:<23} {r.source_id:<16} {r.spec_version:>3} {r.mode:<5} "
            f"{r.status:<15} {r.summary.valid_records:>7}  {started}"
        )


# --- helpers -------------------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _load(spec: Path) -> SourceSpec:
    try:
        return load_spec(spec)
    except SpecError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _buildable(source: SourceSpec) -> SourceSpec:
    """Fail early, before any request, if a custom strategy's plugin is missing."""
    try:
        Extractor(source.fields)
    except UnknownStrategy as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    return source


def _active(state: State, spec: Path) -> SpecVersion:
    """Register the spec file's version and return the version that should run."""
    try:
        active = state.specs.register(_buildable(_load(spec)))
        _buildable(active.spec)
        return active
    except SpecConflict as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _with_start(source: SourceSpec, start: str | None) -> SourceSpec:
    if not start:
        return source
    data = source.model_dump(mode="json")
    data["fetch"]["start"] = start
    try:
        return SourceSpec.model_validate(data)
    except ValidationError as e:
        typer.echo(f"--start: {e.errors()[0]['msg']}", err=True)
        raise typer.Exit(1) from None


def _provider(name: str, patch: Path | None, model: str | None) -> RepairProvider:
    if patch is not None:
        try:
            return PatchProvider(load_patch(patch), name=f"patch:{patch.name}")
        except SpecError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    try:
        return make_provider(name, model=model)
    except (ValueError, ProviderError) as e:
        typer.echo(f"--provider: {e}", err=True)
        raise typer.Exit(1) from None


def _fetcher(source: SourceSpec, user_agent: str) -> HttpFetcher:
    return HttpFetcher.for_spec(source.fetch, user_agent=user_agent)


@contextmanager
def _interruptible() -> Iterator[None]:
    try:
        yield
    except KeyboardInterrupt:
        typer.echo("interrupted", err=True)
        raise typer.Exit(130) from None


def _notifier(names: list[str] | None) -> Notifier:
    notifiers: list[Notifier] = []
    for name in names or []:
        if name == "none":
            continue
        try:
            notifiers.append(make_notifier(name))
        except ValueError as e:
            typer.echo(f"--notify: {e}", err=True)
            raise typer.Exit(1) from None
    return Notifiers(notifiers)


def _utf8_output() -> None:
    """Write UTF-8 everywhere. Records, reports and diffs carry page text (€, ·)
    that a legacy code page, which Windows uses for pipes and files, can't always
    encode; and whoever reads the output can rely on one encoding."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _json_value(value: object) -> str:
    """Decimals stay exact as strings ("566600.00"); dates are ISO 8601."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_plan(step: str) -> Callable[[ShadowPlan], None]:
    """Says what a shadow run will read before it starts, so a long one can be stopped."""

    def say(plan: ShadowPlan) -> None:
        typer.echo(f"{step}: shadow run: {_plan(plan)}", err=True)

    return say


def _plan(plan: ShadowPlan) -> str:
    if plan.scope == "full":
        what = f"the whole source, all {plan.known} of the last good run's records"
    else:
        what = (
            f"every listing page, and the detail pages of a sample of {plan.records} records "
            f"(the last good run had {plan.known})"
        )
    if not plan.requests:
        return f"{what}: every page it reads is stored, so no requests"
    pages = f"{plan.listing_pages} listing pages"
    if plan.detail_pages:
        pages += f" and {plan.detail_pages} detail pages"
    rate = f"{plan.rps:g} request{'' if plan.rps == 1 else 's'} a second"
    return (
        f"{what}: about {plan.requests} requests ({pages}, {plan.stored} of them stored), "
        f"at least {_duration(plan.requests / plan.rps)} at {rate}"
    )


def _duration(seconds: float) -> str:
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def _print_heal(outcome: HealOutcome) -> None:
    def err(line: str) -> None:
        typer.echo(line, err=True)

    err(f"heal: {outcome.status}: {outcome.message}")
    if outcome.requests is not None:
        err(f"  {_requests(outcome.requests)}")
    for p in outcome.proposals:
        confidence = f"confidence {p.patch.confidence:.2f}"
        err(f"  proposal {p.id} ({p.provider}, {confidence}): {p.report.verdict}")
        if p.patch.changes():
            err(f"    changes: {', '.join(p.patch.changes())}")
        _print_checks(p.report, "    ")


def _print_verified(report: VerificationReport) -> None:
    typer.echo(f"promote: verified again: {report.verdict}", err=True)
    _print_checks(report, "  ")


def _print_checks(report: VerificationReport, indent: str) -> None:
    """Every check (passed, failed or not run, and why), then what was compared, field
    by field, and what the rules tolerated."""
    for check in report.checks:
        typer.echo(f"{indent}{check.status:<8} {check.name}: {check.reason}", err=True)
        for problem in check.problems[:5]:
            typer.echo(f"{indent}  problem: {problem}", err=True)
    for line in report.evidence():
        typer.echo(f"{indent}{line}", err=True)


def _requests(sent: Requests) -> str:
    return f"requests: {sent.sent} ({sent.retried} retried)"


def _print_report(report: RunReport, snapshots: Path) -> None:
    def err(line: str) -> None:
        typer.echo(line, err=True)

    summary = report.summary

    def per_field(counts: dict[str, int]) -> str:
        return ", ".join(f"{name} {n}/{summary.records}" for name, n in counts.items())

    kind = "check" if report.mode == "check" else "run"
    err(
        f"{summary.source_id} v{summary.spec_version}, {kind} {summary.run_id}: "
        f"{summary.listing_pages} listing pages, {summary.items} items, "
        f"{summary.valid_records} records, {summary.invalid_records} invalid, "
        f"{len(summary.failures)} failed"
    )
    if report.requests is not None:  # this run's own: not the heal's, nor another run's
        err(f"  {_requests(report.requests)}")
    if summary.fallbacks:
        err(f"  fallback strategies used: {per_field(summary.fallbacks)}")
    if summary.empty:
        err(f"  empty fields: {per_field(summary.empty)}")
    if summary.errors:
        err(f"  unusable text: {per_field(summary.errors)}")
    for failure in summary.failures[:5]:
        err(f"  failed: {failure.url or '(no detail link)'}: {failure.reason}")
    if len(summary.failures) > 5:
        err(f"  ... and {len(summary.failures) - 5} more failures")
    err(f"  snapshots: {snapshots / summary.source_id / summary.run_id}")
    if report.pruned:
        older = f"{len(report.pruned)} older run{'s' if len(report.pruned) > 1 else ''}"
        err(f"  pruned: the snapshots of {older}")
    err(f"status: {report.status}")
    for f in report.failures:
        err(f"  {f.category}: {f.message}")
        if f.fields:
            err(f"    failing: {', '.join(f.fields)}")
    if report.status == "drift":
        err(
            "  the last good run stays the baseline: runs keep drifting until you check this "
            f"data and accept it with: specmend accept-drift {report.run_id}"
        )
    for warning in report.warnings:
        err(f"  warning: {warning}")
