import random

from typer.testing import CliRunner

from scrapemend.blame import EXCUSES, random_excuse
from scrapemend.cli import app

runner = CliRunner()


def test_random_excuse_is_known() -> None:
    assert random_excuse() in EXCUSES


def test_random_excuse_is_deterministic_with_seed() -> None:
    assert random_excuse(random.Random(42)) == random_excuse(random.Random(42))


def test_cli_blame_command() -> None:
    result = runner.invoke(app, ["blame"])
    assert result.exit_code == 0
    assert result.output.strip() in EXCUSES
