"""Integration adapters: mock behaviour, HTTP client, retries and error mapping."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.base import IntegrationError, request_with_retries
from app.integrations.studio import HttpStudioAdapter, MockStudioAdapter
from app.integrations.variant_generator import (
    HttpVariantGeneratorAdapter,
    MockVariantGeneratorAdapter,
)
from app.models.platforms import all_fields
from app.models.schemas import SourceContent

SOURCE = SourceContent(
    title="Observability for generated content",
    content=(
        "A pipeline that validates generated social posts with deterministic "
        "checks, duplicate detection and metrics so failures are traceable."
    ),
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "variant_generator_base_url": "http://variants.test",
        "variant_generator_max_retries": 1,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(handler, base_url: str = "http://variants.test") -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Mock generator
# ---------------------------------------------------------------------------
async def test_mock_generator_returns_all_fields():
    variants = await MockVariantGeneratorAdapter().fetch_variants(SOURCE)
    assert set(variants) == set(all_fields())
    assert all(value.strip() for value in variants.values())


async def test_mock_generator_respects_platform_filter():
    variants = await MockVariantGeneratorAdapter().fetch_variants(SOURCE, platforms=["x"])
    assert list(variants) == ["x_post"]


async def test_mock_generator_missing_field_scenario():
    variants = await MockVariantGeneratorAdapter("missing_field").fetch_variants(SOURCE)
    assert "instagram_caption" not in variants


async def test_mock_generator_overflow_scenario():
    variants = await MockVariantGeneratorAdapter("overflow").fetch_variants(SOURCE)
    assert len(variants["x_post"]) > 280


async def test_mock_generator_duplicate_scenario():
    variants = await MockVariantGeneratorAdapter("duplicate").fetch_variants(SOURCE)
    assert len(set(variants.values())) == 1


async def test_mock_generator_error_scenario_raises():
    with pytest.raises(IntegrationError) as excinfo:
        await MockVariantGeneratorAdapter("generator_error").fetch_variants(SOURCE)
    assert excinfo.value.kind == "connection"


# ---------------------------------------------------------------------------
# HTTP generator
# ---------------------------------------------------------------------------
async def test_http_generator_sends_verified_contract():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=dict.fromkeys(all_fields(), "text"))

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    variants = await adapter.fetch_variants(SOURCE)

    assert captured["path"] == "/generate-variants"
    assert captured["body"]["title"] == "Observability for generated content"
    assert captured["body"]["url"] == "none"
    assert captured["body"]["content"] == SOURCE.content
    assert set(variants) == set(all_fields())


async def test_http_generator_retries_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, json={"detail": "model overloaded"})
        return httpx.Response(200, json={"x_post": "ok", "instagram_caption": "ok"})

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    assert (await adapter.fetch_variants(SOURCE))["x_post"] == "ok"
    assert attempts["count"] == 2


async def test_http_generator_does_not_retry_client_errors():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(422, json={"detail": "bad request"})

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_variants(SOURCE)
    assert excinfo.value.kind == "http_error"
    assert excinfo.value.status_code == 422
    assert attempts["count"] == 1


async def test_http_generator_raises_after_retries_exhausted():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, json={"detail": "boom"})

    adapter = HttpVariantGeneratorAdapter(
        _settings(variant_generator_max_retries=2), client=_client(handler)
    )
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_variants(SOURCE)
    assert calls["count"] == 3
    assert excinfo.value.attempts == 3


async def test_http_generator_handles_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_variants(SOURCE)
    assert excinfo.value.kind == "malformed_response"


async def test_http_generator_handles_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    adapter = HttpVariantGeneratorAdapter(
        _settings(variant_generator_max_retries=1), client=_client(handler)
    )
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_variants(SOURCE)
    assert excinfo.value.kind == "timeout"


async def test_http_generator_rejects_object_without_known_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_variants(SOURCE)
    assert excinfo.value.kind == "malformed_response"


async def test_http_generator_coerces_non_string_values():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"x_post": {"nested": "object"}})

    adapter = HttpVariantGeneratorAdapter(_settings(), client=_client(handler))
    variants = await adapter.fetch_variants(SOURCE)
    assert variants["x_post"].startswith("{")


async def test_request_with_retries_success_without_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    response = await request_with_retries(
        _client(handler),
        "GET",
        "/ping",
        service="test",
        operation="ping",
        max_retries=3,
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Studio
# ---------------------------------------------------------------------------
async def test_mock_studio_returns_fixture_post():
    source = await MockStudioAdapter().fetch_post("demo-post")
    assert source.title.startswith("Shipping a content pipeline")
    assert source.url == "https://example.com/posts/demo"


async def test_mock_studio_unknown_post_is_not_found():
    with pytest.raises(IntegrationError) as excinfo:
        await MockStudioAdapter().fetch_post("does-not-exist")
    assert excinfo.value.kind == "not_found"


async def test_http_studio_fetches_and_unwraps_json_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/socials/posts/demo"
        return httpx.Response(
            200,
            json={
                "name": "demo",
                "content": '{"name": "demo", "content": "the real article body"}',
            },
        )

    adapter = HttpStudioAdapter(
        _settings(studio_base_url="http://studio.test"),
        client=_client(handler, base_url="http://studio.test"),
    )
    source = await adapter.fetch_post("demo")
    assert source.content == "the real article body"


async def test_http_studio_decodes_double_encoded_variants():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json='{"x_post": "hello world"}')

    adapter = HttpStudioAdapter(
        _settings(studio_base_url="http://studio.test"),
        client=_client(handler, base_url="http://studio.test"),
    )
    variants = await adapter.request_variants("demo")
    assert variants == {"x_post": "hello world"}


async def test_http_studio_rejects_response_without_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "demo"})

    adapter = HttpStudioAdapter(
        _settings(studio_base_url="http://studio.test"),
        client=_client(handler, base_url="http://studio.test"),
    )
    with pytest.raises(IntegrationError) as excinfo:
        await adapter.fetch_post("demo")
    assert excinfo.value.kind == "malformed_response"
