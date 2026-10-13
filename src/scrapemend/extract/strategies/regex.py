import re

from scrapemend.extract.document import Document, squash
from scrapemend.spec import RegexSpec


class RegexStrategy:
    """First match of a regular expression in the visible text (not the markup).
    The value is the first capture group if the pattern has one, else the whole match."""

    def __init__(self, spec: RegexSpec) -> None:
        self.spec = spec
        self._pattern = re.compile(spec.regex)

    def extract(self, doc: Document) -> str | None:
        match = self._pattern.search(doc.text)
        if match is None:
            return None
        return squash(match.group(1) if self._pattern.groups else match.group(0)) or None
