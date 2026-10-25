from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from examples.mutating_site.app import create_app
from examples.mutating_site.records import format_date, format_money, generate_notices
from scrapemend.extract.document import Document
from scrapemend.extract.strategies import (
    CssStrategy,
    LabelStrategy,
    RegexStrategy,
    XPathStrategy,
    build,
)
from scrapemend.spec import CssSpec, StrategySpec, XPathSpec

STRATEGY = TypeAdapter[StrategySpec](StrategySpec)


def run(strategy: dict[str, Any], html: str) -> str | None:
    return build(STRATEGY.validate_python(strategy)).extract(Document.parse(html))


# --- document text --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>  Bridge \n  maintenance  </p>", "Bridge maintenance"),
        ("<p><b>Hel</b>lo <i>world</i></p>", "Hello world"),  # inline: no break
        ("<dl><dt>Deadline</dt><dd>3 Nov</dd></dl>", "Deadline 3 Nov"),  # blocks: break
        ("<p>A<br>B</p>", "A B"),
        ("<p>A<script>var x = 1;</script><!-- note -->B</p>", "AB"),
        ("<p>A&nbsp;B</p>", "A B"),
        ("", ""),
    ],
)
def test_document_text(html: str, text: str) -> None:
    assert Document.parse(html).text == text


# --- css ------------------------------------------------------------------------------


def test_css_text_of_first_match() -> None:
    html = "<ul><li class='x'> one </li><li class='x'>two</li></ul>"
    assert run({"css": "li.x"}, html) == "one"


def test_css_attribute() -> None:
    html = "<a class='next' href=' /notices?page=2 '>Next</a>"
    assert run({"css": "a.next::attr(href)"}, html) == "/notices?page=2"


@pytest.mark.parametrize(
    ("strategy", "html"),
    [
        ({"css": "h1"}, "<h2>Title</h2>"),  # no match
        ({"css": "h1"}, "<h1>  </h1>"),  # only whitespace
        ({"css": "a::attr(href)"}, "<a>no href</a>"),  # missing attribute
    ],
)
def test_css_nothing_found(strategy: dict[str, Any], html: str) -> None:
    assert run(strategy, html) is None


def test_css_stays_inside_scope() -> None:
    page = Document.parse(
        "<table><tr class='row'><td class='name'>A</td></tr>"
        "<tr class='row'><td class='name'>B</td></tr></table>"
    )
    second_row = page.root.cssselect("tr.row")[1]
    strategy = CssStrategy(CssSpec(css="td.name"))
    assert strategy.extract(page.scope(second_row)) == "B"


# --- xpath ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xpath", "expected"),
    [
        ("//h1", "Bridge maintenance"),
        ("//a/@href", "/notices/1"),
        ("//h1/text()", "Bridge maintenance"),
        ("string(//a)", "Details"),
        ("count(//p)", "2"),
        ("boolean(//h1)", "true"),
        ("number(//nothing)", None),  # NaN
        ("//nothing", None),
    ],
)
def test_xpath_results(xpath: str, expected: str | None) -> None:
    html = "<h1> Bridge maintenance </h1><p>x</p><p><a href='/notices/1'>Details</a></p>"
    assert run({"xpath": xpath}, html) == expected


def test_xpath_relative_vs_absolute_in_scope() -> None:
    page = Document.parse("<div id='a'><b>A</b></div><div id='b'><b>B</b></div>")
    scope = page.scope(page.root.get_element_by_id("b"))
    relative = XPathStrategy(XPathSpec(xpath=".//b"))
    absolute = XPathStrategy(XPathSpec(xpath="//b"))
    assert relative.extract(scope) == "B"
    assert absolute.extract(scope) == "A"


# --- regex ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("regex", "expected"),
    [
        (r"Ref\.\s*(TN-\d+)", "TN-0001"),
        (r"TN-\d+", "TN-0001"),  # no group: whole match
        (r"Status:\s*(\w+)", None),
        (r"(x)?Ref", None),  # group did not take part
    ],
)
def test_regex(regex: str, expected: str | None) -> None:
    html = "<p>Ref. <b>TN-0001</b> · Published 3 Nov 2026</p>"
    assert run({"regex": regex}, html) == expected


@pytest.mark.parametrize(
    ("within", "regex", "expected"),
    [
        ({"css": "div.meta"}, " · (.+)$", "Dunmere"),
        ({"css": "div.meta"}, "^(.+?) · ", "Construction"),
        ({"xpath": "//div[@class='meta']"}, " · (.+)$", "Dunmere"),
        ({"css": "div.missing"}, "(.+)", None),
    ],
)
def test_regex_within_an_element(within: dict[str, str], regex: str, expected: str | None) -> None:
    html = "<p>Category · Location</p><div class='meta'>Construction · Dunmere</div>"
    assert run({"regex": regex, "within": within}, html) == expected


def test_regex_searches_visible_text_only() -> None:
    html = "<a href='/secret-123'>link</a><script>var id = 'secret-456';</script>"
    assert run({"regex": r"secret-\d+"}, html) is None


# --- label: next_sibling --------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "html", "expected"),
    [
        ("Deadline", "<dl><dt>Deadline</dt><dd>3 Nov 2026</dd></dl>", "3 Nov 2026"),
        ("Deadline", "<tr><th>DEADLINE:</th><td>3 Nov 2026</td></tr>", "3 Nov 2026"),
        ("Deadline:", "<dt>Deadline</dt><dd>3 Nov 2026</dd>", "3 Nov 2026"),
        ("Contracting authority", "<dt>Contracting\n  authority</dt><dd>City</dd>", "City"),
        ("Deadline", "<div><span>Deadline</span></div><div>3 Nov 2026</div>", "3 Nov 2026"),
        ("Deadline", "<dt>Deadline</dt><!-- c --><dd>3 Nov 2026</dd>", "3 Nov 2026"),
        ("Deadline", "<dt>Deadline</dt><dd> </dd><dt>Deadline</dt><dd>later</dd>", "later"),
        ("Deadline", "<dt>Deadlines</dt><dd>3 Nov 2026</dd>", None),
        ("Deadline", "<dl><dt>Deadline</dt></dl>", None),
    ],
)
def test_label_next_sibling(label: str, html: str, expected: str | None) -> None:
    assert run({"label": label}, html) == expected


# --- label: after ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>Deadline: 3 Nov 2026</p>", "3 Nov 2026"),
        ("<p>Deadline <strong>3 Nov 2026</strong></p>", "3 Nov 2026"),
        ("<p><b>Deadline:</b> 3 Nov 2026</p>", "3 Nov 2026"),
        ("<p>deadline \u2013 3 Nov 2026</p>", "3 Nov 2026"),
        ("<div><p>Deadline: 3 Nov</p><p>Value: €5</p></div>", "3 Nov"),
        ("<p>Deadlines extended to 3 Nov</p>", None),
        ("<p>Deadline:</p>", None),
        # A block of fields starting with the label must not yield all of them.
        ("<dl><dt>Deadline</dt><dd>3 Nov</dd><dt>Status</dt><dd>Open</dd></dl>", None),
    ],
)
def test_label_after(html: str, expected: str | None) -> None:
    assert run({"label": {"text": "Deadline", "relation": "after"}}, html) == expected


# --- build ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "cls"),
    [
        ({"css": "h1"}, CssStrategy),
        ({"xpath": "//h1"}, XPathStrategy),
        ({"label": "Title"}, LabelStrategy),
        ({"regex": "x"}, RegexStrategy),
    ],
)
def test_build_picks_the_strategy_class(strategy: dict[str, Any], cls: type) -> None:
    assert isinstance(build(STRATEGY.validate_python(strategy)), cls)


# --- the demo site's three layouts ----------------------------------------------------

NOTICE = next(n for n in generate_notices() if n.value is not None)
EXPECTED = {
    "reference": NOTICE.id,
    "title": NOTICE.title,
    "authority": NOTICE.authority,
    "deadline": format_date(NOTICE.deadline),
    "status": NOTICE.status,
    "value": format_money(NOTICE.value),
}
DETAIL_PAGES = {
    layout: Document.parse(TestClient(create_app(layout=layout)).get(f"/notices/{NOTICE.id}").text)
    for layout in (1, 2, 3)
}

# Which strategies survive which redesign: v2 needs new selectors (labels keep working),
# v3 needs "after" labels and regexes.
MATRIX: list[tuple[dict[str, Any], str, set[int]]] = [
    ({"css": "dd.deadline"}, "deadline", {1}),
    ({"css": "dd.tender__closing"}, "deadline", {2}),
    ({"css": "h1"}, "title", {1, 2}),
    ({"xpath": "//dt[.='Deadline']/following-sibling::dd[1]"}, "deadline", {1, 2}),
    ({"label": "Deadline"}, "deadline", {1, 2}),
    ({"label": {"text": "Deadline", "relation": "after"}}, "deadline", {3}),
    ({"label": "Contracting authority"}, "authority", {1, 2, 3}),
    ({"label": "Reference"}, "reference", {1, 2}),
    ({"label": {"text": "Reference", "relation": "after"}}, "reference", set()),
    ({"regex": r"Ref\.\s*(TN-\d{4}-\d{4})"}, "reference", {3}),
    ({"regex": r"Status:\s*(\w+)"}, "status", {3}),
    ({"regex": r"Estimated value:?\s*(€\s?[\d,.]+)"}, "value", {1, 2, 3}),
]


@pytest.mark.parametrize("layout", [1, 2, 3], ids=lambda n: f"v{n}")
@pytest.mark.parametrize(("strategy", "field", "works_on"), MATRIX, ids=str)
def test_demo_layouts(
    strategy: dict[str, Any], field: str, works_on: set[int], layout: int
) -> None:
    result = build(STRATEGY.validate_python(strategy)).extract(DETAIL_PAGES[layout])
    # Where a strategy doesn't apply it must find nothing, never a wrong value.
    assert result == (EXPECTED[field] if layout in works_on else None)
