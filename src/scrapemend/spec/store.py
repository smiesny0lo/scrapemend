"""Spec versions: every spec a source has run with, and which one is active.

Specs are never edited in place. A spec file's version is registered the first
time it is run; a repair becomes a new version; rollback makes an older version
active again. A spec file whose content changed without a new version number is
refused, so a version number always means one exact spec.
"""

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from scrapemend.spec.models import SourceSpec

Origin = Literal["file", "repair"]


class SpecConflict(Exception):
    """A spec file reuses a version number that is stored with different content."""


class SpecVersion(BaseModel):
    source_id: str
    version: int
    spec: SourceSpec
    origin: Origin
    note: str
    created_at: AwareDatetime


class SourceState(BaseModel):
    source_id: str
    active_version: int
    quarantine_reason: str | None
    changed_at: AwareDatetime


class SpecStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def register(self, spec: SourceSpec) -> SpecVersion:
        """Record a spec file's version and return the version that should run.

        A version newer than the active one becomes active, since someone edited
        the spec on purpose; an older one doesn't replace a promoted repair.
        """
        stored = self.get(spec.id, spec.version)
        if stored is None:
            self.add(spec, "file", "from the spec file")
            state = self.state(spec.id)
            if state is None or spec.version > state.active_version:
                self.activate(spec.id, spec.version)
        elif stored.spec != spec:
            raise SpecConflict(
                f"{spec.id} v{spec.version} is already stored with different content; "
                f"give the edited spec a new version (next free: {self.next_version(spec.id)})"
            )
        active = self.active(spec.id)
        assert active is not None
        return active

    def add(self, spec: SourceSpec, origin: Origin, note: str) -> SpecVersion:
        version = SpecVersion(
            source_id=spec.id,
            version=spec.version,
            spec=spec,
            origin=origin,
            note=note,
            created_at=datetime.now(UTC),
        )
        with self.db:
            self.db.execute(
                "INSERT INTO spec_versions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    spec.id,
                    spec.version,
                    spec.model_dump_json(),
                    origin,
                    note,
                    version.created_at.isoformat(),
                ),
            )
        return version

    def get(self, source_id: str, version: int) -> SpecVersion | None:
        row = self.db.execute(
            "SELECT * FROM spec_versions WHERE source_id = ? AND version = ?",
            (source_id, version),
        ).fetchone()
        return _version(row) if row else None

    def history(self, source_id: str) -> list[SpecVersion]:
        rows = self.db.execute(
            "SELECT * FROM spec_versions WHERE source_id = ? ORDER BY version", (source_id,)
        )
        return [_version(row) for row in rows]

    def next_version(self, source_id: str) -> int:
        row = self.db.execute(
            "SELECT MAX(version) AS v FROM spec_versions WHERE source_id = ?", (source_id,)
        ).fetchone()
        return (row["v"] or 0) + 1

    def state(self, source_id: str) -> SourceState | None:
        row = self.db.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        return SourceState.model_validate(dict(row)) if row else None

    def active(self, source_id: str) -> SpecVersion | None:
        state = self.state(source_id)
        return self.get(source_id, state.active_version) if state else None

    def activate(self, source_id: str, version: int) -> None:
        """Make a stored version the one that runs. Clears any quarantine."""
        if self.get(source_id, version) is None:
            raise KeyError(f"{source_id} has no version {version}")
        with self.db:
            self.db.execute(
                "INSERT INTO sources VALUES (?, ?, NULL, ?) ON CONFLICT (source_id) DO UPDATE"
                " SET active_version = excluded.active_version, quarantine_reason = NULL,"
                " changed_at = excluded.changed_at",
                (source_id, version, datetime.now(UTC).isoformat()),
            )

    def quarantine(self, source_id: str, reason: str) -> None:
        """Mark a source as needing a human: its spec is broken and no repair passed.
        Activating a version (a promotion, a rollback, a new spec file) ends it."""
        with self.db:
            self.db.execute(
                "UPDATE sources SET quarantine_reason = ?, changed_at = ? WHERE source_id = ?",
                (reason, datetime.now(UTC).isoformat(), source_id),
            )


def _version(row: sqlite3.Row) -> SpecVersion:
    return SpecVersion(
        source_id=row["source_id"],
        version=row["version"],
        spec=SourceSpec.model_validate_json(row["spec"]),
        origin=row["origin"],
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
