"""Local state in one SQLite file: runs, baselines, anchors and the last good run's
records (and a drift run's, until it is accepted), spec versions, proposals.

Every table is created on first use, so an older database simply gains the tables
a newer version needs. Stores share one connection; each write commits at once.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    mode         TEXT NOT NULL,      -- run | check
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    records      INTEGER NOT NULL,
    report       TEXT NOT NULL       -- the RunReport as JSON
);
CREATE INDEX IF NOT EXISTS runs_by_source ON runs (source_id, started_at);

CREATE TABLE IF NOT EXISTS baselines (
    source_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- listing | detail
    fingerprint TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    PRIMARY KEY (source_id, kind)
);

CREATE TABLE IF NOT EXISTS anchors (
    source_id TEXT NOT NULL,
    rank      INTEGER NOT NULL,      -- stable order: by hash of the key
    key       TEXT NOT NULL,
    anchor    TEXT NOT NULL,         -- the Anchor as JSON
    PRIMARY KEY (source_id, key)
);

CREATE TABLE IF NOT EXISTS good_records (
    source_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    fields    TEXT NOT NULL,         -- field -> value as anchors store it, as JSON
    PRIMARY KEY (source_id, key)
);

CREATE TABLE IF NOT EXISTS held_baselines (
    source_id TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL,         -- the latest drift run, until accepted or an ok run
    anchors   TEXT NOT NULL,         -- its anchors, as a JSON list
    records   TEXT NOT NULL          -- its valid records, key -> field -> value, as JSON
);

CREATE TABLE IF NOT EXISTS spec_versions (
    source_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    spec       TEXT NOT NULL,        -- the SourceSpec as JSON
    origin     TEXT NOT NULL,        -- file | repair
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, version)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id         TEXT PRIMARY KEY,
    active_version    INTEGER NOT NULL,
    quarantine_reason TEXT,          -- set while a human needs to look at the source
    changed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        TEXT NOT NULL,
    base_version     INTEGER NOT NULL,
    run_id           TEXT NOT NULL,  -- the failed run the repair is for
    provider         TEXT NOT NULL,
    patch            TEXT NOT NULL,  -- the SpecPatch as JSON
    report           TEXT NOT NULL,  -- the VerificationReport as JSON
    status           TEXT NOT NULL,  -- accepted | unverified | rejected | promoted
    promoted_version INTEGER,
    created_at       TEXT NOT NULL
);
"""


class State:
    """All local state of one database file."""

    def __init__(self, path: str | Path) -> None:
        # Imported here: those modules depend on much of the package, state.py on none.
        from scrapemend.heal.anchors import AnchorStore
        from scrapemend.heal.proposals import ProposalStore
        from scrapemend.runs import RunStore
        from scrapemend.spec.store import SpecStore

        self.path = path
        self.db = connect(path)
        self.runs = RunStore(self.db)
        self.anchors = AnchorStore(self.db)
        self.specs = SpecStore(self.db)
        self.proposals = ProposalStore(self.db)

    def close(self) -> None:
        self.db.close()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (or create) a state database with every table in place."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db
