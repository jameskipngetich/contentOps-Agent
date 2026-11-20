"""Semantic evaluation: heuristic scoring and optional LLM integration."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.semantic import (
    LlmSemanticAssessor,
    SemanticAssessor,
    SemanticEvaluationError,
    heuristic_assessment,
    parse_judgement,
)

TITLE = "Shipping an observable content pipeline"
SOURCE = (
    "Shipping an observable content pipeline. Deterministic validation, duplicate "
    "detection, Prometheus metrics and regeneration requests keep generated social "
    "content accountable."
)

ON_TOPIC = (
    "Observable content pipeline: deterministic validation, duplicate detection, "
    "Prometheus metrics and regeneration requests. #observability"
)
OFF_TOPIC = (
    "Our summer sale starts today. Use code SUN20 for discounts on garden furniture "
    "and outdoor lighting while stocks last."
)


def test_heuristic_scores_on_topic_content_highly():
    assessment = heuristic_assessment(TITLE, SOURCE, ON_TOPIC)
    assert assessment.method == "heuristic"
    assert assessment.score > 0.5
    assert assessment.missing_keywords


def test_heuristic_scores_unrelated_content_low():
    assert heuristic_assessment(TITLE, SOURCE, OFF_TOPIC).score < 0.2


def test_heuristic_handles_sources_without_keywords():
    assessment = heuristic_assessment("the and of", "the and of", "anything")
    assert assessment.score == 1.0
    assert "no comparable keywords" in assessment.summary


def test_heuristic_handles_variants_without_keywords():
    assessment = heuristic_assessment(TITLE, SOURCE, "the and of")
    assert assessment.score == 0.0


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "llm_enabled": True,
        "llm_api_key": "test-key",
        "llm_base_url": "https://llm.example.test/v1",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://llm.example.test/v1", transport=httpx.MockTransport(handler)
    )


async def test_llm_assessor_parses_structured_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"score": 0.91, "consistent": true, '
                                '"issues": [], "summary": "faithful"}\n'
                                "```"
                            )
                        }
                    }
                ]
            },
        )

    assessor = LlmSemanticAssessor(_settings(), client=_client(handler))
    result = await assessor.assess(
        source_title="t", source_text=SOURCE, platform="x", variant_text=ON_TOPIC
    )
    assert result.method == "llm"
    assert result.score == pytest.approx(0.91)


async def test_llm_assessor_raises_on_invalid_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    assessor = LlmSemanticAssessor(_settings(), client=_client(handler))
    with pytest.raises(SemanticEvaluationError):
        await assessor.assess(
            source_title="t", source_text=SOURCE, platform="x", variant_text=ON_TOPIC
        )


async def test_facade_falls_back_to_heuristic_when_llm_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    assessor = SemanticAssessor(_settings(), client=_client(handler))
    result = await assessor.assess(
        source_title="t", source_text=SOURCE, platform="x", variant_text=ON_TOPIC
    )
    assert result.method == "heuristic"
    assert result.fallback_reason is not None
    assert assessor.method == "heuristic"
    assert assessor.fallback_reason is not None


async def test_facade_uses_heuristic_when_llm_disabled():
    assessor = SemanticAssessor(Settings())
    result = await assessor.assess(
        source_title="t", source_text=SOURCE, platform="x", variant_text=ON_TOPIC
    )
    assert result.method == "heuristic"
    assert assessor.fallback_reason is None


async def test_facade_marks_method_llm_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"score": 0.8, "consistent": true,  "issues": [], "summary": "ok"}'
                            )
                        }
                    }
                ]
            },
        )

    assessor = SemanticAssessor(_settings(), client=_client(handler))
    await assessor.assess(source_title="t", source_text=SOURCE, platform="x", variant_text=ON_TOPIC)
    assert assessor.method == "llm"
    assert assessor.fallback_reason is None


def test_parse_judgement_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        parse_judgement('{"score": 2, "consistent": true, "issues": [], "summary": ""}')
