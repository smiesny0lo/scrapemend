from typing import Protocol

from scrapemend.extract.document import Document


class Strategy(Protocol):
    """Finds one field value in a document.

    Returns the value as text, or None when nothing (or only whitespace) was found,
    so the engine can fall through to the field's next strategy.
    """

    def extract(self, doc: Document) -> str | None: ...
