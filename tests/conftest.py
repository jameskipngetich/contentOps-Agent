"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

SOURCE = {
    "title": "Shipping an observable content pipeline",
    "content": (
        "Teams that generate social posts with a model hit the same problem: "
        "nobody can tell which outputs are publishable. This post explains how a "
        "small evaluation service with deterministic checks, optional semantic "
        "review and Prometheus metrics keeps generated content accountable. It "
        "covers validation, duplicate detection and regeneration requests."
    ),
    "url": "https://example.com/shipping-an-observable-content-pipeline",
}

GOOD_VARIANTS = {
    "x_post": (
        "Shipping an observable content pipeline: deterministic checks, semantic "
        "review and metrics. #observability"
    ),
    "instagram_caption": (
        "Shipping an observable content pipeline\n\n"
        "The short version: generated social posts need validation, duplicate "
        "detection and metrics.\n\n"
        "- Why it matters: publishable content needs accountability\n"
        "- What next: read the full post and share it with your team\n\n"
        "#contentops #observability #validation #socialmedia #metrics"
    ),
    "discord_message": (
        "**Shipping an observable content pipeline**\n"
        "> Deterministic checks, semantic review and Prometheus metrics.\n"
        "Questions in the thread."
    ),
    "telegram_post": (
        "Shipping an observable content pipeline\n\n"
        "- Deterministic checks catch missing fields and oversized posts\n"
        "- Duplicate detection finds copy-pasted variants\n"
        "- Metrics make failures visible\n\n"
        "https://example.com/read-more"
    ),
}


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {"database_path": tmp_path / "contentops.db"}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client_factory(tmp_path: Path) -> Iterator[Callable[..., TestClient]]:
    stack = ExitStack()

    def make(**overrides: object) -> TestClient:
        app = create_app(make_settings(tmp_path, **overrides))
        return stack.enter_context(TestClient(app))

    yield make
    stack.close()


@pytest.fixture
def client(client_factory: Callable[..., TestClient]) -> TestClient:
    return client_factory()


@pytest.fixture
def evaluate_payload() -> dict[str, object]:
    return {"source": dict(SOURCE), "variants": dict(GOOD_VARIANTS)}
