"""Evaluation and report endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from app.agents.evaluator import EvaluationAgent
from app.api.deps import get_agent, get_repository, get_settings_from_app
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.db import ReportRepository
from app.models.schemas import (
    EvaluationReport,
    EvaluationRequest,
    RegenerateRequest,
    ReportListResponse,
)

router = APIRouter(prefix="/api/v1", tags=["evaluation"])

ReportStatusQuery = Literal["passed", "warnings", "failed"]


@router.post(
    "/evaluate",
    response_model=EvaluationReport,
    status_code=status.HTTP_201_CREATED,
    summary="Evaluate generated platform variants",
    responses={
        201: {"description": "Report created and stored."},
        404: {"description": "The referenced Studio post does not exist."},
        422: {"description": "The request body failed validation."},
        502: {"description": "An external content service failed."},
    },
)
async def evaluate(
    payload: EvaluationRequest,
    request: Request,
    response: Response,
    agent: EvaluationAgent = Depends(get_agent),
) -> EvaluationReport:
    """Validate and score platform variations against the original content.

    A report is created even when variants fail their checks: the report is the
    record of the failure. Use ``regeneration_requests`` to ask the generator for
    new content, or ``POST /api/v1/reports/{id}/regenerate`` to have this service
    do it.
    """

    report = await agent.evaluate(payload)
    response.headers["Location"] = f"/api/v1/reports/{report.report_id}"
    return report


@router.get(
    "/reports",
    response_model=ReportListResponse,
    summary="List stored evaluation reports",
)
async def list_reports(
    settings: Settings = Depends(get_settings_from_app),
    repository: ReportRepository = Depends(get_repository),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: ReportStatusQuery | None = Query(default=None, alias="status"),
    platform: str | None = Query(default=None, description="Filter by platform name"),
) -> ReportListResponse:
    effective_limit = min(limit, settings.max_reports_returned)
    items = repository.list(limit=effective_limit, status=status_filter, platform=platform)
    return ReportListResponse(items=items, count=len(items), limit=effective_limit)


@router.get(
    "/reports/{report_id}",
    response_model=EvaluationReport,
    summary="Fetch a single evaluation report",
    responses={404: {"description": "No report with that id."}},
)
async def get_report(
    report_id: str = Path(min_length=4, max_length=64),
    repository: ReportRepository = Depends(get_repository),
) -> EvaluationReport:
    report = repository.get(report_id)
    if report is None:
        raise NotFoundError(f"report {report_id!r} not found")
    return report


@router.post(
    "/reports/{report_id}/regenerate",
    response_model=EvaluationReport,
    status_code=status.HTTP_201_CREATED,
    summary="Regenerate and re-evaluate failing variants",
    responses={
        201: {"description": "Regenerated variants evaluated in a new report."},
        404: {"description": "No report with that id."},
        409: {"description": "Nothing to regenerate, or the source is missing."},
        502: {"description": "The Variant Generator failed."},
    },
)
async def regenerate_report(
    request: Request,
    response: Response,
    report_id: str = Path(min_length=4, max_length=64),
    payload: RegenerateRequest | None = None,
    agent: EvaluationAgent = Depends(get_agent),
) -> EvaluationReport:
    """Ask the generator for new variants for the platforms that failed, then
    evaluate them again. The resulting report links back with
    ``parent_report_id``.
    """

    body = payload or RegenerateRequest()
    report = await agent.regenerate(report_id, platforms=body.platforms)
    response.headers["Location"] = f"/api/v1/reports/{report.report_id}"
    return report
