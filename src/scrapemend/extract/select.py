"""Structural selection: item containers on a listing page, and links to follow."""

from typing import assert_never

from lxml.cssselect import CSSSelector
from lxml.html import HtmlElement

from scrapemend.extract.document import Document
from scrapemend.extract.normalize import parse_url
from scrapemend.extract.strategies import build
from scrapemend.spec import CssSpec, SelectorSpec, XPathSpec


def select_all(spec: SelectorSpec, doc: Document) -> list[Document]:
    """Every element the selector matches, each as a document scoped to it.
    XPath inside a scope should start with `.//`; `//` searches the whole page."""
    match spec:
        case CssSpec():
            result = CSSSelector(spec.selector, translator="html")(doc.root)
        case XPathSpec():
            result = doc.root.xpath(spec.xpath)
        case _:
            assert_never(spec)
    elements = result if isinstance(result, list) else []  # e.g. count(...) gives a number
    return [doc.scope(e) for e in elements if isinstance(e, HtmlElement)]


def find_link(spec: SelectorSpec, doc: Document) -> str | None:
    """The absolute URL a selector points at, or None. A CSS selector without
    `::attr(...)` reads the matched element's href."""
    if isinstance(spec, CssSpec) and spec.attr is None:
        spec = CssSpec(css=f"{spec.css}::attr(href)")
    raw = build(spec).extract(doc)
    if raw is None:
        return None
    try:
        return parse_url(raw, doc.url)
    except ValueError:
        return None
