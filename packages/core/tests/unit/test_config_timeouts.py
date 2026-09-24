"""Shipped defaults for the chat-turn timeouts.

These pin the numbers themselves, which the other timeout tests deliberately
do not: they inject short values via `object.__setattr__` to exercise the
deadline path, so a silent change to the defaults would not fail anything.
"""
from __future__ import annotations

import pytest

from openexecutive.config import Settings


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")


def test_chat_turn_ceiling_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.chat_stream_timeout_s == 300.0
    assert s.committee_extra_timeout_s == 60.0
    # The route sums them for a Committee turn.
    assert s.chat_stream_timeout_s + s.committee_extra_timeout_s == 360.0


def test_interview_did_not_inherit_the_chat_ceiling() -> None:
    """The onboarding interview has its own, lower ceiling.

    It used to borrow `chat_stream_timeout_s`. It retries twice, so inheriting
    the 300s chat ceiling would mean a 10-minute hang before the wizard
    surfaces a timeout to the user.
    """
    s = Settings(_env_file=None)
    assert s.interview_timeout_s == 120.0
    assert s.interview_timeout_s < s.chat_stream_timeout_s


def test_timeouts_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_STREAM_TIMEOUT_S", "45")
    monkeypatch.setenv("COMMITTEE_EXTRA_TIMEOUT_S", "15")
    monkeypatch.setenv("INTERVIEW_TIMEOUT_S", "30")
    s = Settings(_env_file=None)
    assert s.chat_stream_timeout_s == 45.0
    assert s.committee_extra_timeout_s == 15.0
    assert s.interview_timeout_s == 30.0
