"""The installed `scrapemend` executable, run as a real process (not through CliRunner,
which replaces the output streams), and the demo script that runs it the same way.

On Windows a process writing to a pipe uses the legacy code page unless told
otherwise, so page text such as "€" or "·" came out in cp1252 and the demo, which
reads the CLI's output as UTF-8, crashed. These run without PYTHONIOENCODING or
PYTHONUTF8 set, the way a newcomer runs the demo.
"""

import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

from examples import run_demo
from scrapemend.spec import load_spec
from scrapemend.state import State

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
DEMO_SPEC = ROOT / "examples" / "sources" / "demo.yaml"
UTF8_SETTINGS = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")


@pytest.fixture
def plain_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name in UTF8_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return dict(os.environ)


def executable() -> str:
    found = shutil.which("scrapemend", path=sysconfig.get_path("scripts"))
    assert found, "the scrapemend script is not installed: pip install -e ."
    return found


def test_executable_writes_utf8(plain_env: dict[str, str], tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    State(db).specs.register(load_spec(DEMO_SPEC))
    shown = subprocess.run(
        [executable(), "show", str(DEMO_SPEC), "--db", str(db)],
        capture_output=True,
        env=plain_env,
        check=True,
    )
    assert r"(€\s?[\d,.]+)" in shown.stdout.decode("utf-8")  # the value field's regex

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "id: s\nfetch: {start: 'https://example.org/'}\n"
        "item: {container: {css: li}, follow: {css: a}}\n"
        "fields:\n  cena€:\n    strategies: [{css: h1}]\n",
        encoding="utf-8",
    )
    invalid = subprocess.run(
        [executable(), "validate", str(bad)], capture_output=True, env=plain_env
    )
    assert invalid.returncode == 1
    assert "field name 'cena€' must be lowercase" in invalid.stderr.decode("utf-8")


def test_demo_reads_non_ascii_cli_output(
    plain_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The step that crashed: the demo printing a repair (a regex with "€") from the CLI.
    db = tmp_path / "state.db"
    State(db).specs.register(load_spec(DEMO_SPEC))
    output = run_demo.scrapemend("show", str(DEMO_SPEC), "--db", str(db))
    assert r"(€\s?[\d,.]+)" in output
    assert r"(€\s?[\d,.]+)" in capsys.readouterr().out
