"""A real plugin package, installed with pip and found through its entry points.

The other plugin tests fake `importlib.metadata.entry_points`. This one builds a
tiny wheel declaring a strategy, a repair provider and a notifier, installs it
with pip (offline, into a temporary directory) and runs the real `scrapemend`
executable with that directory on its path, the way a user's plugin is found.
"""

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import scrapemend.extract.strategies as strategies
from scrapemend.events import Event, make_notifier
from scrapemend.extract import Document
from scrapemend.extract.strategies import build
from scrapemend.spec import CustomSpec

pytestmark = pytest.mark.integration

MODULE = '''\
from scrapemend.extract import Document


class Shout:
    """The page's h1, in capitals."""

    def __init__(self, options):
        self.css = options.get("css", "h1")

    def extract(self, doc: Document):
        found = doc.root.cssselect(self.css)
        return found[0].text_content().strip().upper() if found else None


class TinyProvider:
    name = "tiny"

    def propose(self, context):
        return None


class TinyNotifier:
    events = []

    def notify(self, event):
        TinyNotifier.events.append(event)
'''

ENTRY_POINTS = """\
[scrapemend.strategies]
shout = tiny_plugin:Shout

[scrapemend.providers]
tiny = tiny_plugin:TinyProvider

[scrapemend.notifiers]
tiny = tiny_plugin:TinyNotifier
"""

SPEC = """\
id: plugged
fetch: {start: 'https://example.org/'}
item: {container: {css: li}, follow: {css: a}}
fields:
  title:
    required: true
    strategies: [{custom: shout}, {css: h1}]
"""


def build_wheel(directory: Path) -> Path:
    """tiny_plugin 0.1 as a wheel: a zip with the module and its .dist-info."""
    info = "tiny_plugin-0.1.dist-info"
    files = {
        "tiny_plugin/__init__.py": MODULE,
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: tiny-plugin\nVersion: 0.1\n",
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: scrapemend-tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{info}/entry_points.txt": ENTRY_POINTS,
    }
    record = []
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record.append(f"{name},sha256={digest.decode()},{len(text.encode())}")
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = "\n".join(record) + "\n"
    wheel = directory / "tiny_plugin-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return wheel


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where pip installed the plugin."""
    work = tmp_path_factory.mktemp("plugin")
    target = work / "site"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-index", "--no-deps",
         "--disable-pip-version-check", "--target", str(target), str(build_wheel(work))],
        check=True,
    )  # fmt: skip
    assert (target / "tiny_plugin-0.1.dist-info" / "entry_points.txt").exists()
    return target


def scrapemend(*args: str, plugin_path: Path | None) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("scrapemend", path=sysconfig.get_path("scripts"))
    assert executable, "the scrapemend script is not installed: pip install -e ."
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if plugin_path is not None:
        env["PYTHONPATH"] = str(plugin_path)
    return subprocess.run(
        [executable, *args], capture_output=True, text=True, encoding="utf-8", env=env
    )


def test_the_cli_finds_an_installed_plugin(site_dir: Path, tmp_path: Path) -> None:
    spec = tmp_path / "plugged.yaml"
    spec.write_text(SPEC, encoding="utf-8")
    plain = tmp_path / "plain.yaml"  # no custom strategy: only the provider is a plugin
    plain.write_text(SPEC.replace("{custom: shout}, ", ""), encoding="utf-8")
    db = str(tmp_path / "state.db")
    heal = ("heal", str(plain), "--provider", "tiny", "--db", db)

    without = scrapemend("validate", str(spec), plugin_path=None)
    assert without.returncode == 1
    assert "no installed plugin provides the custom strategy 'shout'" in without.stderr
    assert "unknown repair provider 'tiny'" in scrapemend(*heal, plugin_path=None).stderr

    valid = scrapemend("validate", str(spec), plugin_path=site_dir)
    assert valid.returncode == 0, valid.stderr
    healed = scrapemend(*heal, plugin_path=site_dir)
    assert healed.returncode == 0, healed.stderr
    assert "heal: skipped: no failed run to repair" in healed.stderr  # the provider loaded


@pytest.fixture
def plugin_on_path(site_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.syspath_prepend(str(site_dir))
    saved = dict(strategies._registry)
    yield
    strategies._registry.clear()
    strategies._registry.update(saved)
    sys.modules.pop("tiny_plugin", None)


@pytest.mark.usefixtures("plugin_on_path")
def test_entry_points_of_an_installed_plugin_load() -> None:
    shout = build(CustomSpec.model_validate({"custom": "shout"}))
    assert shout.extract(Document.parse("<h1>Kettle</h1>")) == "KETTLE"
    notifier = make_notifier("tiny")
    notifier.notify(Event(kind="quarantined", source_id="plugged", message="needs a human"))
    plugin = sys.modules["tiny_plugin"]  # imported from the pip-installed package
    assert plugin.__file__ is not None
    assert Path(plugin.__file__).is_relative_to(Path(sys.path[0]))
    assert isinstance(notifier, plugin.TinyNotifier)
    assert [e.kind for e in plugin.TinyNotifier.events] == ["quarantined"]
