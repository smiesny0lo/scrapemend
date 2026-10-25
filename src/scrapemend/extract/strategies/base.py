from typing import Protocol, runtime_checkable

from lxml import etree

from scrapemend.extract.document import Document


class Strategy(Protocol):
    """Finds one field value in a document.

    Returns the value as text, or None when nothing (or only whitespace) was found,
    so the engine can fall through to the field's next strategy.
    """

    def extract(self, doc: Document) -> str | None: ...


@runtime_checkable
class Locating(Protocol):
    """A strategy that can also say which element it reads its value from. The
    verification gate uses it to notice two fields reading the same element. The
    built-in strategies all can; for a custom strategy it is optional."""

    def locate(self, doc: Document) -> etree._Element | None: ...
