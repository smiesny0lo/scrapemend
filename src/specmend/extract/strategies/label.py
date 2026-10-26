import re

from lxml.html import HtmlElement

from specmend.extract.document import HIDDEN_TAGS, INLINE_TAGS, Document, text_of
from specmend.spec import LabelSpec

_LINE_TAGS = INLINE_TAGS | HIDDEN_TAGS | {"br"}

# Between a label and its value: whitespace, colon, hyphen, en dash, em dash.
_SEPARATORS = r"[\s:\-\u2013\u2014]*"


class LabelStrategy:
    """Find a value by the label shown next to it, e.g. "Deadline".

    Labels match case-insensitively, ignoring extra whitespace and a trailing colon.

    - next_sibling: the label is a whole element and the value is the next element,
      as in <dt>Deadline</dt><dd>VALUE</dd> or <span>Deadline</span><div>VALUE</div>.
    - after: the value follows the label on the same line of text, as in
      <p>Deadline: VALUE</p> or <p><b>Deadline</b> <strong>VALUE</strong></p>.
    """

    def __init__(self, spec: LabelSpec) -> None:
        self.spec = spec
        words = r"\s+".join(re.escape(word) for word in spec.label.text.rstrip(":").split())
        self._whole = re.compile(rf"{words}\s*:?", re.IGNORECASE)
        self._prefix = re.compile(rf"{words}(?!\w){_SEPARATORS}(.*)", re.IGNORECASE | re.DOTALL)

    def extract(self, doc: Document) -> str | None:
        found = self._find(doc)
        return found[1] if found else None

    def locate(self, doc: Document) -> HtmlElement | None:
        """The element the value is read from: the label's sibling, or the line of text."""
        found = self._find(doc)
        return found[0] if found else None

    def _find(self, doc: Document) -> tuple[HtmlElement, str] | None:
        if self.spec.label.relation == "next_sibling":
            return self._next_sibling(doc)
        return self._after(doc)

    def _next_sibling(self, doc: Document) -> tuple[HtmlElement, str] | None:
        # Document order visits outer elements first, so <div><span>Deadline</span></div>
        # tries the div's sibling before the span's.
        for element in doc.elements():
            if self._whole.fullmatch(text_of(element)):
                sibling = _next_visible_sibling(element)
                if sibling is not None and (value := text_of(sibling)):
                    return sibling, value
        return None

    def _after(self, doc: Document) -> tuple[HtmlElement, str] | None:
        # Only single lines of text count: a <dl> or <div> of blocks can also start
        # with the label, but "the rest of it" would be every field that follows.
        for element in doc.elements():
            if not _is_single_line(element):
                continue
            match = self._prefix.match(text_of(element))
            if match and match.group(1):
                return element, match.group(1)
        return None


def _next_visible_sibling(element: HtmlElement) -> HtmlElement | None:
    sibling = element.getnext()
    while sibling is not None and not isinstance(sibling.tag, str):
        sibling = sibling.getnext()  # skip comments
    return sibling


def _is_single_line(element: HtmlElement) -> bool:
    """True if the element holds only text and inline markup (<b>, <span>, <br>, ...)."""
    return all(
        not isinstance(node.tag, str) or node.tag in _LINE_TAGS
        for node in element.iterdescendants()
    )
