"""Anchors: known-good records from good runs, which repairs are verified against.

After every complete good run, the N valid records whose keys hash lowest become
the source's anchors. Hashing makes the choice stable: the same items keep being
picked run after run while they exist, so their values stay comparable, and the
choice is spread over the whole site rather than the first page.
"""

import hashlib
import heapq
import sqlite3

from pydantic import BaseModel

from scrapemend.extract.normalize import Encoded, encode
from scrapemend.pipeline import ValidatedRecord

__all__ = ["Anchor", "AnchorPicker", "AnchorStore", "AnchorValue", "encode", "key_of", "split"]

AnchorValue = Encoded


class Anchor(BaseModel):
    key: str
    url: str | None
    values: dict[str, AnchorValue]
    raw: dict[str, str | None]  # the text each value was read from, e.g. "27 Sep 2026"
    strategies: dict[str, int | None]  # which strategy won each field
    snapshot_id: str | None
    run_id: str

    @classmethod
    def from_record(cls, record: ValidatedRecord, key: str, run_id: str) -> "Anchor":
        return cls(
            key=key,
            url=record.url,
            values={name: encode(f.value) for name, f in record.fields.items()},
            raw={name: f.raw for name, f in record.fields.items()},
            strategies={name: f.strategy for name, f in record.fields.items()},
            snapshot_id=record.snapshot_id,
            run_id=run_id,
        )


def key_of(record: ValidatedRecord, key_field: str) -> str | None:
    value = record.url if key_field == "url" else encode(record.record.get(key_field))
    return None if value is None else str(value)


def _rank(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


class AnchorPicker:
    """Keeps the `n` valid records whose keys hash lowest, while records stream past."""

    def __init__(self, n: int, key_field: str, run_id: str) -> None:
        self.n = n
        self.key_field = key_field
        self.run_id = run_id
        self._heap: list[tuple[int, str, Anchor]] = []  # (-rank, key, anchor): a max-heap
        self._keys: set[str] = set()

    def add(self, record: ValidatedRecord) -> None:
        if self.n == 0 or not record.valid:
            return
        key = key_of(record, self.key_field)
        if key is None or key in self._keys:
            return
        self._keys.add(key)
        anchor = Anchor.from_record(record, key, self.run_id)
        heapq.heappush(self._heap, (-_rank(key), key, anchor))
        if len(self._heap) > self.n:
            heapq.heappop(self._heap)

    def anchors(self) -> list[Anchor]:
        """Lowest hash first: the order anchors are shown to a repair provider in."""
        return [anchor for _, _, anchor in sorted(self._heap, reverse=True)]


class AnchorStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def replace(self, source_id: str, anchors: list[Anchor]) -> None:
        with self.db:
            self.db.execute("DELETE FROM anchors WHERE source_id = ?", (source_id,))
            self.db.executemany(
                "INSERT INTO anchors VALUES (?, ?, ?, ?)",
                [(source_id, rank, a.key, a.model_dump_json()) for rank, a in enumerate(anchors)],
            )

    def all(self, source_id: str) -> list[Anchor]:
        rows = self.db.execute(
            "SELECT anchor FROM anchors WHERE source_id = ? ORDER BY rank", (source_id,)
        )
        return [Anchor.model_validate_json(row["anchor"]) for row in rows]


def split(anchors: list[Anchor], shown: int) -> tuple[list[Anchor], list[Anchor]]:
    """Anchors a repair provider may see, and the rest, held out: a repair that only
    memorised the shown values fails on the held-out ones."""
    return anchors[:shown], anchors[shown:]
