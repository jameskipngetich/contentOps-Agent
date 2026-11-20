"""Adapter for the existing Social Media Studio service.

Verified from the public repository ``jameskipngetich/s0c14l_m3d14_studio``:

    GET  /socials/posts/:name  -> {"name": ..., "content": ...} (stored as JSON)
    POST /socials/makevariants/:name -> JSON *string* containing the generator
                                 response (the controller stringifies it before
                                 sending, so the client sees a double-encoded body)

ContentOps Agent only needs to read the original post. Fetching variants through
the Studio is supported but discouraged: calling the Variant Generator directly
shortens the chain and avoids the double-encoding quirk.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.base import IntegrationError, request_with_retries
from app.models.platforms import all_fields
from app.models.schemas import SourceContent

logger = get_logger(__name__)

SERVICE_NAME = "studio"
OPERATION_FETCH_POST = "fetch_post"
OPERATION_MAKE_VARIANTS = "make_variants"

DEFAULT_POST_URL = "https://example.com/posts/demo"


class StudioPort:
    async def fetch_post(self, name: str) -> SourceContent:
        raise NotImplementedError

    async def request_variants(self, name: str) -> dict[str, str]:
        raise NotImplementedError


class MockStudioAdapter(StudioPort):
    """In-memory Studio with a couple of fixture posts."""

    def __init__(self) -> None:
        self.posts: dict[str, SourceContent] = {
            "demo-post": SourceContent(
                title="Shipping a content pipeline that stays observable",
                content=(
                    "Teams that generate social posts with a model usually hit the "
                    "same problem: nobody can tell which outputs are publishable. "
                    "This post explains how a small evaluation service with "
                    "deterministic checks, optional semantic review and Prometheus "
                    "metrics keeps generated content accountable. We cover "
                    "validation, duplicate detection and regeneration requests, and "
                    "why each failure should be traceable to a specific check."
                ),
                url=DEFAULT_POST_URL,
            )
        }

    async def fetch_post(self, name: str) -> SourceContent:
        post = self.posts.get(name)
        if post is None:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_FETCH_POST,
                f"post {name!r} does not exist in the mock Studio",
                kind="not_found",
                status_code=404,
            )
        return post

    async def request_variants(self, name: str) -> dict[str, str]:
        raise IntegrationError(
            SERVICE_NAME,
            OPERATION_MAKE_VARIANTS,
            "the mock Studio does not generate variants; call the Variant Generator adapter",
            kind="disabled",
        )


class HttpStudioAdapter(StudioPort):
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _build_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=self.settings.studio_base_url,
            timeout=self.settings.studio_timeout_seconds,
        )

    async def fetch_post(self, name: str) -> SourceContent:
        client = self._build_client()
        close_after = self._client is None
        try:
            response = await request_with_retries(
                client,
                "GET",
                f"/socials/posts/{name}",
                service=SERVICE_NAME,
                operation=OPERATION_FETCH_POST,
                max_retries=self.settings.studio_max_retries,
            )
        finally:
            if close_after:
                await client.aclose()

        try:
            body = response.json()
        except ValueError as exc:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_FETCH_POST,
                "response was not valid JSON",
                kind="malformed_response",
            ) from exc

        post = _extract_post(body)
        if post is None:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_FETCH_POST,
                "response did not contain a post with a content field",
                kind="malformed_response",
            )
        return post

    async def request_variants(self, name: str) -> dict[str, str]:
        client = self._build_client()
        close_after = self._client is None
        try:
            response = await request_with_retries(
                client,
                "POST",
                f"/socials/makevariants/{name}",
                service=SERVICE_NAME,
                operation=OPERATION_MAKE_VARIANTS,
                max_retries=self.settings.studio_max_retries,
            )
        finally:
            if close_after:
                await client.aclose()

        try:
            body = response.json()
        except ValueError as exc:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_MAKE_VARIANTS,
                "response was not valid JSON",
                kind="malformed_response",
            ) from exc

        # The Studio stringifies the generator response before sending it.
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise IntegrationError(
                    SERVICE_NAME,
                    OPERATION_MAKE_VARIANTS,
                    "double-encoded response could not be decoded",
                    kind="malformed_response",
                ) from exc

        if not isinstance(body, dict):
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_MAKE_VARIANTS,
                "expected an object of platform variants",
                kind="malformed_response",
            )
        return {
            field: value if isinstance(value, str) else json.dumps(value)
            for field, value in body.items()
            if field in all_fields() and value is not None
        }


def _extract_post(body: Any) -> SourceContent | None:
    """The Studio returns ``{name, content}``; be tolerant of small variations."""

    candidate = body
    if isinstance(body, list) and body:
        candidate = body[0]
    if not isinstance(candidate, dict):
        return None

    content = candidate.get("content") or candidate.get("post")
    if not isinstance(content, str) or not content.strip():
        return None
    # content may itself be the JSON document written by the Studio's controller.
    if content.lstrip().startswith("{"):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
                content = parsed["content"]
        except json.JSONDecodeError:
            pass

    title = candidate.get("name") or candidate.get("title") or "untitled post"
    return SourceContent(
        title=str(title)[:300],
        content=content[:20_000],
        url=None,
    )


def build_studio(settings: Settings) -> StudioPort:
    if settings.studio_mode == "http":
        logger.info(
            "studio adapter configured",
            extra={"service": SERVICE_NAME, "detail": settings.studio_base_url},
        )
        return HttpStudioAdapter(settings)
    return MockStudioAdapter()
