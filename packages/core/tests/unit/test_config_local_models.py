"""Config-layer tests for local-model settings and provider-availability.

These build ``Settings`` directly with ``_env_file=None`` so the repo-root
``.env`` can't leak values in, and drive the provider-related env vars
explicitly via monkeypatch.
"""
from __future__ import annotations

import pytest

from openexecutive.config import Settings

_PROVIDER_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENROUTER_ENABLED",
    "OPENROUTER_API_KEY",
    "ORCAROUTER_ENABLED",
    "ORCAROUTER_API_KEY",
    "LOCAL_MODELS_ENABLED",
    "LOCAL_BASE_URL",
    "LOCAL_MODELS",
    "LOCAL_API_KEY",
)


def _build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    # EXEC_EMAIL_ADDRESS is a required field unrelated to provider routing —
    # keep a valid baseline so construction reaches the provider validators.
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")
    for key in _PROVIDER_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_local_models_csv_parses_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _build(
        monkeypatch,
        ANTHROPIC_API_KEY="sk-test",
        LOCAL_MODELS_ENABLED="true",
        LOCAL_BASE_URL="http://localhost:11434/v1",
        LOCAL_MODELS="llama3.3, qwen2.5:14b ,",
    )
    assert s.local_models == ["llama3.3", "qwen2.5:14b"]


def test_local_defaults_off_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _build(monkeypatch, ANTHROPIC_API_KEY="sk-test")
    assert s.local_models_enabled is False
    assert s.local_models == []


def test_local_enabled_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="LOCAL_BASE_URL"):
        _build(monkeypatch, ANTHROPIC_API_KEY="sk-test", LOCAL_MODELS_ENABLED="true")


def test_anthropic_free_boot_with_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline of this enhancement: boot with NO Anthropic key."""
    s = _build(
        monkeypatch,
        LOCAL_MODELS_ENABLED="true",
        LOCAL_BASE_URL="http://localhost:11434/v1",
        LOCAL_MODELS="llama3.3",
    )
    assert s.anthropic_api_key is None
    assert s.local_models_enabled is True


def test_no_provider_configured_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # ValidationError subclasses ValueError, so this catches the model_validator.
    with pytest.raises(ValueError, match="No LLM provider"):
        _build(monkeypatch)


def test_anthropic_key_alone_still_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _build(monkeypatch, ANTHROPIC_API_KEY="sk-test")
    assert s.anthropic_api_key == "sk-test"


def test_orcarouter_alone_still_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot with NO Anthropic key — OrcaRouter alone is a valid backend."""
    s = _build(
        monkeypatch,
        ORCAROUTER_ENABLED="true",
        ORCAROUTER_API_KEY="sk-orca-test",
    )
    assert s.anthropic_api_key is None
    assert s.orcarouter_enabled is True


def test_orcarouter_enabled_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="ORCAROUTER_API_KEY"):
        _build(monkeypatch, ORCAROUTER_ENABLED="true")
