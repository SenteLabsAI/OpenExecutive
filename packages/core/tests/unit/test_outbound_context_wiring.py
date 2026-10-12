"""Wiring tests: outbound sends write linkage rows (only from a live session),
and the Discord inbound DM path hydrates the model turn from them.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.integrations.discord_bot import _handle_message
from openexecutive.memory.episodic import find_open_outbound_context, initialize_db
from openexecutive.orchestrator import schedule_tools
from openexecutive.orchestrator.schedule_tools import current_session


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def settings_with_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    class _S:
        slack_bot_token = "xoxb-test"
        discord_bot_token = "discord-test"
        telegram_bot_token = "tg-test"
        knowledge_chat_max_chars = 6000

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _S())


@pytest.fixture(autouse=True)
def reset_session() -> None:
    token = current_session.set(None)
    yield
    current_session.reset(token)


def _find(isolated_db: Path):
    return find_open_outbound_context(
        channel="discord_dm", channel_ref="alex-123",
        within=timedelta(hours=72), db_path=isolated_db,
    )


def test_send_writes_linkage_when_session_active(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: MagicMock(id=42),
    )
    current_session.set(MagicMock(session_id="discord:dm:principal-1"))

    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value="discord-msg-1"),
    ):
        asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "alex-123", "text": "Hey Alex, what happened with the deck?"}
            )
        )

    row = _find(isolated_db)
    assert row is not None
    assert row.originating_session_id == "discord:dm:principal-1"
    # Full text is stored (not truncated to the 160-char activity-feed limit).
    assert row.outbound_text == "Hey Alex, what happened with the deck?"
    assert row.recipient_person_id == 42
    assert row.outbound_message_id == "discord-msg-1"


@pytest.mark.parametrize(("speaker", "kept"), [(7, False), (42, True)])
def test_a_teammates_chat_is_not_quoted_to_someone_else(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch, speaker: int, kept: bool
) -> None:
    # A teammate's own conversation is theirs alone: their message to
    # someone else is linked, but with no excerpt of their chat.
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: MagicMock(id=42),
    )
    monkeypatch.setattr(
        "openexecutive.orchestrator.people_tools.teammate_turn_owner", lambda _s: speaker
    )
    current_session.set(MagicMock(session_id="slack:dm:U_BEN"))

    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value="discord-msg-1"),
    ):
        asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "alex-123", "text": "Ben asks whether Q3 is final"}
            )
        )

    row = _find(isolated_db)
    assert row is not None
    assert row.outbound_text == "Ben asks whether Q3 is final"
    assert row.originating_session_id == ("slack:dm:U_BEN" if kept else None)


def test_send_writes_no_linkage_without_session(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id",
        lambda _id: MagicMock(id=42),
    )
    # current_session stays None (reset_session fixture) — e.g. a scheduler-
    # initiated send. No originating conversation, so no linkage.
    with patch(
        "openexecutive.integrations.discord_bot.send_dm",
        new=AsyncMock(return_value="discord-msg-1"),
    ):
        asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "alex-123", "text": "proactive ping"}
            )
        )

    assert _find(isolated_db) is None


@pytest.mark.asyncio
async def test_discord_dm_inbound_hydrates_from_linkage() -> None:
    """A DM reply is passed to Executive.chat with the hydration helper's
    output, while the pristine text is persisted."""
    send_fn = AsyncMock()
    session_mock = MagicMock(seen_channel_refs=set())

    def _fake_hydrate(*, channel: str, channel_ref: str, user_message: str) -> str:
        assert channel == "discord_dm"
        assert channel_ref == "alex-123"
        return f"<outbound_reply_context>…</outbound_reply_context>\n\n{user_message}"

    with (
        patch("openexecutive.people.store.find_person_by_discord_id", return_value=MagicMock(id=42)),
        patch("openexecutive.alerts.pipeline.schedule_evaluation"),
        patch("openexecutive.knowledge.retriever.retrieve", return_value=""),
        patch("openexecutive.memory.episodic.format_for_prompt", return_value=""),
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile") as mock_profile,
        patch("openexecutive.orchestrator.executive.Executive") as MockExec,
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
        patch("openexecutive.orchestrator.session.Session", return_value=session_mock),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.memory.session_store.load_messages", return_value=[]),
        patch("openexecutive.memory.session_store.create_session"),
        patch("openexecutive.memory.session_store.save_message") as mock_save,
        patch("openexecutive.memory.session_store.update_session_timestamp"),
        patch(
            "openexecutive.integrations.inbound_hydration.hydrate_user_message",
            side_effect=_fake_hydrate,
        ),
    ):
        mock_profile.return_value.is_empty.return_value = True
        mock_exec_instance = MagicMock()
        mock_exec_instance.chat = AsyncMock(return_value="response")
        MockExec.return_value = mock_exec_instance

        await _handle_message(
            text="oh that, sorry",
            discord_user_id="alex-123",
            discord_channel="dm-chan",
            message_id="m1",
            thread_id=None,
            send_fn=send_fn,
            is_dm=True,
            session_id="discord:dm:alex-123",
            session_title="Alex",
        )

    # Executive.chat received the hydrated message.
    chat_kwargs = mock_exec_instance.chat.call_args.kwargs
    assert chat_kwargs["user_message"].startswith("<outbound_reply_context>")
    assert chat_kwargs["user_message"].endswith("oh that, sorry")

    # The persisted user turn is the pristine text, NOT the hydration block,
    # so the one-shot context can't leak into future history.
    user_saves = [
        c for c in mock_save.call_args_list
        if len(c.args) >= 2 and c.args[1] == "user"
    ]
    assert user_saves, "expected a user message to be persisted"
    assert user_saves[0].args[2] == "oh that, sorry"
