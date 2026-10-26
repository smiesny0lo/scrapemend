import pytest
from fastapi.testclient import TestClient

from examples.mutating_site.app import create_app
from scrapemend.detect.fingerprint import CHANGED_BELOW, features, fingerprint, similarity
from scrapemend.extract import Document


def fp(html: str) -> str:
    return fingerprint(Document.parse(html))


TABLE = "<table class='notices'>{}</table>"
ROW = "<tr class='notice'><td class='title'><a href='/{0}'>Title {0}</a></td></tr>"


def test_text_does_not_matter() -> None:
    a = fp(TABLE.format(ROW.format(1)))
    b = fp(TABLE.format(ROW.format(2)).replace("Title", "Something else entirely"))
    assert similarity(a, b) == 1.0


def test_number_of_repeated_items_does_not_matter() -> None:
    one = fp(TABLE.format(ROW.format(1)))
    many = fp(TABLE.format("".join(ROW.format(i) for i in range(50))))
    assert similarity(one, many) == 1.0


def test_digits_in_class_names_are_ignored() -> None:
    assert (
        similarity(fp("<div class='css-1a2b'>x</div>"), fp("<div class='css-9z8y'>x</div>")) == 1.0
    )


def test_renamed_classes_change_the_structure() -> None:
    before = fp(TABLE.format(ROW.format(1)))
    after = fp(
        TABLE.format(ROW.format(1)).replace("notice", "tender-row").replace("title", "col-name")
    )
    assert similarity(before, after) < CHANGED_BELOW


def test_features_describe_elements_and_their_parents() -> None:
    found = features(Document.parse("<div class='b a'><p>x</p></div>"))
    assert {"div.a.b", "p", "div.a.b>p", "body>div.a.b"} <= found


def test_empty_page() -> None:
    assert similarity(fp(""), fp("")) == 1.0


def test_similarity_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        similarity("abc", fp(""))


# --- calibrated on the demo site -----------------------------------------------------------------


@pytest.fixture(scope="module")
def demo() -> dict[tuple[int, str], list[Document]]:
    pages: dict[tuple[int, str], list[Document]] = {}
    for layout in (1, 2, 3):
        client = TestClient(create_app(layout=layout))
        pages[(layout, "listing")] = [
            Document.parse(client.get(f"/notices?page={n}").text) for n in (1, 5, 10)
        ]
        pages[(layout, "detail")] = [
            Document.parse(client.get(f"/notices/TN-2026-{n:04d}").text) for n in (1, 27, 150)
        ]
    return pages


def shared(a: Document, b: Document) -> float:
    """The exact share of features two pages have in common, which similarity estimates."""
    fa, fb = features(a), features(b)
    return len(fa & fb) / len(fa | fb)


@pytest.mark.parametrize("kind", ["listing", "detail"])
@pytest.mark.parametrize("layout", [1, 2, 3])
def test_same_layout_is_recognised(
    demo: dict[tuple[int, str], list[Document]], layout: int, kind: str
) -> None:
    first, *others = demo[(layout, kind)]
    for other in others:
        assert shared(first, other) >= 0.85
        # A 64-value signature estimates that share give or take about 0.1 (the first
        # listing page, without a link back, shares 87% with the last one): still well
        # clear of CHANGED_BELOW.
        assert similarity(fingerprint(first), fingerprint(other)) >= CHANGED_BELOW + 0.15


@pytest.mark.parametrize("kind", ["listing", "detail"])
@pytest.mark.parametrize(("a", "b"), [(1, 2), (1, 3), (2, 3)])
def test_redesigns_are_detected(
    demo: dict[tuple[int, str], list[Document]], a: int, b: int, kind: str
) -> None:
    assert similarity(fingerprint(demo[(a, kind)][0]), fingerprint(demo[(b, kind)][0])) < 0.3
