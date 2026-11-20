"""Application entry point.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

The factory pattern keeps tests able to build an app with different settings
without touching process-wide state.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.evaluator import EvaluationAgent
from app.api import evaluations, meta, metrics
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.metrics import REPORTS_STORED, set_app_info
from app.core.middleware import ObservabilityMiddleware
from app.integrations.studio import build_studio
from app.integrations.variant_generator import build_variant_generator
from app.models.db import ReportRepository

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        repository = ReportRepository(
            settings.database_path, busy_timeout_ms=settings.database_busy_timeout_ms
        )
        repository.initialise()
        REPORTS_STORED.set(repository.count())
        agent = EvaluationAgent(
            settings=settings,
            repository=repository,
            variant_generator=build_variant_generator(settings),
            studio=build_studio(settings),
        )

        app.state.settings = settings
        app.state.repository = repository
        app.state.agent = agent
        app.state.started_at = time.time()

        set_app_info(settings.app_version, settings.environment)
        logger.info(
            "service started",
            extra={
                "service": settings.app_name,
                "detail": (
                    f"version={settings.app_version} env={settings.environment} "
                    f"variant_generator={settings.variant_generator_mode} "
                    f"studio={settings.studio_mode} llm={'on' if settings.llm_active else 'off'}"
                ),
            },
        )
        try:
            yield
        finally:
            logger.info("service stopping", extra={"service": settings.app_name})

    app = FastAPI(
        title="ContentOps Agent",
        version=settings.app_version,
        description=(
            "Evaluates AI-generated social media variants against the original "
            "content: deterministic platform checks, optional semantic review, "
            "structured reports and regeneration requests."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(ObservabilityMiddleware)
    register_exception_handlers(app)

    app.include_router(meta.router)
    app.include_router(evaluations.router)
    app.include_router(metrics.router)

    return app


app = create_app()
