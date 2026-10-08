"""Unit tests for the direct-send activity persistence.

After a successful send_slack_dm / send_discord_dm / send_telegram_message,
the handler must write a status='done' scheduled_actions row so the row
surfaces in `GET /today/activity`. Without it, the only trace of the send
is the transient action_taken SSE chip — the Recent Activity panel on the
brief stays empty.

Failed sends must NOT write a row (no green chip over a red outcome — same
invariant the action_chips layer enforces).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.memory.episodic import initialize_db, list_scheduled_actions
from openexecutive.orchestrator import schedule_tools


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def settings_with_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub get_settings so the send handlers don't bail on missing tokens."""
    class _S:
        slack_bot_token = "xoxb-test"
        discord_bot_token = "discord-test"
        telegram_bot_token = "tg-test"

    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: _S()
    )


def _done_rows() -> list:
    return [a for a in list_scheduled_actions(status="done", limit=100)]


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

def test_send_discord_dm_persists_done_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Roster gate: simulate a registered Discord person.
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: object(),
    )
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value=None),
    ):
        result = asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "discord-123", "text": "ping — board prep?"}
            )
        )
    assert json.loads(result)["status"] == "sent"

    rows = _done_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "discord_dm"
    assert row.channel_ref == "discord-123"
    assert row.status == "done"
    assert "ping" in row.intent_text


def test_send_discord_dm_failure_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: object(),
    )
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(side_effect=RuntimeError("discord 5xx")),
    ):
        result = asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "discord-123", "text": "should fail"}
            )
        )
    assert "error" in json.loads(result)
    assert _done_rows() == []


def test_send_discord_dm_roster_gate_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrostered recipient: roster gate refuses BEFORE send. No row written."""
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: None,
    )
    result = asyncio.run(
        schedule_tools.handle_send_discord_dm(
            {"discord_user_id": "discord-stranger", "text": "hi"}
        )
    )
    assert "error" in json.loads(result)
    assert _done_rows() == []


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #

def test_send_slack_dm_persists_done_row() -> None:
    sent: list[dict] = []

    class _FakeClient:
        def __init__(self, token: str) -> None:
            self.token = token

        async def chat_postMessage(self, **kw: object) -> dict:
            sent.append(kw)
            return {"ok": True, "ts": "1.0"}

    with patch(
        "slack_sdk.web.async_client.AsyncWebClient", _FakeClient
    ):
        result = asyncio.run(
            schedule_tools.handle_send_slack_dm(
                {"user_id": "U123", "text": "huddle in 10?"}
            )
        )
    assert json.loads(result)["status"] == "sent"
    # No previews: Slack would fetch a link in the text (reminders ride this DM).
    assert sent[0]["unfurl_links"] is False and sent[0]["unfurl_media"] is False

    rows = _done_rows()
    assert len(rows) == 1
    assert rows[0].channel == "slack_dm"
    assert rows[0].channel_ref == "U123"
    assert rows[0].status == "done"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def test_send_telegram_message_persists_done_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_telegram_chat_id",
        lambda _ref: object(),
    )
    with patch(
        "openexecutive.integrations.telegram_bot.send_message",
        new=AsyncMock(return_value=None),
    ):
        result = asyncio.run(
            schedule_tools.handle_send_telegram_message(
                {"chat_id": 42, "text": "you free at 3?"}
            )
        )
    assert json.loads(result)["status"] == "sent"

    rows = _done_rows()
    assert len(rows) == 1
    assert rows[0].channel == "telegram"
    assert rows[0].channel_ref == "42"
    assert rows[0].status == "done"


# --------------------------------------------------------------------------- #
# Failure isolation: a write that raises must not break the send response.
# --------------------------------------------------------------------------- #

def test_persist_failure_does_not_break_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: object(),
    )
    # Force the persistence helper's insert to blow up.
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_scheduled_action",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value=None),
    ):
        result = asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "discord-123", "text": "still works"}
            )
        )
    payload = json.loads(result)
    assert payload["status"] == "sent"


def test_correlated_persist_and_audit_failure_does_not_break_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk full hits both SQLite writes (the insert AND the audit log).
    The outer guard must catch the failure-path audit_log exception so
    the send still returns 'sent'.
    """
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: object(),
    )
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_scheduled_action",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("disk full (insert)")),
    )
    # Only fail audit_log when the helper's failure branch calls it
    # (the success-path tool_invocation audit at the call site is
    # unaffected — that's a separate pre-existing failure mode the
    # outer guard doesn't need to defend).
    import openexecutive.audit as audit_mod
    real_log = audit_mod.log_event

    def _conditional_log(event_type: str, message: str, **kw):  # type: ignore[no-untyped-def]
        if "Failed to record done" in message:
            raise RuntimeError("disk full (audit)")
        return real_log(event_type, message, **kw)

    monkeypatch.setattr("openexecutive.audit.log_event", _conditional_log)
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value=None),
    ):
        result = asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "discord-123", "text": "still works"}
            )
        )
    assert json.loads(result)["status"] == "sent"


# --------------------------------------------------------------------------- #
# Atomicity: the row must land directly as status='done' so the scheduler
# runner's claim_due_actions can't race in and re-dispatch.
# --------------------------------------------------------------------------- #

def test_send_lands_directly_as_done_no_pending_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the send returns, NO pending row exists for the channel — the
    insert wrote status='done' atomically. If the helper used a two-step
    insert→mark_done pattern, a concurrent claim_due_actions could grab
    the pending row whose run_at is already in the past and re-dispatch
    the same DM. This test guards that invariant.
    """
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: object(),
    )
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value=None),
    ):
        asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "discord-9", "text": "no double-dispatch"}
            )
        )

    pending = list_scheduled_actions(status="pending", limit=100)
    assert pending == []
    done = list_scheduled_actions(status="done", limit=100)
    assert len(done) == 1
    assert done[0].status == "done"


# --------------------------------------------------------------------------- #
# insert_scheduled_action's new status kwarg: contract-level tests.
# --------------------------------------------------------------------------- #

def test_insert_scheduled_action_rejects_invalid_status() -> None:
    from openexecutive.memory.episodic import insert_scheduled_action

    with pytest.raises(ValueError, match="insert status"):
        insert_scheduled_action(
            run_at="2026-01-01T00:00:00+00:00",
            channel="discord_dm",
            channel_ref="x",
            intent_text="x",
            status="running",  # only pending/done allowed at insert time
        )


def test_insert_scheduled_action_default_status_unchanged() -> None:
    """Existing callers (no status kwarg) must still produce pending rows."""
    from openexecutive.memory.episodic import insert_scheduled_action

    insert_scheduled_action(
        run_at="2099-01-01T00:00:00+00:00",
        channel="discord_dm",
        channel_ref="legacy",
        intent_text="default-status row",
    )
    pending = list_scheduled_actions(status="pending", limit=100)
    assert len(pending) == 1
    assert pending[0].channel_ref == "legacy"
