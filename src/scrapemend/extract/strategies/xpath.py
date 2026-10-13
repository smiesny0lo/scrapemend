from lxml import etree

from scrapemend.extract.document import Document, squash, text_of
from scrapemend.spec import XPathSpec


class XPathStrategy:
    """First result of an XPath expression: an element's text, an attribute, or a
    string/number expression like `string(//h1)`.

    Inside an item container, start the path with `.//` to stay within the item;
    `//` always searches the whole page.
    """

    def __init__(self, spec: XPathSpec) -> None:
        self.spec = spec
        self._xpath = etree.XPath(spec.xpath)

    def extract(self, doc: Document) -> str | None:
        result = self._xpath(doc.root)
        first: object = (result[0] if result else None) if isinstance(result, list) else result
        if first is None:
            return None
        if isinstance(first, etree._Element):
            value = text_of(first)
        elif isinstance(first, bool):
            value = str(first).lower()
        elif isinstance(first, float):
            value = "" if first != first else f"{first:g}"  # NaN means no value
        elif isinstance(first, bytes):
            value = squash(first.decode())
        else:
            value = squash(str(first))
        return value or None
