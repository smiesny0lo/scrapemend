from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scrapemend.spec import CssSpec, LabelSpec, SpecError, load_patch, load_spec, parse_patch
from scrapemend.spec.patch import SpecPatch

ROOT = Path(__file__).parents[2]
DEMO = load_spec(ROOT / "examples" / "sources" / "demo.yaml")


def patch(**data: Any) -> SpecPatch:
    return SpecPatch.model_validate(data)


def test_applies_selectors_and_strategies_as_a_new_version() -> None:
    p = patch(
        container={"css": "tr.tender-row"},
        follow={"css": "a.tender-link"},
        next_page={"css": "a[rel=next]"},
        fields={"title": {"strategies": [{"css": "h1.x"}, {"label": "Title"}]}},
    )
    new = p.apply(DEMO, version=2)
    assert new.version == 2
    assert new.item.container == CssSpec(css="tr.tender-row")
    assert new.item.follow == CssSpec(css="a.tender-link")
    assert new.fetch.paginate is not None and new.fetch.paginate.next == CssSpec(css="a[rel=next]")
    assert new.fields["title"].strategies == (CssSpec(css="h1.x"), LabelSpec(label="Title"))
    assert p.changes() == ["item.container", "item.follow", "fetch.paginate.next", "fields.title"]


def test_everything_else_is_untouched() -> None:
    new = patch(fields={"deadline": {"strategies": [{"css": "dd.x"}]}}).apply(DEMO, version=2)
    assert new.fields["deadline"].date_format == "%d %b %Y"  # kept
    assert new.fields["deadline"].required and new.fields["deadline"].type == "date"
    for name in set(DEMO.fields) - {"deadline"}:
        assert new.fields[name] == DEMO.fields[name]
    assert (new.fetch, new.expect, new.heal, new.item) == (
        DEMO.fetch,
        DEMO.expect,
        DEMO.heal,
        DEMO.item,
    )


def test_normalizing_options_can_change() -> None:
    p = patch(
        fields={
            "value": {
                "strategies": [{"regex": r"Value: (\S+)"}],
                "normalize": ["strip_currency", "strip"],
                "null_values": ["n/a"],
            },
            "deadline": {"strategies": [{"css": "time"}], "date_format": "%Y-%m-%d"},
        }
    )
    new = p.apply(DEMO, version=2)
    assert new.fields["value"].normalize == ("strip_currency", "strip")
    assert new.fields["value"].null_values == ("n/a",)
    assert new.fields["deadline"].date_format == "%Y-%m-%d"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"fetch": {"start": "https://evil.example/"}},
        {"rate": {"rps": 1000}},
        {"fields": {"title": {"strategies": [{"css": "h1"}], "required": False}}},
        {"fields": {"value": {"strategies": [{"css": "x"}], "type": "str"}}},
        {"expect": {"min_items_per_run": 0}},
        {"heal": {"mode": "auto"}},
    ],
)
def test_a_patch_cannot_change_anything_else(forbidden: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        patch(**forbidden)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown fields: price"):
        patch(fields={"price": {"strategies": [{"css": "x"}]}}).apply(DEMO, version=2)


def test_next_page_needs_pagination() -> None:
    data = DEMO.model_dump(mode="json")
    data["fetch"]["paginate"] = None
    single_page = type(DEMO).model_validate(data)
    with pytest.raises(ValueError, match="no pagination"):
        patch(next_page={"css": "a"}).apply(single_page, version=2)


def test_broken_strategies_fail_validation() -> None:
    with pytest.raises(ValidationError, match="invalid CSS selector"):
        patch(fields={"title": {"strategies": [{"css": "div >"}]}})
    with pytest.raises(ValidationError):
        patch(fields={"title": {"strategies": []}})


def test_empty_patch() -> None:
    assert patch().is_empty
    assert not patch(container={"css": "x"}).is_empty


def test_confidence_is_a_probability() -> None:
    with pytest.raises(ValidationError):
        patch(confidence=1.5)


# --- files ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["demo-v2.yaml", "demo-v3.yaml"])
def test_example_repairs_load_and_apply(name: str) -> None:
    repaired = load_patch(ROOT / "examples" / "repairs" / name).apply(DEMO, version=2)
    assert repaired.version == 2


@pytest.mark.parametrize("name", ["v2-wrong-field.yaml", "v3-swapped-dates.yaml", "v2-empty.yaml"])
def test_bad_repair_fixtures_are_still_valid_patches(name: str) -> None:
    load_patch(ROOT / "tests" / "fixtures" / "repairs" / name).apply(DEMO, version=2)


def test_patch_errors_point_at_lines() -> None:
    with pytest.raises(SpecError) as excinfo:
        parse_patch("confidence: 0.9\nfields:\n  title:\n    strategies: [{ css: 'div >' }]\n")
    [problem] = excinfo.value.problems
    assert (problem.line, problem.path) == (4, "fields.title.strategies[0].css")
