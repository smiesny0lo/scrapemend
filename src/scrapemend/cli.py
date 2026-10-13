"""Command-line interface. Commands (run / check / heal / diff / promote) land in later phases."""

from pathlib import Path
from typing import Annotated

import typer

from scrapemend import __version__
from scrapemend.spec import SpecError, load_spec

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


@app.command()
def validate(
    spec: Annotated[Path, typer.Argument(help="Path to a source spec YAML file.")],
) -> None:
    """Check a spec file and report every problem with its line number."""
    try:
        source = load_spec(spec)
    except SpecError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    fields = ", ".join(source.fields)
    typer.echo(f"{spec}: ok ({source.id} v{source.version}, {len(source.fields)} fields: {fields})")
