from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from examples.mutating_site.app import create_app
from examples.mutating_site.records import Notice, generate_notices
from scrapemend.extract import Document, Extractor, FieldResult
from scrapemend.spec import FieldSpec, load_spec

DEMO_SPEC = load_spec(Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml")
NOTICES = generate_notices()


def extract(fields: dict[str, dict[str, Any]], html: str, url: str | None = None) -> Any:
    specs = {name: FieldSpec.model_validate(spec) for name, spec in fields.items()}
    return Extractor(specs).extract(Document.parse(html, url=url))


PRICE: dict[str, Any] = {
    "type": "decimal",
    "normalize": ["strip_currency"],
    "strategies": [{"css": ".a"}, {"css": ".b"}],
}


def test_first_strategy_wins() -> None:
    result = extract({"price": PRICE}, "<p class='a'>€5</p><p class='b'>€6</p>")
    assert result.fields["price"] == FieldResult(Decimal(5), "€5", 0)
    assert not result.fields["price"].used_fallback


def test_falls_back_when_nothing_found() -> None:
    result = extract({"price": PRICE}, "<p class='b'>€6</p>")
    assert result.fields["price"] == FieldResult(Decimal(6), "€6", 1)
    assert result.fields["price"].used_fallback


def test_falls_back_when_text_does_not_convert() -> None:
    # .a found something, but "See documents" is not a price: the wrong element.
    result = extract({"price": PRICE}, "<p class='a'>See documents</p><p class='b'>€6</p>")
    assert result.fields["price"] == FieldResult(Decimal(6), "€6", 1)


def test_reports_why_found_text_was_unusable() -> None:
    result = extract({"price": PRICE}, "<p class='a'>See documents</p><p class='b'>call us</p>")
    price = result.fields["price"]
    assert (price.value, price.raw, price.strategy) == (None, "See documents", None)
    assert price.error == "strategy 0: 'See documents' is not a number"


def test_nothing_found_is_not_an_error() -> None:
    assert extract({"price": PRICE}, "<p>nothing</p>").fields["price"] == FieldResult(
        None, None, None
    )


def test_null_value_counts_as_found() -> None:
    spec = PRICE | {"null_values": ["Not disclosed"]}
    result = extract({"price": spec}, "<p class='a'>Not disclosed</p><p class='b'>€6</p>")
    assert result.fields["price"] == FieldResult(None, "Not disclosed", 0)


def test_record_includes_page_url() -> None:
    fields: dict[str, dict[str, Any]] = {
        "title": {"strategies": [{"css": "h1"}]},
        "pdf": {"type": "url", "strategies": [{"css": "a::attr(href)"}]},
    }
    result = extract(fields, "<h1>T</h1><a href='doc.pdf'>PDF</a>", url="http://example.org/n/1")
    assert result.record == {
        "url": "http://example.org/n/1",
        "title": "T",
        "pdf": "http://example.org/n/doc.pdf",
    }


# --- the demo spec against the demo site ---------------------------------------------------------


def detail_page(notice: Notice, layout: int, client: TestClient) -> Document:
    path = f"/notices/{notice.id}"
    return Document.parse(client.get(path).text, url=f"http://localhost:8000{path}")


def expected_record(notice: Notice) -> dict[str, Any]:
    return {
        "url": f"http://localhost:8000/notices/{notice.id}",
        "reference": notice.id,
        "title": notice.title,
        "authority": notice.authority,
        "category": notice.category,
        "location": notice.location,
        "published": notice.published,
        "deadline": notice.deadline,
        "value": notice.value,
        "status": notice.status,
        "description": notice.description,
    }


def test_demo_spec_extracts_layout_v1_exactly() -> None:
    extractor = Extractor(DEMO_SPEC.fields)
    client = TestClient(create_app(layout=1))
    for notice in NOTICES:
        result = extractor.extract(detail_page(notice, 1, client))
        assert result.record == expected_record(notice)
        assert {r.strategy for r in result.fields.values()} == {0}, notice.id
        assert all(r.error is None for r in result.fields.values()), notice.id


# On a redesign, fields with fallbacks keep working through them (an early warning);
# the others come back empty. Undisclosed values can't be told from missing ones there.
@pytest.mark.parametrize(
    ("layout", "survivors"),
    [(2, {"reference", "authority", "deadline", "value"}), (3, {"authority", "value"})],
)
def test_demo_spec_on_redesigned_layouts(layout: int, survivors: set[str]) -> None:
    extractor = Extractor(DEMO_SPEC.fields)
    client = TestClient(create_app(layout=layout))
    for notice in NOTICES[:40]:
        result = extractor.extract(detail_page(notice, layout, client))
        expected = expected_record(notice)
        for name, field in result.fields.items():
            if name in survivors and not (name == "value" and notice.value is None):
                assert (field.value, field.strategy) == (expected[name], 1), (notice.id, name)
            else:
                assert (field.value, field.strategy) == (None, None), (notice.id, name)
