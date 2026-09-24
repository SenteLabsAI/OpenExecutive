"""HONCHO_ENABLED=true must require HONCHO_API_KEY.

HONCHO_BASE_URL is optional — the honcho-ai SDK accepts base_url=None
and falls back to its built-in production endpoint, which is the right
behavior for the hosted-Honcho setup we run in prod.
"""
from __future__ import annotations

import pytest

from openexecutive.config import Settings


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    # Clear any locally-set Honcho vars so each test starts from a known floor.
    for k in (
        "HONCHO_ENABLED",
        "HONCHO_API_KEY",
        "HONCHO_BASE_URL",
        "HONCHO_PREFETCH_MODE",
        "HONCHO_PREFETCH_MAX_CONCLUSIONS",
    ):
        monkeypatch.delenv(k, raising=False)


def test_disabled_does_not_require_url_or_key() -> None:
    s = Settings()
    assert s.honcho_enabled is False


def test_enabled_without_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONCHO_ENABLED", "true")
    monkeypatch.setenv("HONCHO_BASE_URL", "http://h")
    with pytest.raises(ValueError, match="HONCHO_API_KEY"):
        Settings()


def test_enabled_without_base_url_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK falls back to its production endpoint when base_url is None."""
    monkeypatch.setenv("HONCHO_ENABLED", "true")
    monkeypatch.setenv("HONCHO_API_KEY", "k")
    s = Settings()
    assert s.honcho_enabled is True
    assert s.honcho_base_url is None


def test_enabled_with_all_optional_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONCHO_ENABLED", "true")
    monkeypatch.setenv("HONCHO_API_KEY", "k")
    monkeypatch.setenv("HONCHO_BASE_URL", "http://h")
    s = Settings()
    assert s.honcho_enabled is True
    assert s.honcho_api_key == "k"
    assert s.honcho_base_url == "http://h"


def test_prefetch_mode_defaults_to_representation() -> None:
    s = Settings()
    assert s.honcho_prefetch_mode == "representation"
    assert s.honcho_prefetch_max_conclusions == 20


def test_prefetch_mode_accepts_dialectic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONCHO_PREFETCH_MODE", "dialectic")
    assert Settings().honcho_prefetch_mode == "dialectic"


def test_prefetch_mode_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONCHO_PREFETCH_MODE", "context")
    with pytest.raises(ValueError, match="HONCHO_PREFETCH_MODE|honcho_prefetch_mode"):
        Settings()


@pytest.mark.parametrize("value", ["0", "101"])
def test_prefetch_max_conclusions_is_bounded(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("HONCHO_PREFETCH_MAX_CONCLUSIONS", value)
    with pytest.raises(ValueError):
        Settings()
