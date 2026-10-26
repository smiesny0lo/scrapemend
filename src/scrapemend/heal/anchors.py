"""Anchors: known-good records from good runs, which repairs are verified against.

After every complete good run, the N valid records whose keys hash lowest become
the source's anchors. Hashing makes the choice stable: the same items keep being
picked run after run while they exist, so their values stay comparable, and the
choice is spread over the whole site rather than the first page. The hash is of
the key alone, with a fixed seed: a URL's scheme and host are left out, so the same
site served from another host or port keeps the same anchors.

The values of every valid record of that run are kept too, by key: a repair must
reproduce them on whichever pages of the failed run were stored.

A drift run's anchors and records are held instead: they become the source's only
if a human accepts the drift (scrapemend accept-drift). Only the latest drift
run's are held, until it is accepted or an ok run replaces them.
"""

import hashlib
import heapq
import json
import sqlite3
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel

from scrapemend.extract import RecordResult
from scrapemend.extract.normalize import Encoded, encode
from scrapemend.pipeline import ValidatedRecord

__all__ = [
    "Anchor",
    "AnchorPicker",
    "AnchorStore",
    "AnchorValue",
    "KnownRecords",
    "encode",
    "identity",
    "key_of",
    "split",
]

AnchorValue = Encoded
KnownRecords = Mapping[str, Mapping[str, AnchorValue]]  # key -> field -> value
ANCHOR_SEED = "scrapemend-anchors"


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


def key_of(record: RecordResult | ValidatedRecord, key_field: str) -> str | None:
    value = record.url if key_field == "url" else encode(record.record.get(key_field))
    return None if value is None else str(value)


def identity(key: str) -> str:
    """A record key as it identifies a record wherever the site is served from: a URL
    without its scheme and host (and port), just its path and query."""
    if key.startswith(("http://", "https://")):
        parts = urlsplit(key)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return key


def rank(key: str, seed: str) -> int:
    """Where a key falls in a random order fixed by the seed: a hash of the seed and
    the key's identity."""
    digest = hashlib.sha256(f"{seed}:{identity(key)}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class AnchorPicker:
    """Keeps the `n` valid records whose keys hash lowest, while records stream past,
    and the values of every valid record."""

    def __init__(self, n: int, key_field: str, run_id: str) -> None:
        self.n = n
        self.key_field = key_field
        self.run_id = run_id
        self._heap: list[tuple[int, str, Anchor]] = []  # (-rank, key, anchor): a max-heap
        self._values: dict[str, dict[str, AnchorValue]] = {}

    def add(self, record: ValidatedRecord) -> None:
        if self.n == 0 or not record.valid:
            return
        key = key_of(record, self.key_field)
        if key is None or key in self._values:
            return
        anchor = Anchor.from_record(record, key, self.run_id)
        self._values[key] = anchor.values
        heapq.heappush(self._heap, (-rank(key, ANCHOR_SEED), key, anchor))
        if len(self._heap) > self.n:
            heapq.heappop(self._heap)

    def anchors(self) -> list[Anchor]:
        """Lowest hash first: the order anchors are shown to a repair provider in."""
        return [anchor for _, _, anchor in sorted(self._heap, reverse=True)]

    def records(self) -> dict[str, dict[str, AnchorValue]]:
        """The values of every valid record, by key, in the order they came."""
        return self._values


class AnchorStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def replace(
        self, source_id: str, anchors: list[Anchor], records: KnownRecords | None = None
    ) -> None:
        """Make these the source's anchors, and `records` the values of its last good
        run's records. Both are replaced, so none are left from an older run, and a
        drift run held before is dropped: it is older than the new last good run."""
        with self.db:
            self._replace(source_id, anchors, records or {})

    def hold(
        self, source_id: str, run_id: str, anchors: list[Anchor], records: KnownRecords
    ) -> None:
        """Keep a drift run's anchors and records, in case a human accepts its drift.
        They replace any held before: only the latest drift run's are kept."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO held_baselines VALUES (?, ?, ?, ?)",
                (
                    source_id,
                    run_id,
                    json.dumps([a.model_dump(mode="json") for a in anchors]),
                    json.dumps({key: dict(v) for key, v in records.items()}),
                ),
            )

    def held(self, source_id: str) -> str | None:
        """The drift run whose anchors and records are held, if any."""
        row = self.db.execute(
            "SELECT run_id FROM held_baselines WHERE source_id = ?", (source_id,)
        ).fetchone()
        return str(row["run_id"]) if row else None

    def accept(self, source_id: str, run_id: str) -> None:
        """Make a held drift run's anchors and records the source's."""
        row = self.db.execute(
            "SELECT anchors, records FROM held_baselines WHERE source_id = ? AND run_id = ?",
            (source_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"{source_id} holds no anchors of run {run_id}")
        anchors = [Anchor.model_validate(a) for a in json.loads(row["anchors"])]
        with self.db:
            self._replace(source_id, anchors, json.loads(row["records"]))

    def _replace(self, source_id: str, anchors: list[Anchor], records: KnownRecords) -> None:
        self.db.execute("DELETE FROM anchors WHERE source_id = ?", (source_id,))
        self.db.executemany(
            "INSERT INTO anchors VALUES (?, ?, ?, ?)",
            [(source_id, rank, a.key, a.model_dump_json()) for rank, a in enumerate(anchors)],
        )
        self.db.execute("DELETE FROM good_records WHERE source_id = ?", (source_id,))
        self.db.executemany(
            "INSERT INTO good_records VALUES (?, ?, ?)",
            [(source_id, key, json.dumps(dict(v))) for key, v in records.items()],
        )
        self.db.execute("DELETE FROM held_baselines WHERE source_id = ?", (source_id,))

    def all(self, source_id: str) -> list[Anchor]:
        rows = self.db.execute(
            "SELECT anchor FROM anchors WHERE source_id = ? ORDER BY rank", (source_id,)
        )
        return [Anchor.model_validate_json(row["anchor"]) for row in rows]

    def records(self, source_id: str) -> dict[str, dict[str, AnchorValue]]:
        """The values of the last good run's valid records, by key: empty if that run
        is older than this table."""
        rows = self.db.execute(
            "SELECT key, fields FROM good_records WHERE source_id = ?", (source_id,)
        )
        return {row["key"]: json.loads(row["fields"]) for row in rows}


def split(anchors: list[Anchor], shown: int) -> tuple[list[Anchor], list[Anchor]]:
    """Anchors a repair provider may see, and the rest, held out: a repair that only
    memorised the shown values fails on the held-out ones."""
    return anchors[:shown], anchors[shown:]
