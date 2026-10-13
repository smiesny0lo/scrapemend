from pathlib import Path

import pytest
from typer.testing import CliRunner

from scrapemend.cli import app
from scrapemend.spec import Problem, SpecError, load_spec, parse_spec

DEMO_SPEC = Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml"

# Line numbers matter in these tests; this is line 1.
BASE = """\
id: demo
fetch:
  start: http://localhost:8000/notices
item:
  container: { css: "tr.notice" }
  follow: { css: "td.title a::attr(href)" }
fields:
  title:
    required: true
    strategies:
      - css: "h1"
"""


def problems(text: str) -> list[Problem]:
    with pytest.raises(SpecError) as excinfo:
        parse_spec(text)
    return excinfo.value.problems


def replace(old: str, new: str) -> str:
    assert old in BASE
    return BASE.replace(old, new)


# --- loading -------------------------------------------------------------------------


def test_demo_spec_loads() -> None:
    spec = load_spec(DEMO_SPEC)
    assert spec.id == "tenders-demo"
    assert len(spec.fields) == 10
    assert spec.fields["deadline"].date_format == "%d %b %Y"


def test_base_spec_parses() -> None:
    assert parse_spec(BASE).fields["title"].required


def test_off_stays_a_string() -> None:
    # Plain YAML 1.1 turns `off` into False ("the Norway problem").
    spec = parse_spec(BASE + "heal:\n  mode: off\n")
    assert spec.heal.mode == "off"


# --- problems point at the right line ---------------------------------------------------


def test_unknown_key() -> None:
    found = problems(BASE + "    stratgies: []\n")
    assert found == [Problem(12, 5, "fields.title.stratgies", "unknown key")]


def test_missing_key_is_reported_at_its_parent() -> None:
    [problem] = problems(replace("  start: http://localhost:8000/notices", "  timeout: 5"))
    assert (problem.line, problem.path, problem.message) == (2, "fetch.start", "Field required")


def test_error_inside_a_strategy() -> None:
    [problem] = problems(replace('- css: "h1"', "- label: { text: Title, relation: below }"))
    assert problem.line == 11
    assert problem.path == "fields.title.strategies[0].label.relation"
    assert "'next_sibling' or 'after'" in problem.message


def test_invalid_selector_without_follow_up_noise() -> None:
    # Pydantic also reports "strategies should have at least 1 item" here; that is dropped.
    [problem] = problems(replace('"h1"', '"div >"'))
    assert problem.line == 11
    assert problem.path == "fields.title.strategies[0].css"
    assert problem.message.startswith("invalid CSS selector 'div >': Expected selector")


def test_empty_strategies_are_still_reported() -> None:
    [problem] = problems(replace('\n      - css: "h1"', " []"))
    assert problem.path == "fields.title.strategies"
    assert "at least 1" in problem.message


def test_all_problems_reported_in_line_order() -> None:
    text = replace("id: demo", "id: Demo Spec") + "  title:\n    strategies: [{ css: a }]\n"
    found = problems(text)
    assert [(p.line, p.path) for p in found] == [(1, "id"), (12, "fields.title")]
    assert found[1].message == "duplicate key"


def test_duplicate_key_errors_point_at_the_copy_that_counts() -> None:
    # YAML keeps the last duplicate, so its errors must point there, not at the first.
    found = problems(BASE + '  title:\n    strategies: [{ css: "div >" }]\n')
    assert [(p.line, p.path) for p in found] == [
        (12, "fields.title"),
        (13, "fields.title.strategies[0].css"),
    ]


# --- broken files ----------------------------------------------------------------------------


def test_yaml_syntax_error() -> None:
    [problem] = problems("id: [unclosed\nfetch: {}\n")
    assert problem.line is not None
    assert problem.message.startswith("invalid YAML")


@pytest.mark.parametrize(
    ("text", "message"),
    [("", "spec is empty"), ("# only a comment\n", "spec is empty"), ("- a\n- b\n", "mapping")],
)
def test_not_a_spec(text: str, message: str) -> None:
    [problem] = problems(text)
    assert message in problem.message


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="cannot read file"):
        load_spec(tmp_path / "nope.yaml")


def test_error_message_lists_every_problem() -> None:
    with pytest.raises(SpecError) as excinfo:
        parse_spec(BASE + "    stratgies: []\nheal:\n  mode: yolo\n", source="demo.yaml")
    assert str(excinfo.value).splitlines() == [
        "demo.yaml: 2 problems",
        "  line 12, col 5: fields.title.stratgies: unknown key",
        "  line 14, col 9: heal.mode: Input should be 'off', 'propose' or 'auto'",
    ]


# --- CLI ----------------------------------------------------------------------------------------


runner = CliRunner()


def test_cli_validate_ok() -> None:
    result = runner.invoke(app, ["validate", str(DEMO_SPEC)])
    assert result.exit_code == 0
    assert "ok (tenders-demo v1, 10 fields" in result.output


def test_cli_validate_reports_problems(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(BASE + "    stratgies: []\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "line 12, col 5: fields.title.stratgies: unknown key" in result.output
