"""Command-line interface. Commands (check / heal / diff / promote) land in later phases."""

import io
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, TextIO

import typer
from pydantic import ValidationError

from scrapemend import __version__
from scrapemend.fetch import DEFAULT_USER_AGENT, BlockedError, FetchError, HttpFetcher
from scrapemend.pipeline import RunAborted, RunSummary, SourceRun
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, SpecError, load_spec

app = typer.Typer(
    name="scrapemend",
    help="Self-healing scraper engine.",
    no_args_is_help=True,
    add_completion=False,
)


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


SpecArgument = Annotated[Path, typer.Argument(help="Path to a source spec YAML file.")]


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
    snapshots: Annotated[
        Path, typer.Option(envvar="SCRAPEMEND_SNAPSHOT_DIR", help="Where raw pages are stored.")
    ] = Path("snapshots"),
    user_agent: Annotated[
        str, typer.Option(envvar="SCRAPEMEND_USER_AGENT", help="User-Agent header to send.")
    ] = DEFAULT_USER_AGENT,
    start: Annotated[
        str | None, typer.Option(help="Start URL to use instead of the spec's fetch.start.")
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Stop after this many items.")] = None,
) -> None:
    """Scrape a source and write one JSON record per line.

    A summary goes to stderr. Exit code 2 means the run was aborted (e.g. blocked).
    """
    source = _load(spec)
    if start:
        source = _with_start(source, start)
    store = FilesystemSnapshotStore(snapshots)

    with HttpFetcher.for_spec(source.fetch, user_agent=user_agent) as fetcher:
        source_run = SourceRun(source, fetcher, store, limit=limit)
        output = out.open("w", encoding="utf-8", newline="\n") if out else _utf8_stdout()
        try:
            for result in source_run.records():
                output.write(json.dumps(result.record, default=_json_value, ensure_ascii=False))
                output.write("\n")
                output.flush()
        except BlockedError as e:
            _abort(source_run.summary, fetcher, snapshots, f"blocked by the site ({e}); stopping")
        except (FetchError, RunAborted) as e:
            _abort(source_run.summary, fetcher, snapshots, str(e))
        except KeyboardInterrupt:
            _abort(source_run.summary, fetcher, snapshots, "interrupted", code=130)
        finally:
            if out:
                output.close()
        _print_summary(source_run.summary, fetcher, snapshots)


# --- helpers ---------------------------------------------------------------------------


def _load(spec: Path) -> SourceSpec:
    try:
        return load_spec(spec)
    except SpecError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _with_start(source: SourceSpec, start: str) -> SourceSpec:
    data = source.model_dump(mode="json")
    data["fetch"]["start"] = start
    try:
        return SourceSpec.model_validate(data)
    except ValidationError as e:
        typer.echo(f"--start: {e.errors()[0]['msg']}", err=True)
        raise typer.Exit(1) from None


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


def _abort(
    summary: RunSummary, fetcher: HttpFetcher, snapshots: Path, reason: str, code: int = 2
) -> None:
    _print_summary(summary, fetcher, snapshots)
    typer.echo(f"run aborted: {reason}", err=True)
    raise typer.Exit(code)


def _print_summary(summary: RunSummary, fetcher: HttpFetcher, snapshots: Path) -> None:
    def err(line: str) -> None:
        typer.echo(line, err=True)

    def per_field(counts: dict[str, int]) -> str:
        return ", ".join(f"{name} {n}/{summary.records}" for name, n in counts.items())

    err(
        f"{summary.source_id} v{summary.spec_version}, run {summary.run_id}: "
        f"{summary.listing_pages} listing pages, {summary.items} items, "
        f"{summary.records} records, {len(summary.failures)} failed"
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
