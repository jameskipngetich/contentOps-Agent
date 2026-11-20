"""Semantic consistency checks.

Two implementations:

* ``heuristic`` — keyword coverage plus token overlap. Deterministic, offline and
  the default. Good enough to catch a variant that drifted onto another topic.
* ``llm`` — an OpenAI-compatible chat completion that returns a small JSON
  judgement. Enabled with ``LLM_ENABLED=true`` and ``LLM_API_KEY``.

If the LLM call fails for any reason the service falls back to the heuristic and
reports ``fallback_reason`` instead of failing the evaluation request.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import INTEGRATION_LATENCY, INTEGRATION_REQUESTS_TOTAL
from app.services.validators import keywords

logger = get_logger(__name__)

#: Coverage is measured against this many of the source's best keywords. Short
#: platform posts cannot reuse the full vocabulary of a long article.
_COVERAGE_DENOMINATOR = 10
#: The source keyword list is longer than the variant list: variants reuse only a
#: subset of an article's vocabulary.
_SOURCE_KEYWORD_LIMIT = 40


@dataclass
class SemanticAssessment:
    score: float
    method: str
    summary: str
    missing_keywords: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    matched_keywords: int = 0
    containment: float = 0.0
    coverage: float = 0.0


class SemanticEvaluationError(RuntimeError):
    pass


class LlmJudgement(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    consistent: bool
    issues: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------
def heuristic_assessment(
    source_title: str, source_text: str, variant_text: str
) -> SemanticAssessment:
    """Score topical faithfulness with three cheap, explainable signals.

    * title coverage -- how much of the article title's vocabulary the post uses.
      This is the strongest signal that both texts are about the same subject.
    * containment -- the share of the post's own key terms that also appear in
      the source. This catches copy that drifted onto another topic.
    * coverage -- how much of the source's vocabulary the post reuses, measured
      against a capped denominator because a 280 character post cannot repeat a
      full article.

    The heuristic judges the topic, not the facts. It is deliberately tolerant,
    and the numbers behind the score are reported in the check details.
    """

    source_keywords = keywords(source_text, limit=_SOURCE_KEYWORD_LIMIT)
    variant_keywords = keywords(variant_text)
    title_keywords = keywords(source_title)

    if not source_keywords:
        return SemanticAssessment(
            score=1.0,
            method="heuristic",
            summary="the source content had no comparable keywords",
        )
    if not variant_keywords:
        return SemanticAssessment(
            score=0.0,
            method="heuristic",
            summary="the variant had no comparable keywords",
        )

    source_set = set(source_keywords)
    variant_set = set(variant_keywords)
    matched = source_set & variant_set
    missing = [word for word in source_keywords if word not in variant_set]

    title_coverage = (
        len(set(title_keywords) & variant_set) / len(set(title_keywords)) if title_keywords else 0.0
    )
    containment = len(matched) / len(variant_set)
    coverage = min(1.0, len(matched) / _COVERAGE_DENOMINATOR)
    score = round(min(1.0, 0.4 * title_coverage + 0.35 * containment + 0.25 * coverage), 4)

    summary = (
        f"{len(matched)} shared key terms; title {title_coverage:.2f}, "
        f"containment {containment:.2f}, coverage {coverage:.2f}"
    )
    return SemanticAssessment(
        score=score,
        method="heuristic",
        summary=summary,
        missing_keywords=missing[:5],
        issues=[] if score >= 0.35 else [f"low keyword overlap, missing: {', '.join(missing[:5])}"],
        matched_keywords=len(matched),
        containment=round(containment, 4),
        coverage=round(coverage, 4),
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You review social media posts that were derived from an article. "
    "Decide whether the post stays faithful to the article: same topic, no "
    "invented facts, no unrelated promotion. Reply with JSON only, shaped as "
    '{"score": <0..1>, "consistent": <bool>, "issues": [<short strings>], '
    '"summary": <one sentence>}.'
)


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


class LlmSemanticAssessor:
    """Calls an OpenAI-compatible chat completions endpoint."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _build_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=self.settings.llm_base_url,
            timeout=self.settings.llm_timeout_seconds,
        )

    async def assess(
        self, *, source_title: str, source_text: str, platform: str, variant_text: str
    ) -> SemanticAssessment:
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Article title: {source_title}\n"
                        f"Article:\n{source_text[:4000]}\n\n"
                        f"Platform: {platform}\n"
                        f"Post:\n{variant_text}"
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        started = time.perf_counter()
        outcome = "success"

        try:
            client = self._build_client()
            close_after = self._client is None
            try:
                response = await client.post("/chat/completions", json=payload, headers=headers)
            finally:
                if close_after:
                    await client.aclose()
            response.raise_for_status()
            body = response.json()
            raw = body["choices"][0]["message"]["content"]
            judgement = LlmJudgement.model_validate_json(_strip_code_fence(raw))
        except (httpx.HTTPError, KeyError, IndexError, ValidationError, ValueError) as exc:
            outcome = "error"
            raise SemanticEvaluationError(f"llm evaluation failed: {exc}") from exc
        finally:
            INTEGRATION_LATENCY.labels(service="llm", operation="assess").observe(
                time.perf_counter() - started
            )
            INTEGRATION_REQUESTS_TOTAL.labels(
                service="llm", operation="assess", outcome=outcome
            ).inc()

        return SemanticAssessment(
            score=judgement.score,
            method="llm",
            summary=judgement.summary or "llm judgement",
            issues=judgement.issues,
        )


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------
class SemanticAssessor:
    """Chooses the configured evaluator and degrades to the heuristic safely."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.llm = LlmSemanticAssessor(settings, client=client)
        self._fallback_reason: str | None = None
        self._used_llm_successfully = False

    @property
    def method(self) -> str:
        return "llm" if self._used_llm_successfully else "heuristic"

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    async def assess(
        self, *, source_title: str, source_text: str, platform: str, variant_text: str
    ) -> SemanticAssessment:
        if not self.settings.llm_active:
            return heuristic_assessment(source_title, source_text, variant_text)

        try:
            assessment = await self.llm.assess(
                source_title=source_title,
                source_text=source_text,
                platform=platform,
                variant_text=variant_text,
            )
        except SemanticEvaluationError as exc:
            logger.warning(
                "llm semantic evaluation failed, falling back to heuristic",
                extra={"platform": platform, "detail": str(exc), "service": "llm"},
            )
            self._fallback_reason = str(exc)
            assessment = heuristic_assessment(source_title, source_text, variant_text)
            assessment.fallback_reason = str(exc)
            return assessment

        self._used_llm_successfully = True
        for issue in assessment.issues:
            logger.debug("llm issue", extra={"platform": platform, "detail": issue})
        return assessment


def semantic_details(assessment: SemanticAssessment, threshold: float) -> dict[str, Any]:
    return {
        "score": assessment.score,
        "threshold": threshold,
        "method": assessment.method,
        "matched_keywords": assessment.matched_keywords,
        "containment": assessment.containment,
        "coverage": assessment.coverage,
        "missing_keywords": assessment.missing_keywords,
        "issues": assessment.issues,
        "summary": assessment.summary,
    }


def parse_judgement(raw: str) -> LlmJudgement:
    """Exposed for tests: validates a raw LLM response body."""

    return LlmJudgement.model_validate_json(_strip_code_fence(raw))
