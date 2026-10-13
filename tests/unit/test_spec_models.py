from typing import Any

import pytest
from pydantic import ValidationError

from scrapemend.spec import (
    CssSpec,
    LabelSpec,
    RegexSpec,
    SourceSpec,
    XPathSpec,
)


def spec_data() -> dict[str, Any]:
    """A valid spec for layout v1 of the demo site."""
    return {
        "id": "tenders-demo",
        "fetch": {
            "start": "http://localhost:8000/notices",
            "paginate": {"next": {"css": "li.next a::attr(href)"}, "max_pages": 20},
            "rate": {"rps": 5},
        },
        "item": {
            "container": {"css": "tr.notice"},
            "follow": {"css": "td.title a::attr(href)"},
        },
        "fields": {
            "title": {"required": True, "strategies": [{"css": "h1.notice-title"}]},
            "deadline": {
                "type": "date",
                "date_format": "%d %b %Y",
                "strategies": [
                    {"css": "dd.deadline"},
                    {"label": {"text": "Deadline", "relation": "after"}},
                ],
            },
            "value": {
                "type": "decimal",
                "volatile": True,
                "normalize": ["strip_currency"],
                "strategies": [{"xpath": "//dd[@class='value']"}, {"regex": r"€\s?([\d,.]+)"}],
            },
        },
        "expect": {"min_items_per_run": 100, "max_null_rate": {"value": 0.1}},
    }


def errors(data: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str]]:
    with pytest.raises(ValidationError) as excinfo:
        SourceSpec.model_validate(data)
    return [(tuple(e["loc"]), e["msg"]) for e in excinfo.value.errors()]


def assert_error(data: dict[str, Any], loc: tuple[str | int, ...], text: str) -> None:
    found = errors(data)
    assert any(where == loc and text in msg for where, msg in found), found


# --- valid specs ------------------------------------------------------------------


def test_valid_spec_parses() -> None:
    spec = SourceSpec.model_validate(spec_data())
    assert spec.version == 1
    assert spec.heal.mode == "propose"
    assert spec.item.key == "url"
    assert str(spec.fetch.start) == "http://localhost:8000/notices"

    deadline = spec.fields["deadline"].strategies
    assert isinstance(deadline[0], CssSpec)
    assert isinstance(deadline[1], LabelSpec)
    assert deadline[1].label.relation == "after"

    value = spec.fields["value"].strategies
    assert isinstance(value[0], XPathSpec)
    assert isinstance(value[1], RegexSpec)


def test_css_attr_suffix_is_split_off() -> None:
    strategy = CssSpec(css="li.next a::attr(href)")
    assert strategy.selector == "li.next a"
    assert strategy.attr == "href"

    plain = CssSpec(css="h1")
    assert plain.selector == "h1"
    assert plain.attr is None


def test_label_shorthand() -> None:
    data = spec_data()
    data["fields"]["title"]["strategies"].append({"label": "Title"})
    strategy = SourceSpec.model_validate(data).fields["title"].strategies[-1]
    assert isinstance(strategy, LabelSpec)
    assert strategy.label.text == "Title"
    assert strategy.label.relation == "next_sibling"


def test_spec_is_frozen() -> None:
    spec = SourceSpec.model_validate(spec_data())
    with pytest.raises(ValidationError):
        spec.version = 2  # type: ignore[misc]


def test_key_can_be_a_required_field() -> None:
    data = spec_data()
    data["item"]["key"] = "title"
    assert SourceSpec.model_validate(data).item.key == "title"


def test_round_trips_through_dump() -> None:
    spec = SourceSpec.model_validate(spec_data())
    assert SourceSpec.model_validate(spec.model_dump(mode="json")) == spec


# --- typos and unknown keys -------------------------------------------------------------


def test_unknown_key_is_rejected() -> None:
    data = spec_data()
    data["fields"]["title"]["stratgies"] = data["fields"]["title"].pop("strategies")
    assert_error(data, ("fields", "title", "stratgies"), "Extra inputs are not permitted")


@pytest.mark.parametrize(
    "strategy",
    [{"css": "h1", "regex": "x"}, {"selector": "h1"}, {}],
    ids=["two-keys", "unknown-key", "empty"],
)
def test_strategy_needs_exactly_one_type(strategy: dict[str, str]) -> None:
    data = spec_data()
    data["fields"]["title"]["strategies"] = [strategy]
    assert_error(data, ("fields", "title", "strategies", 0), "exactly one of")


def test_container_must_be_css_or_xpath() -> None:
    data = spec_data()
    data["item"]["container"] = {"label": "Title"}
    assert_error(data, ("item", "container"), "expected exactly one of: css, xpath")


def test_strategies_cannot_be_empty() -> None:
    data = spec_data()
    data["fields"]["title"]["strategies"] = []
    assert_error(data, ("fields", "title", "strategies"), "at least 1")


# --- broken selectors are caught at load time ---------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "message"),
    [
        ({"css": "div >"}, "invalid CSS selector"),
        ({"xpath": "//["}, "invalid XPath"),
        ({"regex": "(unclosed"}, "invalid regex"),
    ],
)
def test_invalid_expressions_are_rejected(strategy: dict[str, str], message: str) -> None:
    data = spec_data()
    data["fields"]["title"]["strategies"] = [strategy]
    found = errors(data)
    assert any(message in m for _, m in found), found


# --- cross-references ------------------------------------------------------------------------


def test_key_must_be_a_field() -> None:
    data = spec_data()
    data["item"]["key"] = "reference"
    assert_error(data, ("item",), "key 'reference' is not a field")


def test_key_field_must_be_required() -> None:
    data = spec_data()
    data["item"]["key"] = "deadline"
    assert_error(data, ("item",), "must be required")


def test_url_key_needs_follow() -> None:
    data = spec_data()
    del data["item"]["follow"]
    assert_error(data, ("item",), "needs item.follow")


def test_url_is_a_reserved_field_name() -> None:
    data = spec_data()
    data["fields"]["url"] = {"strategies": [{"css": "a"}]}
    assert_error(data, ("fields",), "reserved")


def test_null_rates_must_name_fields() -> None:
    data = spec_data()
    data["expect"]["max_null_rate"] = {"price": 0.1}
    assert_error(data, ("expect",), "unknown fields: price")


# --- value constraints ------------------------------------------------------------------------


def test_field_names_are_snake_case() -> None:
    data = spec_data()
    data["fields"]["Title"] = data["fields"].pop("title")
    found = errors(data)
    assert any(loc[:2] == ("fields", "Title") for loc, _ in found), found


def test_date_format_only_for_dates() -> None:
    data = spec_data()
    data["fields"]["title"]["date_format"] = "%Y"
    assert_error(data, ("fields", "title"), "date_format only applies")


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("fetch", "rate", "rps"), 0),
        (("fetch", "paginate", "max_pages"), 0),
        (("fetch", "start"), "not a url"),
        (("expect", "max_null_rate", "value"), 1.5),
        (("heal", "mode"), "yolo"),
        (("id",), "Tenders Demo"),
        (("fields", "title", "type"), "float"),
        (("fields", "title", "normalize"), ["titlecase"]),
    ],
)
def test_bad_values_are_rejected(path: tuple[str, ...], bad: Any) -> None:
    data = spec_data()
    target = data
    for part in path[:-1]:
        target = target.setdefault(part, {})
    target[path[-1]] = bad
    found = errors(data)
    assert any(loc[: len(path)] == path for loc, _ in found), found
