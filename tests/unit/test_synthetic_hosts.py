"""Tests and their fixtures only name hosts that belong to nobody (RFC 2606), or this
machine: a real domain in a test (x.org is the X.Org Foundation's) can end up
fetched, linked or mistaken for a real target."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RESERVED = re.compile(
    r"(?:[\w-]+\.)*(?:example\.(?:org|com|net)|[\w-]+\.example)|localhost|127\.0\.0\.1"
)
NAMED = {"api.anthropic.com"}  # the API's own host, in a fake connection error; never contacted


def test_tests_only_name_reserved_hosts() -> None:
    found: dict[str, set[str]] = {}
    for path in ROOT.joinpath("tests").rglob("*"):
        if path.suffix not in (".py", ".yaml", ".json", ".md") or "__pycache__" in path.parts:
            continue
        for host in re.findall(r"https?://([\w.-]+)", path.read_text(encoding="utf-8")):
            if not RESERVED.fullmatch(host) and host not in NAMED:
                found.setdefault(str(path.relative_to(ROOT)), set()).add(host)
    assert found == {}
