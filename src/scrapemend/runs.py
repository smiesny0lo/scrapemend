"""Run metadata: what each run found, kept in the SQLite runs table."""

import sqlite3
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from scrapemend.detect.classify import Diagnosis, Failure, Status
from scrapemend.ids import new_run_id
from scrapemend.pipeline import RunSummary

__all__ = ["RunMode", "RunReport", "RunStore", "new_run_id"]

RunMode = Literal["run", "check"]
GOOD: tuple[Status, ...] = ("ok", "drift")  # runs whose data can be trusted


class RunReport(BaseModel):
    run_id: str
    source_id: str
    spec_version: int
    mode: RunMode
    status: Status
    started_at: AwareDatetime
    finished_at: AwareDatetime
    complete: bool  # crawled to the end, without --limit or sampling
    summary: RunSummary
    failures: list[Failure] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    structure: dict[str, float] = Field(default_factory=dict)  # kind -> similarity to baseline
    diagnosis: Diagnosis | None = None
    error: str | None = None  # why the crawl stopped early, if it did

    @property
    def good(self) -> bool:
        return self.status in GOOD


class RunStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def save(self, report: RunReport) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.run_id,
                report.source_id,
                report.spec_version,
                report.mode,
                report.status,
                report.started_at.isoformat(),
                report.finished_at.isoformat(),
                report.summary.valid_records,
                report.model_dump_json(),
            ),
        )
        self.db.commit()

    def get(self, run_id: str) -> RunReport | None:
        row = self.db.execute("SELECT report FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunReport.model_validate_json(row["report"]) if row else None

    def recent(self, source_id: str | None = None, limit: int = 20) -> list[RunReport]:
        """Newest first."""
        if source_id is None:
            rows = self.db.execute(
                "SELECT report FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.execute(
                "SELECT report FROM runs WHERE source_id = ? ORDER BY started_at DESC LIMIT ?",
                (source_id, limit),
            )
        return [RunReport.model_validate_json(row["report"]) for row in rows]

    def last_good(self, source_id: str) -> RunReport | None:
        """The latest complete run whose data can be trusted: the baseline to compare to."""
        rows = self.db.execute(
            "SELECT report FROM runs WHERE source_id = ? AND mode = 'run' AND status IN (?, ?)"
            " ORDER BY started_at DESC",
            (source_id, *GOOD),
        )
        for row in rows:
            report = RunReport.model_validate_json(row["report"])
            if report.complete:
                return report
        return None

    def latest(self, source_id: str) -> RunReport | None:
        reports = self.recent(source_id, limit=1)
        return reports[0] if reports else None

    def baseline(self, source_id: str, kind: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM baselines WHERE source_id = ? AND kind = ?",
            (source_id, kind),
        ).fetchone()
        return str(row["fingerprint"]) if row else None

    def set_baseline(self, source_id: str, kind: str, fingerprint: str, run_id: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?)",
            (source_id, kind, fingerprint, run_id),
        )
        self.db.commit()
