"""Configuration handling and environment overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_are_safe_and_offline():
    settings = Settings()
    assert settings.variant_generator_mode == "mock"
    assert settings.studio_mode == "mock"
    assert settings.llm_enabled is False
    assert settings.llm_active is False


def test_llm_enabled_without_key_is_rejected():
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        Settings(llm_enabled=True, llm_api_key=None)


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(log_level="chatty")


def test_log_level_is_normalised():
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_base_urls_are_validated_and_trimmed():
    settings = Settings(studio_base_url="http://studio.local:3000/")
    assert settings.studio_base_url == "http://studio.local:3000"
    with pytest.raises(ValidationError):
        Settings(studio_base_url="studio.local:3000")


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VARIANT_GENERATOR_MODE", "http")
    monkeypatch.setenv("VARIANT_GENERATOR_BASE_URL", "http://variants.local:8000")
    monkeypatch.setenv("MOCK_VARIANT_SCENARIO", "overflow")
    settings = Settings()
    assert settings.variant_generator_mode == "http"
    assert settings.variant_generator_base_url == "http://variants.local:8000"
    assert settings.mock_variant_scenario == "overflow"


def test_thresholds_must_be_probabilities():
    with pytest.raises(ValidationError):
        Settings(semantic_threshold=1.5)
