"""Command-line interface."""

import io
import json
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, TextIO

import typer
from pydantic import ValidationError

from scrapemend import __version__
from scrapemend.detect.classify import Status
from scrapemend.events import LogNotifier, Notifier, Notifiers, StreamNotifier
from scrapemend.fetch import DEFAULT_USER_AGENT, HttpFetcher
from scrapemend.pipeline import ValidatedRecord
from scrapemend.runner import run_source
from scrapemend.runs import RunMode, RunReport, RunStore
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, SpecError, load_spec
from scrapemend.state import connect

app = typer.Typer(
    name="scrapemend",
    help="Self-healing scraper engine.",
    no_args_is_help=True,
    add_completion=False,
)

# Exit codes: 0 good, 1 usage or spec error, 2 the site refused or failed us,
# 3 the data is broken (layout changed, nothing found, fields failing).
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
        typer.echo(f"scrapemend {__version__}")
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


@app.command()
def version() -> None:
    """Show the version."""
    typer.echo(f"scrapemend {__version__}")


# --- shared options ------------------------------------------------------------------

SpecArgument = Annotated[Path, typer.Argument(help="Path to a source spec YAML file.")]
DbOption = Annotated[
    Path, typer.Option("--db", envvar="SCRAPEMEND_DB", help="SQLite file for run history.")
]
SnapshotsOption = Annotated[
    Path, typer.Option(envvar="SCRAPEMEND_SNAPSHOT_DIR", help="Where raw pages are stored.")
]
UserAgentOption = Annotated[
    str, typer.Option(envvar="SCRAPEMEND_USER_AGENT", help="User-Agent header to send.")
]
StartOption = Annotated[
    str | None, typer.Option(help="Start URL to use instead of the spec's fetch.start.")
]
NotifyOption = Annotated[
    list[str] | None,
    typer.Option(
        "--notify",
        envvar="SCRAPEMEND_NOTIFY",
        help="Send alerts to: stdout, log. Repeat for several.",
    ),
]
DEFAULT_DB = Path("scrapemend.db")
DEFAULT_SNAPSHOTS = Path("snapshots")


@app.command()
def validate(spec: SpecArgument) -> None:
    """Check a spec file and report every problem with its line number."""
    source = _load(spec)
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
    start: StartOption = None,
    db: DbOption = DEFAULT_DB,
    snapshots: SnapshotsOption = DEFAULT_SNAPSHOTS,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
    notify: NotifyOption = None,
) -> None:
    """Scrape a source, write one JSON record per line, and judge the run.

    A report goes to stderr. Exit codes: 0 good, 2 blocked or unreachable,
    3 the data is broken (layout changed, nothing found, failing fields).
    """
    source = _source(spec, start)
    output = out.open("w", encoding="utf-8", newline="\n") if out else _utf8_stdout()

    def write(record: ValidatedRecord) -> None:
        output.write(json.dumps(record.record, default=_json_value, ensure_ascii=False) + "\n")
        output.flush()

    try:
        report = _execute(
            source,
            "run",
            db=db,
            snapshots=snapshots,
            user_agent=user_agent,
            notify=notify,
            limit=limit,
            keep_invalid=keep_invalid,
            on_record=write,
        )
    finally:
        if out:
            output.close()
    raise typer.Exit(EXIT_CODES[report.status])


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
    """Scrape a small sample and report whether the spec still works. Writes no
    records and never changes the baseline that later runs are compared to."""
    source = _source(spec, start)
    report = _execute(
        source,
        "check",
        db=db,
        snapshots=snapshots,
        user_agent=user_agent,
        notify=notify,
        limit=sample,
    )
    raise typer.Exit(EXIT_CODES[report.status])


@app.command()
def runs(
    source_id: Annotated[str | None, typer.Argument(help="Only runs of this source.")] = None,
    limit: Annotated[int, typer.Option(min=1, help="How many runs to show.")] = 20,
    db: DbOption = DEFAULT_DB,
) -> None:
    """List recent runs, newest first."""
    reports = RunStore(connect(db)).recent(source_id, limit=limit)
    if not reports:
        typer.echo("no runs yet")
        return
    typer.echo(
        f"{'RUN':<23} {'SOURCE':<16} {'VER':>3} {'MODE':<5} {'STATUS':<15} {'RECORDS':>7}  STARTED"
    )
    for r in reports:
        started = r.started_at.strftime("%Y-%m-%d %H:%M:%S")
        typer.echo(
            f"{r.run_id:<23} {r.source_id:<16} {r.spec_version:>3} {r.mode:<5} "
            f"{r.status:<15} {r.summary.valid_records:>7}  {started}"
        )


# --- helpers ---------------------------------------------------------------------------


def _execute(
    source: SourceSpec,
    mode: RunMode,
    *,
    db: Path,
    snapshots: Path,
    user_agent: str,
    notify: list[str] | None,
    limit: int | None = None,
    keep_invalid: bool = False,
    on_record: Callable[[ValidatedRecord], None] | None = None,
) -> RunReport:
    store = FilesystemSnapshotStore(snapshots)
    with HttpFetcher.for_spec(source.fetch, user_agent=user_agent) as fetcher:
        try:
            report = run_source(
                source,
                fetcher,
                store,
                RunStore(connect(db)),
                mode=mode,
                limit=limit,
                keep_invalid=keep_invalid,
                on_record=on_record,
                notifier=_notifier(notify),
            )
        except KeyboardInterrupt:
            typer.echo("interrupted", err=True)
            raise typer.Exit(130) from None
        _print_report(report, fetcher, snapshots)
    return report


def _load(spec: Path) -> SourceSpec:
    try:
        return load_spec(spec)
    except SpecError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _source(spec: Path, start: str | None) -> SourceSpec:
    source = _load(spec)
    if not start:
        return source
    data = source.model_dump(mode="json")
    data["fetch"]["start"] = start
    try:
        return SourceSpec.model_validate(data)
    except ValidationError as e:
        typer.echo(f"--start: {e.errors()[0]['msg']}", err=True)
        raise typer.Exit(1) from None


def _notifier(names: list[str] | None) -> Notifier:
    notifiers: list[Notifier] = []
    for name in names or []:
        if name == "stdout":
            notifiers.append(StreamNotifier())
        elif name == "log":
            notifiers.append(LogNotifier())
        elif name != "none":
            typer.echo(f"--notify: unknown notifier {name!r} (use stdout, log)", err=True)
            raise typer.Exit(1)
    return Notifiers(notifiers)


def _utf8_stdout() -> TextIO:
    # JSONL is UTF-8; a Windows console or pipe may default to a legacy code page.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    return sys.stdout


def _json_value(value: object) -> str:
    """Decimals stay exact as strings ("566600.00"); dates are ISO 8601."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_report(report: RunReport, fetcher: HttpFetcher, snapshots: Path) -> None:
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
    err(f"  requests: {fetcher.requests} ({fetcher.retries} retried)")
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
    err(f"status: {report.status}")
    for f in report.failures:
        err(f"  {f.category}: {f.message}")
        if f.fields:
            err(f"    failing: {', '.join(f.fields)}")
    for warning in report.warnings:
        err(f"  warning: {warning}")
