from typing import cast

from lxml.cssselect import CSSSelector
from lxml.html import HtmlElement

from scrapemend.extract.document import Document, squash, text_of
from scrapemend.spec import CssSpec


class CssStrategy:
    """Text of the first element matching a CSS selector, or one of its attributes
    with `::attr(name)`. The selector may also match the scope element itself."""

    def __init__(self, spec: CssSpec) -> None:
        self.spec = spec
        self._select = CSSSelector(spec.selector, translator="html")

    def extract(self, doc: Document) -> str | None:
        element = self.locate(doc)
        if element is None:
            return None
        value = squash(element.get(self.spec.attr)) if self.spec.attr else text_of(element)
        return value or None

    def locate(self, doc: Document) -> HtmlElement | None:
        """The element the value is read from."""
        # A CSS selection is always a list of elements; the stubs type it like any XPath result.
        for element in cast(list[HtmlElement], self._select(doc.root)):
            return element
        return None
