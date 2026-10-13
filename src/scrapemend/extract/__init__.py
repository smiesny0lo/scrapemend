"""Extraction engine with ordered per-field strategy fallback."""

from scrapemend.extract.document import Document
from scrapemend.extract.engine import Extractor, FieldResult, RecordResult
from scrapemend.extract.normalize import Value

__all__ = ["Document", "Extractor", "FieldResult", "RecordResult", "Value"]
