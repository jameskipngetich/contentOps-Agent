"""SQLite report repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.models.db import ReportRepository
from app.models.schemas import (
    EvaluationReport,
    EvaluatorInfo,
    SourceSummary,
    VariantEvaluation,
)


def _report(report_id: str, status: str = "passed", score: int = 100) -> EvaluationReport:
    return EvaluationReport(
        report_id=report_id,
        created_at=datetime.now(UTC),
        status=status,  # type: ignore[arg-type]
        score=score,
        source=SourceSummary(title="A title", url=None, characters=10),
        variant_source="inline",
        variants=[
            VariantEvaluation(
                platform="x",
                field="x_post",
                status=status,  # type: ignore[arg-type]
                score=score,
                characters=20,
                hashtags=1,
                checks=[],
                excerpt="excerpt",
            )
        ],
        duplicates=[],
        failed_checks=0 if status == "passed" else 2,
        regeneration_requests=[],
        evaluator=EvaluatorInfo(semantic="heuristic", llm_enabled=False),
        duration_ms=1.0,
        trace=[],
    )


def test_save_and_get_roundtrip(tmp_path: Path):
    repository = ReportRepository(tmp_path / "reports.db")
    repository.initialise()
    report = _report("abc123")
    repository.save(report, source_json='{"title": "A title", "content": "body"}')

    loaded = repository.get("abc123")
    assert loaded is not None
    assert loaded.report_id == report.report_id
    assert loaded.source.title == "A title"
    assert repository.get_source("abc123")["content"] == "body"


def test_missing_report_returns_none(tmp_path: Path):
    repository = ReportRepository(tmp_path / "reports.db")
    repository.initialise()
    assert repository.get("nope") is None
    assert repository.get_source("nope") is None


def test_list_orders_newest_first_and_filters(tmp_path: Path):
    repository = ReportRepository(tmp_path / "reports.db")
    repository.initialise()
    repository.save(_report("aaa1", status="passed"))
    repository.save(_report("bbb2", status="failed", score=20))

    all_reports = repository.list()
    assert [r.report_id for r in all_reports] == ["bbb2", "aaa1"]
    assert [r.report_id for r in repository.list(status="failed")] == ["bbb2"]
    assert [r.report_id for r in repository.list(platform="x")] == ["bbb2", "aaa1"]
    assert repository.list(platform="instagram") == []
    assert repository.count() == 2


def test_list_respects_limit(tmp_path: Path):
    repository = ReportRepository(tmp_path / "reports.db")
    repository.initialise()
    for index in range(3):
        repository.save(_report(f"id-{index}"))
    assert len(repository.list(limit=2)) == 2


def test_initialise_is_idempotent(tmp_path: Path):
    path = tmp_path / "reports.db"
    ReportRepository(path).initialise()
    repository = ReportRepository(path)
    repository.initialise()
    assert repository.count() == 0


def test_healthcheck_reports_ok(tmp_path: Path):
    repository = ReportRepository(tmp_path / "reports.db")
    repository.initialise()
    ok, detail = repository.healthcheck()
    assert ok is True
    assert detail is None
