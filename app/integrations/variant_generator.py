"""Adapters for the existing Variant Generator service.

Verified contract (from the public repository ``jameskipngetich/variant_generator``,
``agent2.py``):

    POST /generate-variants
    request:  {"title": str, "content": str, "url": str | "none" | absent}
    response: {"x_post": str, "instagram_caption": str,
               "discord_message": str, "telegram_post": str}

The service returns HTTP 500 with ``{"detail": ...}`` when generation fails, so a
non-2xx response is treated as an upstream failure. The upstream service has no
authentication today; if that changes, add the header here and read it from an
environment variable.

Two implementations are provided: a deterministic mock (default) and a real HTTP
client (``VARIANT_GENERATOR_MODE=http``).
"""

from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.base import IntegrationError, request_with_retries
from app.models.platforms import PLATFORM_RULES, all_fields
from app.models.schemas import SourceContent
from app.services.validators import keywords

logger = get_logger(__name__)

SERVICE_NAME = "variant_generator"
OPERATION_GENERATE = "generate_variants"

FIELD_TO_PLATFORM = {rule.field: platform for platform, rule in PLATFORM_RULES.items()}


class VariantGeneratorPort:
    """Interface the evaluation pipeline depends on."""

    async def fetch_variants(
        self, source: SourceContent, platforms: list[str] | None = None
    ) -> dict[str, str]:
        raise NotImplementedError

    async def request_regeneration(
        self, source: SourceContent, *, platforms: list[str], reason: str
    ) -> dict[str, str]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------
class MockVariantGeneratorAdapter(VariantGeneratorPort):
    """Deterministic stand-in for the generator.

    ``MOCK_VARIANT_SCENARIO`` makes realistic upstream problems reproducible
    without any external service:

    * ``clean``          -- valid, distinct variants
    * ``overflow``       -- the X post exceeds 280 characters
    * ``missing_field``  -- ``instagram_caption`` is absent
    * ``duplicate``      -- every platform receives the same copy
    * ``generator_error``-- the adapter always raises, like a dead upstream
    """

    def __init__(self, scenario: str = "clean") -> None:
        self.scenario = scenario

    def _clean_payload(self, source: SourceContent, fields: list[str]) -> dict[str, str]:
        terms = keywords(source.content, limit=6) or ["news", "update", "story"]
        topic = ", ".join(terms[:3])
        hashtags = " ".join(f"#{term.replace('-', '')}" for term in terms[:5])

        payload = {
            "x_post": (
                f"{source.title[:110]} — {topic}. Full piece in the thread. "
                f"#{terms[0].replace('-', '')}"
            ),
            "instagram_caption": (
                f"{source.title}\n\n"
                f"The short version of this update: it touches on {', '.join(terms[:4])}.\n\n"
                "- Why it matters: it changes how the team works day to day.\n"
                "- What to do next: read the full article and share it.\n\n"
                f"{hashtags} #contentops"
            ),
            "discord_message": (
                f"**{source.title}**\n"
                f"> New post is live. Key themes: {', '.join(terms[:4])}.\n"
                "Read it and drop your questions in the thread."
            ),
            "telegram_post": (
                f"{source.title}\n\n"
                f"- Themes: {', '.join(terms[:4])}\n"
                "- Read the full post on the blog\n"
                "- Reply with feedback if anything is unclear\n\n"
                "https://example.com/read-more"
            ),
        }
        return {field: payload[field] for field in fields}

    def _apply_scenario(
        self, payload: dict[str, str], source: SourceContent, fields: list[str]
    ) -> dict[str, str]:
        if self.scenario == "overflow":
            payload["x_post"] = (f"{source.title} " + "This is a long generated post. " * 20)[:600]
        elif self.scenario == "missing_field":
            payload.pop("instagram_caption", None)
        elif self.scenario == "duplicate":
            shared = (
                f"{source.title}: a single generated message reused for every "
                "platform, which is exactly what cross-posting should avoid."
            )
            payload = dict.fromkeys(fields, shared)
        return payload

    async def fetch_variants(
        self, source: SourceContent, platforms: list[str] | None = None
    ) -> dict[str, str]:
        selected = platforms or list(PLATFORM_RULES)
        fields = [PLATFORM_RULES[platform].field for platform in selected]
        if self.scenario == "generator_error":
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_GENERATE,
                "mock upstream is configured to fail (MOCK_VARIANT_SCENARIO=generator_error)",
                kind="connection",
                attempts=3,
            )
        payload = self._apply_scenario(self._clean_payload(source, fields), source, fields)
        logger.info(
            "mock variants produced",
            extra={"service": SERVICE_NAME, "detail": f"scenario={self.scenario}"},
        )
        return payload

    async def request_regeneration(
        self, source: SourceContent, *, platforms: list[str], reason: str
    ) -> dict[str, str]:
        logger.info(
            "mock regeneration requested",
            extra={
                "service": SERVICE_NAME,
                "platform": ",".join(platforms),
                "detail": reason[:200],
            },
        )
        return await self.fetch_variants(source, platforms=platforms)


# ---------------------------------------------------------------------------
# Real HTTP adapter
# ---------------------------------------------------------------------------
class HttpVariantGeneratorAdapter(VariantGeneratorPort):
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _build_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=self.settings.variant_generator_base_url,
            timeout=self.settings.variant_generator_timeout_seconds,
        )

    async def _post(self, payload_source: tuple[SourceContent, str]) -> dict[str, str]:
        source, content = payload_source
        client = self._build_client()
        close_after = self._client is None
        try:
            response = await request_with_retries(
                client,
                "POST",
                "/generate-variants",
                service=SERVICE_NAME,
                operation=OPERATION_GENERATE,
                max_retries=self.settings.variant_generator_max_retries,
                json={"title": source.title, "content": content, "url": source.url or "none"},
            )
        finally:
            if close_after:
                await client.aclose()

        try:
            body = response.json()
        except ValueError as exc:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_GENERATE,
                "response was not valid JSON",
                kind="malformed_response",
            ) from exc

        if not isinstance(body, dict):
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_GENERATE,
                f"expected a JSON object, got {type(body).__name__}",
                kind="malformed_response",
            )

        variants: dict[str, str] = {}
        for field in all_fields():
            value = body.get(field)
            if value is None:
                continue
            # Non-string values are coerced to JSON so the malformed_content
            # check flags them instead of crashing the evaluator.
            variants[field] = value if isinstance(value, str) else json.dumps(value)
        if not variants:
            raise IntegrationError(
                SERVICE_NAME,
                OPERATION_GENERATE,
                "response contained none of the expected platform fields",
                kind="malformed_response",
            )
        return variants

    async def fetch_variants(
        self, source: SourceContent, platforms: list[str] | None = None
    ) -> dict[str, str]:
        variants = await self._post((source, source.content))
        if platforms is None:
            return variants
        allowed = {PLATFORM_RULES[platform].field for platform in platforms}
        return {field: value for field, value in variants.items() if field in allowed}

    async def request_regeneration(
        self, source: SourceContent, *, platforms: list[str], reason: str
    ) -> dict[str, str]:
        # The upstream service has no regeneration endpoint. Reusing the
        # generation endpoint with an explicit instruction is the contract we can
        # verify today; see docs/integration.md.
        instruction = (
            "Rewrite the content for these platforms only: "
            f"{', '.join(platforms)}.\nReason for regeneration: {reason}\n\n"
            "Original content:\n"
        )
        variants = await self._post((source, instruction + source.content))
        allowed = {PLATFORM_RULES[platform].field for platform in platforms}
        return {field: value for field, value in variants.items() if field in allowed}


def build_variant_generator(settings: Settings) -> VariantGeneratorPort:
    if settings.variant_generator_mode == "http":
        logger.info(
            "variant generator adapter configured",
            extra={"service": SERVICE_NAME, "detail": settings.variant_generator_base_url},
        )
        return HttpVariantGeneratorAdapter(settings)
    return MockVariantGeneratorAdapter(settings.mock_variant_scenario)
