"""The evaluation pipeline.

Business logic: given original content and a set of platform variants, produce
per-variant results. No HTTP, no database and no adapter knowledge, which is what
makes it straightforward to unit test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.platforms import PLATFORM_RULES
from app.models.schemas import (
    CheckResult,
    DuplicatePair,
    SourceContent,
    VariantEvaluation,
)
from app.services.semantic import SemanticAssessment, SemanticAssessor, semantic_details
from app.services.validators import (
    check_duplicates,
    check_field_present,
    check_hashtags,
    check_length,
    check_malformed,
    check_platform_style,
    hashtags,
    summarise_checks,
)

logger = get_logger(__name__)

EXCERPT_LENGTH = 160


@dataclass
class EvaluationOutcome:
    variants: list[VariantEvaluation]
    duplicates: list[DuplicatePair]
    failed_checks: int
    evaluator_method: str
    fallback_reason: str | None = None
    assessments: dict[str, SemanticAssessment] = field(default_factory=dict)


async def evaluate_variants(
    *,
    source: SourceContent,
    variants: dict[str, str],
    platforms: list[str],
    settings: Settings,
    assessor: SemanticAssessor,
) -> EvaluationOutcome:
    """Run deterministic and semantic checks for each selected platform."""

    texts = {platform: variants.get(PLATFORM_RULES[platform].field) for platform in platforms}
    duplicates = check_duplicates(texts, settings.duplicate_threshold)

    semantic_tasks = {
        platform: _semantic_check(
            assessor=assessor,
            settings=settings,
            source=source,
            platform=platform,
            text=texts[platform],
        )
        for platform in platforms
        if texts[platform] and texts[platform].strip()
    }

    semantic_results: dict[str, CheckResult] = {}
    assessments: dict[str, SemanticAssessment] = {}
    if semantic_tasks:
        results = await asyncio.gather(*semantic_tasks.values(), return_exceptions=True)
        for platform, result in zip(semantic_tasks, results, strict=True):
            if isinstance(result, BaseException):  # pragma: no cover - defensive
                logger.error(
                    "semantic check crashed",
                    extra={"platform": platform, "detail": str(result)},
                )
                semantic_results[platform] = CheckResult(
                    name="semantic_consistency",
                    status="fail",
                    severity="error",
                    message=f"semantic check could not run: {result}",
                )
                continue
            check, assessment = result
            semantic_results[platform] = check
            assessments[platform] = assessment

    flagged = {platform for pair in duplicates for platform in pair.platforms}
    evaluations: list[VariantEvaluation] = []
    failed_checks = 0

    for platform in platforms:
        rule = PLATFORM_RULES[platform]
        text = texts[platform]
        checks: list[CheckResult] = [check_field_present(text)]

        if text and text.strip():
            checks.append(check_length(rule, text))
            checks.append(check_hashtags(rule, text))
            checks.append(check_malformed(text))
            style_check = check_platform_style(rule, text)
            if style_check is not None:
                checks.append(style_check)
            checks.append(
                _duplicate_check_for(platform, flagged, duplicates, settings.duplicate_threshold)
            )
            if platform in semantic_results:
                checks.append(semantic_results[platform])

        score, status = summarise_checks(checks)
        failed_checks += sum(
            1 for check in checks if check.status == "fail" and check.severity == "error"
        )
        evaluations.append(
            VariantEvaluation(
                platform=platform,
                field=rule.field,
                status=status,
                score=score,
                characters=len(text) if text else 0,
                hashtags=len(hashtags(text)) if text else None,
                checks=checks,
                excerpt=(text or "")[:EXCERPT_LENGTH],
            )
        )

    return EvaluationOutcome(
        variants=evaluations,
        duplicates=duplicates,
        failed_checks=failed_checks,
        evaluator_method=assessor.method,
        fallback_reason=assessor.fallback_reason,
        assessments=assessments,
    )


def _duplicate_check_for(
    platform: str,
    flagged: set[str],
    duplicates: list[DuplicatePair],
    threshold: float,
) -> CheckResult:
    if platform not in flagged:
        return CheckResult(
            name="duplicate_content",
            status="pass",
            severity="error",
            message="content is distinct from other platform variants",
        )
    partners = sorted(
        {
            other
            for pair in duplicates
            if platform in pair.platforms
            for other in pair.platforms
            if other != platform
        }
    )
    return CheckResult(
        name="duplicate_content",
        status="fail",
        severity="error",
        message=(
            f"content is nearly identical to {', '.join(partners)}, "
            f"so it is not adapted for {platform}"
        ),
        details={"threshold": threshold, "platforms": partners},
    )


async def _semantic_check(
    *,
    assessor: SemanticAssessor,
    settings: Settings,
    source: SourceContent,
    platform: str,
    text: str,
) -> tuple[CheckResult, SemanticAssessment]:
    assessment = await assessor.assess(
        source_title=source.title,
        source_text=source.content,
        platform=platform,
        variant_text=text,
    )
    passed = assessment.score >= settings.semantic_threshold
    check = CheckResult(
        name="semantic_consistency",
        status="pass" if passed else "fail",
        severity="error",
        message=(
            f"{assessment.method} evaluator scored faithfulness "
            f"{assessment.score:.2f} against a threshold of "
            f"{settings.semantic_threshold:.2f}. {assessment.summary}"
        ),
        details=semantic_details(assessment, settings.semantic_threshold),
    )
    return check, assessment
