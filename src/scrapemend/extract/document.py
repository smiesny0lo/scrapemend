"""Parsed HTML that strategies read from."""

import re
from collections.abc import Iterator
from functools import cached_property
from typing import Self

import lxml.html
from lxml import etree
from lxml.html import HtmlElement

# Never visible page content, so never part of an element's text.
HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript", "head"})

# Text inside these flows on without a break: <b>Hel</b>lo reads "Hello". Every
# other element (div, p, td, dd, br, ...) separates its text from its neighbours.
INLINE_TAGS = frozenset(
    {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i",
        "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time",
        "u", "var", "wbr",
    }
)  # fmt: skip

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")


class Document:
    """An HTML page, or one element of it that strategies are scoped to (an item container)."""

    def __init__(self, root: HtmlElement, url: str | None = None) -> None:
        self.root = root
        self.url = url

    @classmethod
    def parse(cls, html: str | bytes, url: str | None = None) -> Self:
        if isinstance(html, str):
            # lxml rejects already-decoded text that still declares an encoding.
            html = _XML_DECLARATION.sub("", html, count=1)
        if not html.strip():
            html = "<html></html>"
        return cls(lxml.html.document_fromstring(html), url)

    def scope(self, element: HtmlElement) -> "Document":
        """The same page, limited to one element."""
        return Document(element, self.url)

    @cached_property
    def text(self) -> str:
        return text_of(self.root)

    def elements(self) -> Iterator[HtmlElement]:
        """The root and every visible element below it, in document order."""
        yield self.root
        yield from _visible_descendants(self.root)


def text_of(element: etree._Element) -> str:
    """An element's visible text with whitespace collapsed, roughly as a browser shows it."""
    parts: list[str] = []
    _collect_text(element, parts)
    return squash("".join(parts))


def squash(text: str | None) -> str:
    """Collapse runs of whitespace to single spaces and trim the ends."""
    return " ".join(text.split()) if text else ""


def _collect_text(element: etree._Element, parts: list[str]) -> None:
    block = element.tag not in INLINE_TAGS
    if block:
        parts.append(" ")
    if element.text:
        parts.append(element.text)
    for child in element:
        if _is_visible(child):
            _collect_text(child, parts)
        if child.tail:
            parts.append(child.tail)
    if block:
        parts.append(" ")


def _visible_descendants(element: HtmlElement) -> Iterator[HtmlElement]:
    for child in element:
        if _is_visible(child):
            yield child
            yield from _visible_descendants(child)


def _is_visible(node: etree._Element) -> bool:
    # Comments and processing instructions have a function as their tag.
    return isinstance(node.tag, str) and node.tag not in HIDDEN_TAGS
