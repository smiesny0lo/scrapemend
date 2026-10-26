"""Run metadata: what each run found, kept in the SQLite runs table."""

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from scrapemend.detect.classify import Diagnosis, Failure, Status
from scrapemend.fetch import Requests
from scrapemend.ids import new_run_id
from scrapemend.pipeline import RunSummary

__all__ = ["RunMode", "RunReport", "RunStore", "new_run_id"]

RunMode = Literal["run", "check"]
GOOD: tuple[Status, ...] = ("ok", "drift")  # runs that didn't fail (drift alerts, unrepaired)


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
    # A human made this run the baseline: a drift run they accepted, or a provisional
    # version's run, when they confirmed the version.
    accepted_at: AwareDatetime | None = None
    provisional: bool = False  # its version was promoted automatically, not confirmed yet
    pruned: list[str] = Field(default_factory=list)  # older runs whose snapshots it deleted
    requests: Requests | None = None  # what this run sent, if its fetcher counts

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
        """The baseline later runs and repairs are compared with: the latest complete
        run that was ok, or whose drift a human accepted (see runner.accept_drift).
        Until then a drift run is not: the runs after it are compared with the same
        last good run as it was, so they keep drifting, and alerting. Neither is an ok
        run of a provisional version, until a human confirms it (runner.confirm)."""
        rows = self.db.execute(
            "SELECT report FROM runs WHERE source_id = ? AND mode = 'run' AND ("
            "(status = 'ok' AND (COALESCE(json_extract(report, '$.provisional'), 0) = 0"
            " OR json_extract(report, '$.accepted_at') IS NOT NULL))"
            " OR (status = 'drift' AND json_extract(report, '$.accepted_at') IS NOT NULL))"
            " ORDER BY started_at DESC",
            (source_id,),
        )
        for row in rows:
            report = RunReport.model_validate_json(row["report"])
            if report.complete:
                return report
        return None

    def accept(self, run_id: str, at: datetime) -> RunReport:
        """Mark a drift run, or a provisional version's run, as accepted by a human,
        which makes it the last good run."""
        report = self.get(run_id)
        if report is None:
            raise KeyError(run_id)
        accepted = report.model_copy(update={"accepted_at": at})
        self.save(accepted)
        return accepted

    def last_failed(self, source_id: str) -> RunReport | None:
        """The latest run, of any mode, that failed: what heal repairs by default."""
        row = self.db.execute(
            "SELECT report FROM runs WHERE source_id = ? AND status NOT IN (?, ?)"
            " ORDER BY started_at DESC LIMIT 1",
            (source_id, *GOOD),
        ).fetchone()
        return RunReport.model_validate_json(row["report"]) if row else None

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
