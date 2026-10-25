import re

from lxml import etree
from lxml.cssselect import CSSSelector

from scrapemend.extract.document import Document, squash, text_of
from scrapemend.spec import CssSpec, RegexSpec


class RegexStrategy:
    """First match of a regular expression in the visible text (not the markup).
    The value is the first capture group if the pattern has one, else the whole match.
    With `within`, only the first element matching that selector is searched."""

    def __init__(self, spec: RegexSpec) -> None:
        self.spec = spec
        self._pattern = re.compile(spec.regex)
        within = spec.within
        self._within: etree.XPath | None  # CSSSelector is an XPath subclass
        if within is None:
            self._within = None
        elif isinstance(within, CssSpec):
            self._within = CSSSelector(within.selector, translator="html")
        else:
            self._within = etree.XPath(within.xpath)

    def extract(self, doc: Document) -> str | None:
        found = self._find(doc)
        return found[1] if found else None

    def locate(self, doc: Document) -> etree._Element | None:
        """The element whose text was searched: the `within` element, or the whole scope."""
        found = self._find(doc)
        return found[0] if found else None

    def _find(self, doc: Document) -> tuple[etree._Element, str] | None:
        element: etree._Element = doc.root
        text = doc.text
        if self._within is not None:
            found = self._within(doc.root)
            found = found if isinstance(found, list) else []
            elements = [e for e in found if isinstance(e, etree._Element)]
            if not elements:
                return None
            element = elements[0]
            text = text_of(element)
        match = self._pattern.search(text)
        if match is None:
            return None
        value = squash(match.group(1) if self._pattern.groups else match.group(0))
        return (element, value) if value else None
