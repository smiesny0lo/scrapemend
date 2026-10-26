import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version
from typer.testing import CliRunner

import specmend
from specmend.cli import app
from specmend.heal.providers import ProviderError
from specmend.heal.providers.anthropic import AnthropicProvider

ROOT = Path(__file__).parents[2]
runner = CliRunner()


def test_version_is_the_installed_packages() -> None:
    # One version, valid for PyPI: __version__ is what the wheel's metadata says.
    assert specmend.__version__ == metadata.version(specmend.DISTRIBUTION)
    assert str(Version(specmend.__version__)) == specmend.__version__  # normalized PEP 440


def test_the_distribution_name_is_one_constant() -> None:
    # The name on PyPI is DISTRIBUTION, and pyproject.toml and every install hint say
    # the same.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == specmend.DISTRIBUTION
    assert f"{specmend.DISTRIBUTION}[demo,anthropic]" in project["optional-dependencies"]["dev"]
    hint = f'pip install "{specmend.DISTRIBUTION}[anthropic]"'
    assert hint in (ROOT / "docs" / "providers.md").read_text(encoding="utf-8")


def test_the_anthropic_provider_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # as if it weren't installed
    with pytest.raises(ProviderError) as missing:
        AnthropicProvider()
    assert f'pip install "{specmend.DISTRIBUTION}[anthropic]"' in str(missing.value)


def test_cli_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert specmend.__version__ in result.output


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert specmend.__version__ in result.output
