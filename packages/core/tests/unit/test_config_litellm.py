"""Config-layer tests for LiteLLM gateway settings and provider-availability.

Build ``Settings`` with ``_env_file=None`` so the repo-root ``.env`` can't
leak values in, and drive the provider env vars explicitly via monkeypatch.
"""
from __future__ import annotations

import pytest

from openexecutive.config import Settings

_PROVIDER_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENROUTER_ENABLED",
    "OPENROUTER_API_KEY",
    "LOCAL_MODELS_ENABLED",
    "LOCAL_BASE_URL",
    "LOCAL_MODELS",
    "LOCAL_API_KEY",
    "LITELLM_ENABLED",
    "LITELLM_MODELS",
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
)


def _build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")
    for key in _PROVIDER_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_litellm_models_csv_parses_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _build(
        monkeypatch,
        ANTHROPIC_API_KEY="sk-test",
        LITELLM_ENABLED="true",
        LITELLM_MODELS="gemini/gemini-2.5-pro, groq/llama-3.3-70b ,",
    )
    assert s.litellm_models == ["gemini/gemini-2.5-pro", "groq/llama-3.3-70b"]


def test_litellm_defaults_off_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _build(monkeypatch, ANTHROPIC_API_KEY="sk-test")
    assert s.litellm_enabled is False
    assert s.litellm_models == []


def test_litellm_enabled_requires_models(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="LITELLM_MODELS"):
        _build(monkeypatch, ANTHROPIC_API_KEY="sk-test", LITELLM_ENABLED="true")


def test_litellm_alone_satisfies_provider_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Anthropic key, no OpenRouter, no local — LiteLLM alone is enough.
    s = _build(
        monkeypatch,
        LITELLM_ENABLED="true",
        LITELLM_MODELS="gemini/gemini-2.5-pro",
    )
    assert s.litellm_enabled is True
    assert s.anthropic_api_key is None


def test_no_provider_configured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="No LLM provider configured"):
        _build(monkeypatch)
