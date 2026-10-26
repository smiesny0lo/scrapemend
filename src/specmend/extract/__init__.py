"""Extraction engine with ordered per-field strategy fallback."""

from specmend.extract.document import Document
from specmend.extract.engine import Extractor, FieldResult, RecordResult
from specmend.extract.normalize import Value

__all__ = ["Document", "Extractor", "FieldResult", "RecordResult", "Value"]
