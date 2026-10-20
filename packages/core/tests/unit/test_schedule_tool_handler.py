"""Unit tests for the schedule_followup tool handler.

Focuses on input validation, anti-spam guards, and timezone handling. The send-
tool handlers are exercised separately (they hit real network APIs in prod).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory.episodic import initialize_db
from openexecutive.orchestrator.schedule_tools import (
    current_session,
    handle_schedule_followup,
)
from openexecutive.orchestrator.session import Session


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


@pytest.fixture()
def session_with_telegram_ref() -> Iterator[Session]:
    s = Session()
    s.seen_channel_refs.add(("telegram", "42"))
    token = current_session.set(s)
    yield s
    current_session.reset(token)


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _call(tool_input: dict) -> dict:
    return json.loads(asyncio.run(handle_schedule_followup(tool_input)))


def test_happy_path_returns_scheduled_id(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "Remind user about the board prep.",
        }
    )
    assert payload["status"] == "scheduled"
    assert isinstance(payload["id"], int)
    assert payload["channel"] == "telegram"


def test_rejects_past_run_at(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "run_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "x",
        }
    )
    assert "must be in the future" in payload["error"]


def test_rejects_unparseable_run_at(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "run_at": "next tuesday-ish",
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "x",
        }
    )
    assert "ISO8601" in payload["error"]


def test_rejects_unknown_channel(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "run_at": _future_iso(),
            "channel": "carrier_pigeon",
            "channel_ref": "42",
            "intent": "x",
        }
    )
    assert "unknown channel" in payload["error"]


def test_rejects_unseen_channel_ref(session_with_telegram_ref: Session) -> None:
    payload = _call(
        {
            "run_at": _future_iso(),
            "channel": "telegram",
            "channel_ref": "99",  # not in seen_channel_refs
            "intent": "x",
        }
    )
    assert "not seen in this session" in payload["error"]


def test_rejects_beyond_horizon(session_with_telegram_ref: Session) -> None:
    far = (datetime.now(UTC) + timedelta(days=400)).isoformat()
    payload = _call(
        {
            "run_at": far,
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "x",
        }
    )
    assert "days out" in payload["error"]


def test_accepts_z_suffix_utc(session_with_telegram_ref: Session) -> None:
    iso_z = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _call(
        {
            "run_at": iso_z,
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "x",
        }
    )
    assert payload["status"] == "scheduled"


def test_rejects_when_pending_cap_exceeded(
    session_with_telegram_ref: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_PENDING_PER_CHANNEL_REF", "1")
    first = _call(
        {
            "run_at": _future_iso(60),
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "first",
        }
    )
    assert first["status"] == "scheduled"
    second = _call(
        {
            "run_at": _future_iso(120),
            "channel": "telegram",
            "channel_ref": "42",
            "intent": "second",
        }
    )
    assert "too many pending" in second["error"]
