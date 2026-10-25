"""Local state in one SQLite file: runs, baselines, anchors, spec versions, proposals.

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
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (or create) a state database with every table in place."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db
