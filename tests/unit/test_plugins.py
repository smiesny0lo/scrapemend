from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import specmend.events as events
import specmend.extract.strategies as strategies
from examples.plugins.jsonld import JsonLdStrategy
from specmend.cli import app
from specmend.events import Event, StreamNotifier, make_notifier
from specmend.extract import Document, Extractor
from specmend.extract.strategies import UnknownStrategy, build, register_strategy
from specmend.spec import CustomSpec, FieldSpec, parse_spec

PAGE = """
<script type="application/ld+json">{"@type": "Organization", "name": "Shop"}</script>
<script type="application/ld+json">not json</script>
<script type="application/ld+json">
  [{"@type": "Product", "name": "Kettle", "offers": {"price": "24.90", "currency": "EUR"}}]
</script>
<span class="price">€24.90</span>
"""


class EntryPoint:
    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self.target = target

    def load(self) -> Any:
        return self.target


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = dict(strategies._registry)
    yield
    strategies._registry.clear()
    strategies._registry.update(saved)


def field(**options: Any) -> FieldSpec:
    return FieldSpec.model_validate(
        {"type": "decimal", "strategies": [{"custom": {"name": "jsonld", "options": options}}]}
    )


def test_jsonld_example_plugin() -> None:
    register_strategy("jsonld", JsonLdStrategy)
    result = Extractor({"price": field(path="offers.price", type="Product")}).extract(
        Document.parse(PAGE)
    )
    assert str(result.fields["price"].value) == "24.90"
    first_name = build(
        CustomSpec.model_validate({"custom": {"name": "jsonld", "options": {"path": "name"}}})
    )
    assert first_name.extract(Document.parse(PAGE)) == "Shop"


def test_custom_strategies_fall_back_like_any_other() -> None:
    register_strategy("jsonld", JsonLdStrategy)
    spec = FieldSpec.model_validate(
        {
            "type": "decimal",
            "normalize": ["strip_currency"],
            "strategies": [
                {"custom": {"name": "jsonld", "options": {"path": "offers.missing"}}},
                {"css": "span.price"},
            ],
        }
    )
    result = Extractor({"price": spec}).extract(Document.parse(PAGE))
    assert (str(result.fields["price"].value), result.fields["price"].strategy) == ("24.90", 1)


def test_shorthand() -> None:
    assert CustomSpec.model_validate({"custom": "jsonld"}).custom.name == "jsonld"


def test_entry_point_plugins_are_found_without_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(group: str) -> list[EntryPoint]:
        return [EntryPoint("jsonld", JsonLdStrategy)] if group == "specmend.strategies" else []

    monkeypatch.setattr(strategies, "entry_points", fake)
    assert isinstance(
        build(CustomSpec.model_validate({"custom": {"name": "jsonld", "options": {"path": "x"}}})),
        JsonLdStrategy,
    )


def test_unknown_custom_strategy_fails_when_the_spec_is_built() -> None:
    with pytest.raises(UnknownStrategy, match="'nope'"):
        Extractor({"x": FieldSpec.model_validate({"strategies": [{"custom": "nope"}]})})


def test_cli_reports_a_missing_plugin_before_fetching(tmp_path: Path) -> None:
    spec = tmp_path / "s.yaml"
    spec.write_text(
        "id: s\nfetch: {start: 'https://example.org/'}\n"
        "item: {container: {css: li}, follow: {css: a}}\n"
        "fields:\n  title:\n    required: true\n    strategies: [{custom: missing-plugin}]\n",
        encoding="utf-8",
    )
    parse_spec(spec.read_text("utf-8"))  # the file itself is fine
    for command in (["validate", str(spec)], ["check", str(spec), "--db", str(tmp_path / "s.db")]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 1
        assert "no installed plugin provides the custom strategy 'missing-plugin'" in result.output


def test_plugin_options_are_checked_by_the_plugin() -> None:
    with pytest.raises(ValueError, match=r"needs options\.path"):
        JsonLdStrategy({})


# --- notifiers ------------------------------------------------------------------------------


def test_make_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(make_notifier("stdout"), StreamNotifier)
    with pytest.raises(ValueError, match="unknown notifier 'pager'"):
        make_notifier("pager")

    received: list[Event] = []

    class Pager:
        def notify(self, event: Event) -> None:
            received.append(event)

    monkeypatch.setattr(
        events,
        "entry_points",
        lambda group: [EntryPoint("pager", Pager)] if group == "specmend.notifiers" else [],
    )
    pager = make_notifier("pager")
    pager.notify(Event(kind="quarantined", source_id="s", message="m"))
    assert [e.kind for e in received] == ["quarantined"]
