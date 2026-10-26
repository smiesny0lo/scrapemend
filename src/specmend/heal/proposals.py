"""Repair proposals: every repair tried, its verification report and what became of it."""

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from specmend.heal.verify import VerificationReport
from specmend.spec.patch import SpecPatch

ProposalStatus = Literal["accepted", "unverified", "rejected", "promoted"]
STATUS: dict[str, ProposalStatus] = {
    "passed": "accepted",
    "not run": "unverified",  # promote --force may promote it
    "failed": "rejected",
}


class Proposal(BaseModel):
    id: int
    source_id: str
    base_version: int  # the spec version the patch applies to
    run_id: str  # the failed run it repairs
    provider: str
    patch: SpecPatch
    report: VerificationReport
    status: ProposalStatus
    promoted_version: int | None
    created_at: AwareDatetime


class ProposalStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def add(
        self,
        source_id: str,
        base_version: int,
        run_id: str,
        provider: str,
        patch: SpecPatch,
        report: VerificationReport,
    ) -> Proposal:
        status = STATUS[report.verdict]
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO proposals (source_id, base_version, run_id, provider, patch, report,"
                " status, promoted_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    source_id,
                    base_version,
                    run_id,
                    provider,
                    patch.model_dump_json(),
                    report.model_dump_json(),
                    status,
                    datetime.now(UTC).isoformat(),
                ),
            )
        proposal = self.get(int(cursor.lastrowid or 0))
        assert proposal is not None
        return proposal

    def get(self, proposal_id: int) -> Proposal | None:
        row = self.db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _proposal(row) if row else None

    def for_source(self, source_id: str) -> list[Proposal]:
        """Newest first."""
        rows = self.db.execute(
            "SELECT * FROM proposals WHERE source_id = ? ORDER BY id DESC", (source_id,)
        )
        return [_proposal(row) for row in rows]

    def reverified(self, proposal_id: int, report: VerificationReport) -> None:
        """Store the verdict of verifying a proposal again (before promoting it)."""
        status = STATUS[report.verdict]
        with self.db:
            self.db.execute(
                "UPDATE proposals SET report = ?, status = ? WHERE id = ?",
                (report.model_dump_json(), status, proposal_id),
            )

    def mark_promoted(self, proposal_id: int, version: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE proposals SET status = 'promoted', promoted_version = ? WHERE id = ?",
                (version, proposal_id),
            )


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        source_id=row["source_id"],
        base_version=row["base_version"],
        run_id=row["run_id"],
        provider=row["provider"],
        patch=SpecPatch.model_validate_json(row["patch"]),
        report=VerificationReport.model_validate_json(row["report"]),
        status=row["status"],
        promoted_version=row["promoted_version"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
