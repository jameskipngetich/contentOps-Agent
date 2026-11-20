"""Evaluation agent orchestration, using fake adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.evaluator import EvaluationAgent
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, UpstreamServiceError
from app.integrations.base import IntegrationError
from app.integrations.studio import MockStudioAdapter, StudioPort
from app.integrations.variant_generator import MockVariantGeneratorAdapter
from app.models.db import ReportRepository
from app.models.schemas import EvaluationRequest, SourceContent

SOURCE = SourceContent(
    title="Observability for generated content",
    content=(
        "A pipeline that validates generated social posts with deterministic "
        "checks, duplicate detection and metrics so failures are traceable."
    ),
)

CLEAN_VARIANTS = {
    "x_post": (
        "Observability for generated content: deterministic checks, duplicate detection. #metrics"
    ),
    "instagram_caption": (
        "Observability for generated content\n\n"
        "Generated posts need deterministic checks, duplicate detection and metrics.\n\n"
        "- Why: failures must be traceable\n"
        "- Next: read the full post\n\n"
        "#metrics #contentops #validation #pipeline #quality"
    ),
    "discord_message": (
        "**Observability for generated content**\n"
        "> Deterministic checks, duplicate detection and metrics.\n"
        "Questions below."
    ),
    "telegram_post": (
        "Observability for generated content\n\n"
        "- deterministic checks\n- duplicate detection\n- metrics\n\nRead more"
    ),
}

BROKEN_VARIANTS = {
    "x_post": "Observability for generated content. #metrics",
    # instagram_caption deliberately missing
    "discord_message": "**Observability for generated content**\n> checks and metrics",
    "telegram_post": "Observability for generated content\n- deterministic checks\n- metrics",
}


class SequenceGenerator:
    """Returns whatever is queued; records how it was called."""

    def __init__(self, fetch: dict[str, str] | None = None, regenerate=None) -> None:
        self.fetch = fetch or CLEAN_VARIANTS
        self.regenerate = regenerate
        self.regenerate_calls: list[dict[str, object]] = []

    async def fetch_variants(self, source, platforms=None):
        if isinstance(self.fetch, Exception):
            raise self.fetch
        return dict(self.fetch)

    async def request_regeneration(self, source, *, platforms, reason):
        self.regenerate_calls.append({"platforms": platforms, "reason": reason})
        if isinstance(self.regenerate, Exception):
            raise self.regenerate
        return dict(self.regenerate or CLEAN_VARIANTS)


class FailingStudio(StudioPort):
    async def fetch_post(self, name: str):
        raise IntegrationError(
            "studio", "fetch_post", "post missing", kind="not_found", status_code=404
        )

    async def request_variants(self, name: str):
        raise NotImplementedError


def _agent(
    tmp_path: Path,
    *,
    generator=None,
    studio: StudioPort | None = None,
    **settings_overrides: object,
) -> tuple[EvaluationAgent, ReportRepository]:
    settings = Settings(
        database_path=tmp_path / "agent.db",
        **settings_overrides,  # type: ignore[arg-type]
    )
    repository = ReportRepository(settings.database_path)
    repository.initialise()
    agent = EvaluationAgent(
        settings=settings,
        repository=repository,
        variant_generator=generator or SequenceGenerator(),
        studio=studio or MockStudioAdapter(),
    )
    return agent, repository


async def test_evaluate_inline_variants_stores_report(tmp_path: Path):
    agent, repository = _agent(tmp_path)
    request = EvaluationRequest(source=SOURCE, variants=CLEAN_VARIANTS)
    report = await agent.evaluate(request)

    assert report.status == "passed"
    assert report.variant_source == "inline"
    assert {v.platform for v in report.variants} == {"x", "instagram", "discord", "telegram"}
    assert report.regeneration_requests == []
    assert report.evaluator.semantic == "heuristic"
    assert repository.get(report.report_id) is not None
    assert report.trace[0].step == "resolve_source"


async def test_missing_variant_produces_failed_check_and_regeneration_request(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    request = EvaluationRequest(source=SOURCE, variants=BROKEN_VARIANTS)
    report = await agent.evaluate(request)

    instagram = next(v for v in report.variants if v.platform == "instagram")
    assert instagram.status == "failed"
    assert instagram.checks[0].name == "field_present"
    assert instagram.checks[0].status == "fail"
    assert report.status == "failed"
    assert [r.platform for r in report.regeneration_requests] == ["instagram"]
    assert "field_present" in report.regeneration_requests[0].instruction


async def test_evaluate_fetches_variants_from_generator(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    report = await agent.evaluate(EvaluationRequest(source=SOURCE))
    assert report.variant_source == "variant_generator"
    assert len(report.variants) == 4


async def test_generator_failure_maps_to_upstream_error(tmp_path: Path):
    generator = SequenceGenerator(
        fetch=IntegrationError("variant_generator", "generate_variants", "down")
    )
    agent, _ = _agent(tmp_path, generator=generator)
    with pytest.raises(UpstreamServiceError) as excinfo:
        await agent.evaluate(EvaluationRequest(source=SOURCE))
    assert excinfo.value.details["service"] == "variant_generator"


async def test_studio_post_is_loaded_by_name(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    report = await agent.evaluate(
        EvaluationRequest(studio_post_name="demo-post", variants=CLEAN_VARIANTS)
    )
    assert report.source.title.startswith("Shipping a content pipeline")


async def test_unknown_studio_post_is_not_found(tmp_path: Path):
    agent, _ = _agent(tmp_path, studio=FailingStudio())
    with pytest.raises(NotFoundError):
        await agent.evaluate(EvaluationRequest(studio_post_name="missing"))


async def test_platform_filter_limits_evaluation(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    report = await agent.evaluate(
        EvaluationRequest(source=SOURCE, variants=CLEAN_VARIANTS, platforms=["x", "discord"])
    )
    assert [v.platform for v in report.variants] == ["x", "discord"]


async def test_regenerate_reruns_failed_platforms_only(tmp_path: Path):
    generator = SequenceGenerator(regenerate=CLEAN_VARIANTS)
    agent, repository = _agent(tmp_path, generator=generator)

    original = await agent.evaluate(EvaluationRequest(source=SOURCE, variants=BROKEN_VARIANTS))
    assert original.status == "failed"

    report = await agent.regenerate(original.report_id)

    assert report.parent_report_id == original.report_id
    assert [v.platform for v in report.variants] == ["instagram"]
    assert report.status == "passed"
    assert generator.regenerate_calls[0]["platforms"] == ["instagram"]
    assert "field_present" in str(generator.regenerate_calls[0]["reason"])
    assert repository.get(report.report_id) is not None


async def test_regenerate_with_nothing_to_fix_conflicts(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    report = await agent.evaluate(EvaluationRequest(source=SOURCE, variants=CLEAN_VARIANTS))
    with pytest.raises(ConflictError):
        await agent.regenerate(report.report_id)


async def test_regenerate_unknown_report_is_not_found(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    with pytest.raises(NotFoundError):
        await agent.regenerate("missing-report")


async def test_regenerate_reports_generator_failure(tmp_path: Path):
    generator = SequenceGenerator(
        regenerate=IntegrationError("variant_generator", "generate_variants", "down")
    )
    agent, _ = _agent(tmp_path, generator=generator)
    original = await agent.evaluate(EvaluationRequest(source=SOURCE, variants=BROKEN_VARIANTS))
    with pytest.raises(UpstreamServiceError):
        await agent.regenerate(original.report_id)


async def test_duplicate_scenario_flags_every_platform(tmp_path: Path):
    agent, _ = _agent(tmp_path)
    report = await agent.evaluate(
        EvaluationRequest(
            source=SOURCE, variants=dict.fromkeys(CLEAN_VARIANTS, CLEAN_VARIANTS["x_post"])
        )
    )
    assert report.duplicates
    assert all(v.status == "failed" for v in report.variants)
    assert all(
        any(c.name == "duplicate_content" and c.status == "fail" for c in v.checks)
        for v in report.variants
    )


async def test_mock_generator_with_missing_field_scenario(tmp_path: Path):
    agent, _ = _agent(tmp_path, generator=MockVariantGeneratorAdapter("missing_field"))
    report = await agent.evaluate(EvaluationRequest(source=SOURCE))
    instagram = next(v for v in report.variants if v.platform == "instagram")
    assert instagram.status == "failed"
