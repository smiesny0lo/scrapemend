"""Load source specs from YAML, reporting every problem with its line and column."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from scrapemend.spec.models import SourceSpec

Loc = tuple[int | str, ...]


@dataclass(frozen=True)
class Problem:
    line: int | None  # 1-based
    column: int | None  # 1-based
    path: str  # where in the spec, e.g. "fields.title.strategies[0]"; "" for the whole file
    message: str

    def __str__(self) -> str:
        where = f"line {self.line}, col {self.column}: " if self.line else ""
        path = f"{self.path}: " if self.path else ""
        return f"{where}{path}{self.message}"


class SpecError(Exception):
    """A spec file could not be loaded."""

    def __init__(self, source: str, problems: list[Problem]) -> None:
        self.source = source
        self.problems = problems
        count = f"{len(problems)} problem{'s' if len(problems) != 1 else ''}"
        lines = "\n".join(f"  {p}" for p in problems)
        super().__init__(f"{source}: {count}\n{lines}")


class _SpecLoader(yaml.SafeLoader):
    """SafeLoader where only true/false are booleans. In plain YAML 1.1, `mode: off`
    would load as False and `country: no` as False ("the Norway problem")."""


_BOOL_TAG = "tag:yaml.org,2002:bool"
_SpecLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_spec(path: str | Path) -> SourceSpec:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        problem = Problem(None, None, "", f"cannot read file: {e.strerror}")
        raise SpecError(str(path), [problem]) from None
    return parse_spec(text, source=str(path))


def parse_spec(text: str, source: str = "<string>") -> SourceSpec:
    try:
        root = yaml.compose(text, Loader=_SpecLoader)
        data = yaml.load(text, Loader=_SpecLoader)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        line, col = (mark.line + 1, mark.column + 1) if mark else (None, None)
        raise SpecError(source, [Problem(line, col, "", f"invalid YAML: {e.problem}")]) from None

    if root is None:
        raise SpecError(source, [Problem(None, None, "", "spec is empty")])
    if not isinstance(data, dict):
        raise SpecError(source, [Problem(1, 1, "", "spec must be a mapping of keys to values")])
    problems = _duplicate_keys(root)
    try:
        spec = SourceSpec.model_validate(data)
    except ValidationError as e:
        problems += [_problem(root, err) for err in _relevant(e.errors())]
    if problems:
        raise SpecError(source, sorted(problems, key=lambda p: (p.line or 0, p.column or 0)))
    return spec


def _duplicate_keys(node: yaml.Node, path: str = "") -> list[Problem]:
    """YAML silently keeps the last of two equal keys; in a spec that is always a mistake."""
    problems: list[Problem] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[Any] = set()
        for key, value in node.value:
            child = f"{path}.{key.value}" if path else str(key.value)
            if key.value in seen:
                mark = key.start_mark
                problems.append(Problem(mark.line + 1, mark.column + 1, child, "duplicate key"))
            seen.add(key.value)
            problems += _duplicate_keys(value, child)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            problems += _duplicate_keys(item, f"{path}[{i}]")
    return problems


def _relevant(errors: list[ErrorDetails]) -> list[ErrorDetails]:
    """Drop "list too short" errors that only exist because its items failed to validate;
    the item errors already explain the problem."""
    return [
        err
        for err in errors
        if not (
            err["type"] == "too_short"
            and any(
                other["loc"][: len(err["loc"])] == err["loc"]
                and len(other["loc"]) > len(err["loc"])
                for other in errors
            )
        )
    ]


def _problem(root: yaml.Node, err: ErrorDetails) -> Problem:
    mark, path = _locate(root, err["loc"])
    message = err["msg"].removeprefix("Value error, ")
    if err["type"] == "extra_forbidden":
        message = "unknown key"
    return Problem(mark.line + 1, mark.column + 1, path, message)


def _locate(root: yaml.Node, loc: Loc) -> tuple[yaml.Mark, str]:
    """Follow an error location through the YAML tree to the closest node.

    Parts that don't exist in the YAML are skipped: pydantic repeats the strategy
    type as a tag (`strategies, 0, label, label, text`), and a missing key has no
    node of its own, so it is reported at its parent with the key in the path.
    """
    node, mark, parts = root, root.start_mark, []
    for i, part in enumerate(loc):
        child = _child(node, part)
        if child is None:
            is_missing_key = isinstance(node, yaml.MappingNode) and not str(part).startswith("[")
            if i == len(loc) - 1 and is_missing_key:
                parts.append(part)
            continue
        key_node, node = child
        parts.append(part)
        # Point at the value for scalars, at the key line for nested blocks.
        mark = node.start_mark if isinstance(node, yaml.ScalarNode) else key_node.start_mark
    return mark, _format_path(parts)


def _child(node: yaml.Node, part: int | str) -> tuple[yaml.Node, yaml.Node] | None:
    if isinstance(node, yaml.MappingNode):
        for key, value in reversed(node.value):  # the last duplicate wins, as when loading
            if key.value == part:
                return key, value
    elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
        item = node.value[part]
        return item, item
    return None


def _format_path(parts: list[int | str]) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else f".{part}" if out else str(part)
    return out
