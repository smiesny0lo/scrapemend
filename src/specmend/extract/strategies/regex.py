from lxml import etree
from lxml.cssselect import CSSSelector

from specmend.extract import patterns
from specmend.extract.document import Document, squash, text_of
from specmend.extract.patterns import RegexTimeout, compile_pattern
from specmend.extract.strategies.base import StrategyTimeout
from specmend.spec import CssSpec, RegexSpec

MAX_TIMEOUTS = 3  # searches that may time out before a strategy stops trying, for good


class RegexStrategy:
    """First match of a regular expression in the visible text (not the markup).
    The value is the first capture group if the pattern has one, else the whole match.
    With `within`, only the first element matching that selector is searched.

    It runs on the spec's engine (see specmend.extract.patterns). A search with
    Python's re that takes longer than its time limit raises StrategyTimeout; after
    MAX_TIMEOUTS of them, the strategy raises it at once, without searching."""

    def __init__(self, spec: RegexSpec) -> None:
        self.spec = spec
        self._pattern = compile_pattern(spec.regex, spec.engine)
        self.timeouts = 0
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
        if self.timeouts >= MAX_TIMEOUTS:
            raise StrategyTimeout(
                f"regex {self.spec.regex!r} timed out {self.timeouts} times; not tried again"
            )
        try:
            matched = self._pattern.first(text)
        except RegexTimeout:
            self.timeouts += 1
            limit = f"{patterns.TIME_LIMIT:g}s"
            raise StrategyTimeout(f"regex {self.spec.regex!r} took longer than {limit}") from None
        value = squash(matched)
        return (element, value) if value else None
