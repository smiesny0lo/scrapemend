"""Spec versions: every spec a source has run with, and which one is active.

Specs are never edited in place. A spec file's version is registered the first
time it is run; a repair becomes a new version; rollback makes an older version
active again. A spec file whose content changed without a new version number is
refused, so a version number always means one exact spec.

A repair promoted automatically is provisional until a human confirms it: its runs
don't become the last good run (see specmend.runner.confirm).
"""

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from specmend.spec.models import SourceSpec

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
    provisional: bool = False  # promoted automatically, and not confirmed by a human yet


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

    def add(
        self, spec: SourceSpec, origin: Origin, note: str, *, provisional: bool = False
    ) -> SpecVersion:
        """Store a version. `provisional`: heal promoted it automatically, so no human
        has looked at it yet."""
        version = SpecVersion(
            source_id=spec.id,
            version=spec.version,
            spec=spec,
            origin=origin,
            note=note,
            created_at=datetime.now(UTC),
            provisional=provisional,
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
            if provisional:
                self.db.execute(
                    "INSERT INTO automatic_promotions VALUES (?, ?, NULL)", (spec.id, spec.version)
                )
        return version

    def get(self, source_id: str, version: int) -> SpecVersion | None:
        row = self.db.execute(
            f"{_SELECT} WHERE v.source_id = ? AND v.version = ?", (source_id, version)
        ).fetchone()
        return _version(row) if row else None

    def history(self, source_id: str) -> list[SpecVersion]:
        rows = self.db.execute(f"{_SELECT} WHERE v.source_id = ? ORDER BY v.version", (source_id,))
        return [_version(row) for row in rows]

    def is_provisional(self, source_id: str, version: int) -> bool:
        """Promoted automatically, and not confirmed yet."""
        stored = self.get(source_id, version)
        return stored is not None and stored.provisional

    def promoted_automatically(self, source_id: str, version: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM automatic_promotions WHERE source_id = ? AND version = ?",
            (source_id, version),
        ).fetchone()
        return row is not None

    def confirm(self, source_id: str, version: int) -> None:
        """A human checked a version promoted automatically: it is no longer provisional."""
        with self.db:
            self.db.execute(
                "UPDATE automatic_promotions SET confirmed_at = ?"
                " WHERE source_id = ? AND version = ?",
                (datetime.now(UTC).isoformat(), source_id, version),
            )

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


_SELECT = (
    "SELECT v.*, a.version IS NOT NULL AND a.confirmed_at IS NULL AS provisional"
    " FROM spec_versions v LEFT JOIN automatic_promotions a"
    " ON a.source_id = v.source_id AND a.version = v.version"
)


def _version(row: sqlite3.Row) -> SpecVersion:
    return SpecVersion(
        source_id=row["source_id"],
        version=row["version"],
        spec=SourceSpec.model_validate_json(row["spec"]),
        origin=row["origin"],
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
        provisional=bool(row["provisional"]),
    )
