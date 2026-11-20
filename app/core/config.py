"""Application configuration.

All configuration comes from environment variables (or a local ``.env`` file when
running outside containers). Nothing is hardcoded and no credential has a default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "ci", "staging", "production"]
LogFormat = Literal["json", "console"]
AdapterMode = Literal["mock", "http"]
MockScenario = Literal[
    "clean",
    "overflow",
    "missing_field",
    "duplicate",
    "generator_error",
]

VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


class Settings(BaseSettings):
    """Runtime settings for ContentOps Agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service identity ---------------------------------------------------
    app_name: str = "contentops-agent"
    app_version: str = "0.1.0"
    environment: Environment = "local"
    log_level: str = "INFO"
    log_format: LogFormat = "json"

    # --- Persistence --------------------------------------------------------
    database_path: Path = Path("data/contentops.db")
    database_busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    max_reports_returned: int = Field(default=100, ge=1, le=1000)

    # --- Variant Generator integration -------------------------------------
    variant_generator_mode: AdapterMode = "mock"
    variant_generator_base_url: str = "http://localhost:8000"
    variant_generator_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    variant_generator_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Social Media Studio integration -----------------------------------
    studio_mode: AdapterMode = "mock"
    studio_base_url: str = "http://localhost:3000"
    studio_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    studio_max_retries: int = Field(default=2, ge=0, le=5)

    # --- Mock behaviour (used by the mock adapters only) -------------------
    mock_variant_scenario: MockScenario = "clean"

    # --- Semantic evaluation (optional LLM) --------------------------------
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # --- Evaluation thresholds ---------------------------------------------
    semantic_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    duplicate_threshold: float = Field(default=0.90, ge=0.0, le=1.0)

    # --- Metrics ------------------------------------------------------------
    metrics_enabled: bool = True

    # --- Validators ---------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)}, got {value!r}")
        return level

    @field_validator("variant_generator_base_url", "studio_base_url", "llm_base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"expected an http(s) URL, got {value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _llm_requires_key(self) -> Settings:
        if self.llm_enabled and not self.llm_api_key:
            raise ValueError(
                "LLM_ENABLED=true requires LLM_API_KEY to be set. "
                "Leave LLM_ENABLED=false to use deterministic heuristics instead."
            )
        return self

    # --- Convenience --------------------------------------------------------
    @property
    def llm_active(self) -> bool:
        return bool(self.llm_enabled and self.llm_api_key)

    @property
    def variant_generator_configured(self) -> bool:
        return self.variant_generator_mode == "http" and bool(self.variant_generator_base_url)


@lru_cache
def get_settings() -> Settings:
    """Return process-wide settings (cached; tests clear the cache)."""

    return Settings()
