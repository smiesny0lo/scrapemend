"""Snapshots: every raw response, stored so pages can be re-parsed without re-fetching."""

import codecs
import re
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from specmend.extract.document import Document

SnapshotKind = Literal["listing", "detail"]

# Source and run ids become directory names, so they must be plain path segments.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# Response headers worth nothing for re-parsing and possibly sensitive.
_DROPPED_HEADERS = frozenset({"set-cookie"})


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    run_id: str
    spec_version: int
    kind: SnapshotKind
    url: str  # final URL, after redirects; relative links resolve against it
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    fetched_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    fingerprint: str | None = None  # page structure hash, filled in by detection
    body: bytes = Field(repr=False)

    @field_validator("source_id", "run_id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError(f"{value!r} must be letters, digits, '.', '-' or '_'")
        return value

    @field_validator("headers")
    @classmethod
    def _clean_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        cleaned = {name.lower(): value for name, value in headers.items()}
        return {name: value for name, value in cleaned.items() if name not in _DROPPED_HEADERS}

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def text(self) -> str:
        return decode_body(self.body, self.content_type)

    def document(self) -> Document:
        return Document.parse(self.text, url=self.url)


class SnapshotStore(Protocol):
    """Where snapshots live. Ids are opaque strings handed out by `put`."""

    def put(self, snapshot: Snapshot) -> str:
        """Store a snapshot and return its id."""
        ...

    def get(self, snapshot_id: str) -> Snapshot:
        """Load a snapshot; raises KeyError if there is none with that id."""
        ...

    def runs(self, source_id: str) -> list[str]:
        """Run ids that have snapshots for this source, oldest first."""
        ...

    def snapshots(self, source_id: str, run_id: str) -> list[str]:
        """Snapshot ids of one run, in the order they were stored."""
        ...

    def delete_run(self, source_id: str, run_id: str) -> None:
        """Delete every snapshot of one run (nothing, if it has none)."""
        ...


# --- decoding ----------------------------------------------------------------------------

_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE)


def decode_body(body: bytes, content_type: str = "") -> str:
    """Decode a response body the way browsers do: byte order mark, then the
    Content-Type charset, then <meta charset>, then UTF-8, then Windows-1252."""
    if body.startswith(codecs.BOM_UTF8):
        return body[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    declared: list[str] = []
    if header := _HEADER_CHARSET.search(content_type):
        declared.append(header.group(1))
    if meta := _META_CHARSET.search(body[:4096]):
        declared.append(meta.group(1).decode("ascii"))
    for charset in declared:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:  # a charset name Python doesn't know
            continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("windows-1252", errors="replace")
