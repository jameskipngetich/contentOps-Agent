"""Request and response models.

These models are the public contract of the API and are what Swagger renders.
Internal data structures live next to the code that uses them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.platforms import PLATFORM_RULES, all_fields

CheckStatus = Literal["pass", "warning", "fail"]
ReportStatus = Literal["passed", "warnings", "failed"]
VariantSource = Literal["inline", "variant_generator"]
SemanticEvaluator = Literal["heuristic", "llm"]


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class SourceContent(BaseModel):
    """The original content the variants were derived from."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20_000)
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("url", mode="before")
    @classmethod
    def _normalise_placeholder_url(cls, value: Any) -> Any:
        # The existing Studio sends the literal string "none" when a post has
        # no source URL. Treat that as absent rather than rejecting the request.
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
            return None
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class EvaluationRequest(BaseModel):
    """Either supply the original content, or name a post in the Studio."""

    model_config = ConfigDict(extra="forbid")

    source: SourceContent | None = None
    studio_post_name: str | None = Field(default=None, min_length=1, max_length=200)
    variants: dict[str, str] | None = Field(
        default=None,
        description=(
            "Platform variations keyed by generator field name "
            "(x_post, instagram_caption, discord_message, telegram_post). "
            "Omit to fetch them from the Variant Generator adapter."
        ),
    )
    platforms: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Restrict evaluation to these platforms. Defaults to all four.",
    )
    parent_report_id: str | None = Field(
        default=None,
        description="Set when this evaluation re-checks a regenerated report.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EvaluationRequest:
        if (self.source is None) == (self.studio_post_name is None):
            raise ValueError(
                "provide exactly one of 'source' (inline content) or "
                "'studio_post_name' (fetch it from Social Media Studio)"
            )
        return self

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted({p for p in value if p not in PLATFORM_RULES})
        if unknown:
            raise ValueError(
                f"unknown platform(s): {', '.join(unknown)}. Supported: {', '.join(PLATFORM_RULES)}"
            )
        return list(dict.fromkeys(value))

    @field_validator("variants")
    @classmethod
    def _validate_variant_fields(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        known = set(all_fields())
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(
                f"unknown variant field(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(sorted(known))}"
            )
        return value

    @property
    def selected_platforms(self) -> list[str]:
        return self.platforms or list(PLATFORM_RULES)


class RegenerateRequest(BaseModel):
    """Optional body for re-running a failed evaluation."""

    model_config = ConfigDict(extra="forbid")

    platforms: list[str] | None = None

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted({p for p in value if p not in PLATFORM_RULES})
        if unknown:
            raise ValueError(f"unknown platform(s): {', '.join(unknown)}")
        return list(dict.fromkeys(value))


# ---------------------------------------------------------------------------
# Evaluation output
# ---------------------------------------------------------------------------
class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    severity: Literal["error", "warning", "info"]
    message: str
    details: dict[str, Any] | None = None


class DuplicatePair(BaseModel):
    platforms: list[str]
    similarity: float = Field(ge=0.0, le=1.0)


class VariantEvaluation(BaseModel):
    platform: str
    field: str
    status: ReportStatus
    score: int = Field(ge=0, le=100)
    characters: int
    hashtags: int | None
    checks: list[CheckResult]
    excerpt: str


class RegenerationRequestItem(BaseModel):
    platform: str
    field: str
    reasons: list[str]
    instruction: str


class TraceStep(BaseModel):
    step: str
    status: Literal["ok", "failed", "skipped"]
    duration_ms: float
    note: str | None = None


class SourceSummary(BaseModel):
    title: str
    url: str | None = None
    characters: int


class EvaluatorInfo(BaseModel):
    semantic: SemanticEvaluator
    llm_enabled: bool
    fallback_reason: str | None = None


class EvaluationReport(BaseModel):
    report_id: str
    created_at: datetime
    status: ReportStatus
    score: int = Field(ge=0, le=100)
    source: SourceSummary
    variant_source: VariantSource
    variants: list[VariantEvaluation]
    duplicates: list[DuplicatePair]
    failed_checks: int
    regeneration_requests: list[RegenerationRequestItem]
    evaluator: EvaluatorInfo
    parent_report_id: str | None = None
    duration_ms: float
    trace: list[TraceStep]


class ReportSummary(BaseModel):
    report_id: str
    created_at: datetime
    status: ReportStatus
    score: int
    title: str
    platforms: list[str]
    failed_checks: int
    parent_report_id: str | None = None


class ReportListResponse(BaseModel):
    items: list[ReportSummary]
    count: int
    limit: int


# ---------------------------------------------------------------------------
# Service metadata
# ---------------------------------------------------------------------------
class ServiceInfo(BaseModel):
    name: str
    version: str
    description: str
    documentation: str
    endpoints: list[str]
    supported_platforms: list[str]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    uptime_seconds: float


class DependencyStatus(BaseModel):
    name: str
    required: bool
    status: Literal["ok", "degraded", "unavailable"]
    detail: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[DependencyStatus]
