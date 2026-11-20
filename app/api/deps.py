"""FastAPI dependencies.

Everything stateful lives on ``app.state`` and is wired once in the lifespan, so
tests can override it and routes stay small.
"""

from __future__ import annotations

from fastapi import Request

from app.agents.evaluator import EvaluationAgent
from app.core.config import Settings
from app.models.db import ReportRepository


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(request: Request) -> ReportRepository:
    return request.app.state.repository


def get_agent(request: Request) -> EvaluationAgent:
    return request.app.state.agent
