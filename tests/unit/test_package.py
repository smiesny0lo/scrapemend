from importlib import metadata

from packaging.version import Version
from typer.testing import CliRunner

import scrapemend
from scrapemend.cli import app

runner = CliRunner()


def test_version_is_the_installed_packages() -> None:
    # One version, valid for PyPI: __version__ is what the wheel's metadata says.
    assert scrapemend.__version__ == metadata.version("scrapemend")
    assert str(Version(scrapemend.__version__)) == scrapemend.__version__  # normalized PEP 440


def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert scrapemend.__version__ in result.output


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert scrapemend.__version__ in result.output
