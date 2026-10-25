"""Optional extras exist for code that uses them, not for plans."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def imported_packages() -> set[str]:
    found = set()
    for path in [*ROOT.joinpath("src").rglob("*.py"), *ROOT.joinpath("examples").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r"^\s*(?:from|import)\s+(\w+)", text, re.MULTILINE))
    return found


def test_every_extra_is_used_by_some_code() -> None:
    pyproject = tomllib.loads(ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    extras: dict[str, list[str]] = pyproject["project"]["optional-dependencies"]
    used = imported_packages() | {"jinja2"}  # the demo site's templates, loaded by FastAPI
    unused = {}
    for extra, requirements in extras.items():
        if extra == "dev":  # tools, not imports
            continue
        names = [re.split(r"[<>=!~ ]", r, maxsplit=1)[0] for r in requirements]
        missing = [n for n in names if n.split("[")[0] not in used]
        if missing:
            unused[extra] = missing
    assert unused == {}
