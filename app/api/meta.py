"""Service metadata and probe endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response, status

from app.core.config import Settings
from app.core.metrics import set_app_info
from app.models.platforms import PLATFORM_RULES
from app.models.schemas import (
    DependencyStatus,
    HealthResponse,
    ReadyResponse,
    ServiceInfo,
)

router = APIRouter(tags=["service"])


@router.get("/", response_model=ServiceInfo, summary="Service information")
async def service_info(request: Request) -> ServiceInfo:
    settings: Settings = request.app.state.settings
    return ServiceInfo(
        name=settings.app_name,
        version=settings.app_version,
        description=(
            "Evaluates AI-generated social media variants against the original "
            "content and returns a structured report."
        ),
        documentation="/docs",
        endpoints=[
            "GET /",
            "GET /health",
            "GET /ready",
            "POST /api/v1/evaluate",
            "GET /api/v1/reports",
            "GET /api/v1/reports/{report_id}",
            "POST /api/v1/reports/{report_id}/regenerate",
            "GET /metrics",
        ],
        supported_platforms=list(PLATFORM_RULES),
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(request: Request) -> HealthResponse:
    """Liveness: the process can serve requests.

    This deliberately does not touch external services; a dead Variant Generator
    is not a reason to restart this pod.
    """

    settings: Settings = request.app.state.settings
    started_at = getattr(request.app.state, "started_at", time.time())
    set_app_info(settings.app_version, settings.environment)
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        uptime_seconds=round(time.time() - started_at, 2),
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    responses={503: {"description": "A required dependency is unavailable."}},
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    """Readiness: the service can do useful work.

    The database is required. Integrations are reported but not required, so the
    service starts and stays in rotation while an external service is down.

    The response code matters: a Kubernetes readiness probe looks at the status
    code, not the body, so an unready service must answer 503.
    """

    settings: Settings = request.app.state.settings
    repository = request.app.state.repository

    db_ok, db_detail = repository.healthcheck()
    checks = [
        DependencyStatus(
            name="sqlite",
            required=True,
            status="ok" if db_ok else "unavailable",
            detail=db_detail,
        ),
        DependencyStatus(
            name="variant_generator",
            required=False,
            status="ok",
            detail=(
                f"mode={settings.variant_generator_mode} url={settings.variant_generator_base_url}"
            ),
        ),
        DependencyStatus(
            name="studio",
            required=False,
            status="ok",
            detail=f"mode={settings.studio_mode} url={settings.studio_base_url}",
        ),
        DependencyStatus(
            name="llm",
            required=False,
            status="ok" if settings.llm_active else "degraded",
            detail=(
                f"enabled model={settings.llm_model}"
                if settings.llm_active
                else "disabled; semantic checks use the deterministic heuristic"
            ),
        ),
    ]

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        status="ready" if db_ok else "not_ready",
        checks=checks,
    )
