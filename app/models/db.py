"""SQLite persistence for evaluation reports.

Only one table is needed for the MVP. The report body is stored as JSON so the
evaluation schema can evolve without migrations, while the columns used for
listing and filtering are promoted to real columns.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger
from app.models.schemas import EvaluationReport, ReportSummary

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_id         TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL,
    score             INTEGER NOT NULL,
    title             TEXT NOT NULL,
    platforms         TEXT NOT NULL,
    failed_checks     INTEGER NOT NULL,
    parent_report_id  TEXT,
    source_json       TEXT,
    report_json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports (status);
"""


_LIST_SELECT = (
    "SELECT report_id, created_at, status, score, title, platforms, "
    "failed_checks, parent_report_id FROM reports"
)


class ReportRepository:
    def __init__(self, database_path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms

    # -- infrastructure ----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        logger.info("database ready", extra={"service": "sqlite", "path": str(self.path)})

    def healthcheck(self) -> tuple[bool, str | None]:
        """Return (ok, detail). Used by /ready."""

        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True, None
        except sqlite3.Error as exc:  # pragma: no cover - environment dependent
            return False, str(exc)

    # -- writes ------------------------------------------------------------
    def save(self, report: EvaluationReport, source_json: str | None = None) -> None:
        platforms = [variant.platform for variant in report.variants]
        payload = report.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reports (
                    report_id, created_at, status, score, title,
                    platforms, failed_checks, parent_report_id, source_json,
                    report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.created_at.astimezone(UTC).isoformat(),
                    report.status,
                    report.score,
                    report.source.title,
                    ",".join(platforms),
                    report.failed_checks,
                    report.parent_report_id,
                    source_json,
                    payload,
                ),
            )
        logger.info(
            "report stored",
            extra={"report_id": report.report_id, "status": report.status},
        )

    # -- reads -------------------------------------------------------------
    def get(self, report_id: str) -> EvaluationReport | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        if row is None:
            return None
        return EvaluationReport.model_validate_json(row["report_json"])

    def get_source(self, report_id: str) -> dict[str, object] | None:
        """The original content for a report, used to drive regeneration."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_json FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        if row is None or not row["source_json"]:
            return None
        return json.loads(row["source_json"])

    def list(
        self,
        limit: int = 20,
        status: str | None = None,
        platform: str | None = None,
    ) -> list[ReportSummary]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if platform:
            # platforms is a comma separated column; match on whole tokens.
            clauses.append("(',' || platforms || ',') LIKE ?")
            params.append(f"%,{platform},%")
        # ``clauses`` only ever contains literals defined above; all user input is
        # passed as bound parameters, never interpolated.
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        # Ruff flags any interpolated SQL, so the suppression is explicit here with
        # the reason above: nothing from the request reaches the query text.
        query = f"{_LIST_SELECT} {where} ORDER BY created_at DESC LIMIT ?"  # noqa: S608

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._to_summary(row) for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()
        return int(row["c"])

    @staticmethod
    def _to_summary(row: sqlite3.Row) -> ReportSummary:
        created = row["created_at"]
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return ReportSummary(
            report_id=row["report_id"],
            created_at=parsed,
            status=row["status"],
            score=row["score"],
            title=row["title"],
            platforms=[p for p in row["platforms"].split(",") if p],
            failed_checks=row["failed_checks"],
            parent_report_id=row["parent_report_id"],
        )
