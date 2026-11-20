"""The evaluation agent.

The agent is the thin orchestration layer: it decides where content comes from,
runs the evaluation pipeline, turns failed checks into concrete regeneration
requests, and stores the report. Every step is recorded in ``trace`` so a failed
request can be explained without reading the code.

Keeping this separate from ``app.services.evaluation`` means the rules can be
tested without adapters, and the orchestration can be tested with fake adapters.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, UpstreamServiceError
from app.core.logging import get_logger
from app.core.metrics import (
    EVALUATION_DURATION,
    EVALUATIONS_TOTAL,
    FAILED_CHECKS_TOTAL,
    REPORTS_STORED,
    SEMANTIC_EVALUATOR_USED,
    VARIANT_RESULTS_TOTAL,
)
from app.integrations.base import IntegrationError
from app.integrations.studio import StudioPort
from app.integrations.variant_generator import VariantGeneratorPort
from app.models.db import ReportRepository
from app.models.platforms import PLATFORM_RULES
from app.models.schemas import (
    EvaluationReport,
    EvaluationRequest,
    EvaluatorInfo,
    RegenerationRequestItem,
    SourceContent,
    SourceSummary,
    TraceStep,
    VariantEvaluation,
)
from app.services.evaluation import EvaluationOutcome, evaluate_variants
from app.services.semantic import SemanticAssessor

logger = get_logger(__name__)


class _Trace:
    """Small helper so every step reports the same shape."""

    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    def record(self, step: str, status: str, started: float, note: str | None = None) -> None:
        self.steps.append(
            TraceStep(
                step=step,
                status=status,  # type: ignore[arg-type]
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                note=note,
            )
        )


class EvaluationAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: ReportRepository,
        variant_generator: VariantGeneratorPort,
        studio: StudioPort,
        assessor_factory: Callable[[], SemanticAssessor] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.variant_generator = variant_generator
        self.studio = studio
        self._assessor_factory = assessor_factory or (lambda: SemanticAssessor(settings))

    # -- public API --------------------------------------------------------
    async def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        started = time.perf_counter()
        trace = _Trace()

        source = await self._resolve_source(request, trace)

        if request.variants is not None:
            variants = _select_variants(request.variants, request.selected_platforms)
            variant_source = "inline"
            trace.record("resolve_variants", "ok", started, "variants supplied in the request")
        else:
            step_started = time.perf_counter()
            try:
                variants = await self.variant_generator.fetch_variants(
                    source, platforms=request.selected_platforms
                )
            except IntegrationError as exc:
                trace.record("fetch_variants", "failed", step_started, str(exc))
                raise UpstreamServiceError(
                    "The Variant Generator could not return variants.",
                    details=exc.as_detail(),
                ) from exc
            variant_source = "variant_generator"
            trace.record(
                "fetch_variants",
                "ok",
                step_started,
                f"{len(variants)} platform fields returned",
            )

        outcome = await self._evaluate(
            source=source,
            variants=variants,
            platforms=request.selected_platforms,
            parent_report_id=request.parent_report_id,
            variant_source=variant_source,
            trace=trace,
        )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        report = self._build_report(
            source=source,
            outcome=outcome,
            variant_source=variant_source,
            parent_report_id=request.parent_report_id,
            duration_ms=duration_ms,
            trace=trace,
        )
        self._persist(report, source)
        self._record_metrics(report, duration_ms)
        return report

    async def regenerate(
        self, report_id: str, platforms: list[str] | None = None
    ) -> EvaluationReport:
        """Re-run evaluation for variants that already failed."""

        original = self.repository.get(report_id)
        if original is None:
            raise NotFoundError(f"report {report_id!r} not found")
        source_data = self.repository.get_source(report_id)
        if source_data is None:
            raise ConflictError(
                "The original content for this report is not stored, so it cannot be regenerated."
            )

        source = SourceContent.model_validate(source_data)
        targets = [
            variant.platform
            for variant in original.variants
            if variant.status == "failed" and (platforms is None or variant.platform in platforms)
        ]
        if not targets:
            raise ConflictError(
                "No variant in this report needs regeneration.",
                details={"platforms": [v.platform for v in original.variants]},
            )

        trace = _Trace()
        started = time.perf_counter()
        reasons = {
            variant.platform: _failure_reasons(variant)
            for variant in original.variants
            if variant.platform in targets
        }
        try:
            regenerated = await self.variant_generator.request_regeneration(
                source,
                platforms=targets,
                reason="; ".join(f"{p}: {', '.join(r)}" for p, r in reasons.items()),
            )
        except IntegrationError as exc:
            trace.record("request_regeneration", "failed", started, str(exc))
            raise UpstreamServiceError(
                "The Variant Generator could not regenerate the failing variants.",
                details=exc.as_detail(),
            ) from exc
        trace.record("request_regeneration", "ok", started, f"regenerated {', '.join(targets)}")

        outcome = await self._evaluate(
            source=source,
            variants=_select_variants(regenerated, targets),
            platforms=targets,
            parent_report_id=report_id,
            variant_source=original.variant_source,
            trace=trace,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        report = self._build_report(
            source=source,
            outcome=outcome,
            variant_source=original.variant_source,
            parent_report_id=report_id,
            duration_ms=duration_ms,
            trace=trace,
        )
        self._persist(report, source)
        self._record_metrics(report, duration_ms)
        return report

    # -- internals ---------------------------------------------------------
    async def _resolve_source(self, request: EvaluationRequest, trace: _Trace) -> SourceContent:
        started = time.perf_counter()
        if request.source is not None:
            trace.record("resolve_source", "ok", started, "original content supplied inline")
            return request.source

        assert request.studio_post_name is not None
        try:
            source = await self.studio.fetch_post(request.studio_post_name)
        except IntegrationError as exc:
            trace.record("fetch_studio_post", "failed", started, str(exc))
            if exc.kind == "not_found":
                raise NotFoundError(
                    f"post {request.studio_post_name!r} was not found in Social Media Studio",
                    details=exc.as_detail(),
                ) from exc
            raise UpstreamServiceError(
                "Social Media Studio could not provide the original post.",
                details=exc.as_detail(),
            ) from exc
        trace.record(
            "fetch_studio_post", "ok", started, f"loaded post {request.studio_post_name!r}"
        )
        return source

    async def _evaluate(
        self,
        *,
        source: SourceContent,
        variants: dict[str, str],
        platforms: list[str],
        parent_report_id: str | None,
        variant_source: str,
        trace: _Trace,
    ) -> EvaluationOutcome:
        started = time.perf_counter()
        outcome = await evaluate_variants(
            source=source,
            variants=variants,
            platforms=platforms,
            settings=self.settings,
            assessor=self._assessor_factory(),
        )
        trace.record(
            "evaluate_variants",
            "ok",
            started,
            f"{len(outcome.variants)} variants, {outcome.failed_checks} blocking failures",
        )
        logger.info(
            "evaluation completed",
            extra={
                "report_id": parent_report_id or "new",
                "platform": ",".join(platforms),
                "detail": (
                    f"variant_source={variant_source}, evaluator={outcome.evaluator_method}"
                ),
            },
        )
        return outcome

    def _build_report(
        self,
        *,
        source: SourceContent,
        outcome: EvaluationOutcome,
        variant_source: str,
        parent_report_id: str | None,
        duration_ms: float,
        trace: _Trace,
    ) -> EvaluationReport:
        regeneration = [
            _regeneration_request(variant)
            for variant in outcome.variants
            if variant.status == "failed"
        ]
        status = _overall_status(outcome.variants)

        report = EvaluationReport(
            report_id=uuid.uuid4().hex,
            created_at=datetime.now(UTC),
            status=status,
            score=_overall_score(outcome.variants),
            source=SourceSummary(
                title=source.title,
                url=source.url,
                characters=len(source.content),
            ),
            variant_source=variant_source,  # type: ignore[arg-type]
            variants=outcome.variants,
            duplicates=outcome.duplicates,
            failed_checks=outcome.failed_checks,
            regeneration_requests=regeneration,
            evaluator=EvaluatorInfo(
                semantic=outcome.evaluator_method,  # type: ignore[arg-type]
                llm_enabled=self.settings.llm_active,
                fallback_reason=outcome.fallback_reason,
            ),
            parent_report_id=parent_report_id,
            duration_ms=duration_ms,
            trace=trace.steps,
        )
        logger.info(
            "report built",
            extra={
                "report_id": report.report_id,
                "status": report.status,
                "detail": f"score={report.score}",
            },
        )
        return report

    def _persist(self, report: EvaluationReport, source: SourceContent) -> None:
        self.repository.save(report, source_json=json.dumps(source.model_dump()))
        REPORTS_STORED.set(self.repository.count())

    def _record_metrics(self, report: EvaluationReport, duration_ms: float) -> None:
        EVALUATIONS_TOTAL.labels(status=report.status).inc()
        EVALUATION_DURATION.observe(duration_ms / 1000)
        SEMANTIC_EVALUATOR_USED.labels(evaluator=report.evaluator.semantic).inc()
        for variant in report.variants:
            VARIANT_RESULTS_TOTAL.labels(platform=variant.platform, status=variant.status).inc()
            for check in variant.checks:
                if check.status != "pass":
                    FAILED_CHECKS_TOTAL.labels(
                        platform=variant.platform,
                        check=check.name,
                        status=check.status,
                    ).inc()


def _select_variants(variants: dict[str, str], platforms: list[str]) -> dict[str, str]:
    allowed = {PLATFORM_RULES[platform].field for platform in platforms}
    return {field: value for field, value in variants.items() if field in allowed}


def _failure_reasons(variant: VariantEvaluation) -> list[str]:
    return [f"{check.name}: {check.message}" for check in variant.checks if check.status == "fail"]


def _regeneration_request(variant: VariantEvaluation) -> RegenerationRequestItem:
    reasons = _failure_reasons(variant) or ["variant failed evaluation"]
    return RegenerationRequestItem(
        platform=variant.platform,
        field=variant.field,
        reasons=reasons,
        instruction=(
            f"Regenerate the {variant.platform} variant ({variant.field}). "
            "Fix: " + "; ".join(reasons) + "."
        ),
    )


def _overall_status(variants: list[VariantEvaluation]) -> str:
    statuses = {variant.status for variant in variants}
    if "failed" in statuses:
        return "failed"
    if "warnings" in statuses:
        return "warnings"
    return "passed"


def _overall_score(variants: list[VariantEvaluation]) -> int:
    if not variants:
        return 0
    return round(sum(variant.score for variant in variants) / len(variants))
