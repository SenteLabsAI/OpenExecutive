"""Unit tests for `message_person` — the server-side recipient-resolving DM tool.

The Executive kept fabricating / mis-copying channel ids when handed raw
send_*_dm tools. `message_person(person_id, text)` removes that failure mode:
the model passes only a person_id and the server resolves the person's
configured channel + real channel id, delegating to the matching send handler.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.orchestrator.schedule_tools import handle_message_person
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


def _settings(*, slack=None, discord=None, telegram=None) -> SimpleNamespace:
    return SimpleNamespace(
        slack_bot_token=slack,
        discord_bot_token=discord,
        telegram_bot_token=telegram,
        calendar_booking_enabled=False,
        mcp_enabled=False,
    )


def _call(payload: dict) -> dict:
    return json.loads(asyncio.run(handle_message_person(payload)))


def test_message_person_routes_to_real_discord_id() -> None:
    """The reported QA bug: model never has to supply a Discord id. It passes
    person_id; the server sends to the person's real discord snowflake."""
    pid = people_store.upsert_person(
        full_name="Dana Ops",
        discord_user_id="100000000000000001",
        preferred_channel="discord",
    )
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(discord="tok")),
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "heads up"})
    assert result["status"] == "sent"
    fake_send.assert_awaited_once_with("100000000000000001", "heads up")


def test_message_person_honours_preferred_channel() -> None:
    """When a person has several channels, preferred_channel wins."""
    pid = people_store.upsert_person(
        full_name="Tess Telegram",
        discord_user_id="100000000000000001",
        telegram_chat_id="555123",
        preferred_channel="telegram",
    )
    fake_tg = AsyncMock(return_value=None)
    fake_discord = AsyncMock(return_value=None)
    with (
        patch(
            "openexecutive.config.get_settings",
            return_value=_settings(discord="tok", telegram="tok"),
        ),
        patch("openexecutive.integrations.telegram_bot.send_message", fake_tg),
        patch("openexecutive.integrations.discord_bot.send_dm", fake_discord),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert result["status"] == "sent"
    fake_tg.assert_awaited_once()  # telegram (preferred) used
    fake_discord.assert_not_awaited()


def test_message_person_falls_back_when_preferred_not_configured() -> None:
    """Preferred channel unconfigured on the deployment → fall back to another
    channel the person has that IS configured."""
    pid = people_store.upsert_person(
        full_name="Sam Slackless",
        slack_user_id="U047QN4EH",  # prefers slack...
        discord_user_id="100000000000000001",  # ...but only discord is configured
        preferred_channel="slack",
    )
    fake_send = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(discord="tok")),
        patch("openexecutive.integrations.discord_bot.send_dm", fake_send),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert result["status"] == "sent"
    fake_send.assert_awaited_once_with("100000000000000001", "hi")


def test_message_person_no_reachable_channel_falls_back_to_alert() -> None:
    """Person only has a Slack id but Slack isn't configured → no reachable
    channel. Don't drop the finding: create a briefing alert assigned to them."""
    pid = people_store.upsert_person(full_name="Only Slack", slack_user_id="U047QN4EH")
    fake_alert = AsyncMock(return_value="{}")
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(discord="tok")),
        patch("openexecutive.orchestrator.alert_tools.handle_create_alert", fake_alert),
    ):
        result = _call({"person_id": pid, "text": "Heads up on X"})
    assert result["status"] == "alerted"
    assert result["person_id"] == pid
    fake_alert.assert_awaited_once()
    # The alert is routed to the intended person.
    assert fake_alert.await_args.args[0]["assigned_to_person_id"] == pid
    # The card carries the message as written, not a triage rewrite.
    assert fake_alert.await_args.kwargs == {"as_written": True}


def test_message_person_unknown_person_refused() -> None:
    with patch(
        "openexecutive.config.get_settings", return_value=_settings(discord="tok")
    ):
        result = _call({"person_id": 99999, "text": "hi"})
    assert "error" in result
    assert "roster" in result["error"].lower()


def test_message_person_archived_refused() -> None:
    pid = people_store.upsert_person(
        full_name="Gone", discord_user_id="100000000000000001"
    )
    people_store.archive_person(pid)
    with patch(
        "openexecutive.config.get_settings", return_value=_settings(discord="tok")
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert "error" in result
    assert "roster" in result["error"].lower()


def test_message_person_bad_args_refused() -> None:
    assert "error" in _call({"text": "hi"})  # missing person_id
    pid = people_store.upsert_person(
        full_name="X", discord_user_id="100000000000000001"
    )
    with patch(
        "openexecutive.config.get_settings", return_value=_settings(discord="tok")
    ):
        assert "error" in _call({"person_id": pid, "text": "   "})  # empty text


def test_message_person_skips_malformed_channel_id_and_falls_back() -> None:
    """A malformed stored telegram id (preferred) is skipped, not handed to the
    delegate as a misleading error — fall back to a usable channel."""
    pid = people_store.upsert_person(
        full_name="Bad TG",
        telegram_chat_id="@notnumeric",  # preferred but malformed
        discord_user_id="100000000000000001",
        preferred_channel="telegram",
    )
    fake_discord = AsyncMock(return_value=None)
    fake_tg = AsyncMock(return_value=None)
    with (
        patch(
            "openexecutive.config.get_settings",
            return_value=_settings(discord="tok", telegram="tok"),
        ),
        patch("openexecutive.integrations.discord_bot.send_dm", fake_discord),
        patch("openexecutive.integrations.telegram_bot.send_message", fake_tg),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert result["status"] == "sent"
    fake_tg.assert_not_awaited()  # malformed telegram id skipped
    fake_discord.assert_awaited_once_with("100000000000000001", "hi")


def test_message_person_sends_to_valid_negative_telegram_group_id() -> None:
    """A legitimate negative Telegram group id (single leading '-') must send."""
    pid = people_store.upsert_person(
        full_name="Group Chat",
        telegram_chat_id="-1001234567890",
        preferred_channel="telegram",
    )
    fake_tg = AsyncMock(return_value=None)
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(telegram="tok")),
        patch("openexecutive.integrations.telegram_bot.send_message", fake_tg),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert result["status"] == "sent"
    fake_tg.assert_awaited_once()


def test_message_person_skips_double_dash_telegram_id() -> None:
    """A malformed '--123' chat id (passes a naive lstrip check but breaks
    int()) must be skipped, not handed to the delegate."""
    pid = people_store.upsert_person(
        full_name="Bad Dashes",
        telegram_chat_id="--123",
        preferred_channel="telegram",
    )
    fake_alert = AsyncMock(return_value="{}")
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(telegram="tok")),
        patch("openexecutive.orchestrator.alert_tools.handle_create_alert", fake_alert),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    # Malformed chat id is skipped (not sent); falls back to an alert.
    assert result["status"] == "alerted"
    fake_alert.assert_awaited_once()


def test_message_person_bad_args_error_names_person_id() -> None:
    """Missing person_id must return a self-correcting error so the synthesis
    model retries with a person_id from the roster (it kept omitting it)."""
    result = _call({"text": "hi"})
    assert "error" in result
    assert "person_id" in result["error"]


def test_message_person_falls_back_across_channels_on_send_failure() -> None:
    """A send failure on the preferred channel (e.g. Discord 403) must fall
    through to the next configured channel, not surface as the tool's error."""
    pid = people_store.upsert_person(
        full_name="Multi Chan",
        discord_user_id="100000000000000001",
        telegram_chat_id="555123",
        preferred_channel="discord",
    )
    failing_discord = AsyncMock(side_effect=RuntimeError("403 Forbidden"))
    ok_tg = AsyncMock(return_value=None)
    with (
        patch(
            "openexecutive.config.get_settings",
            return_value=_settings(discord="tok", telegram="tok"),
        ),
        patch("openexecutive.integrations.discord_bot.send_dm", failing_discord),
        patch("openexecutive.integrations.telegram_bot.send_message", ok_tg),
        patch("openexecutive.audit.log_event"),
        patch("openexecutive.orchestrator.schedule_tools._record_send_to_activity"),
    ):
        result = _call({"person_id": pid, "text": "hi"})
    assert result["status"] == "sent"   # delivered via the telegram fallback
    ok_tg.assert_awaited_once()


def test_message_person_registered_in_toolkit() -> None:
    from openexecutive.orchestrator.schedule_tools import (
        SCHEDULE_TOOL_HANDLERS,
        SCHEDULE_TOOLS,
    )

    assert "message_person" in {t["name"] for t in SCHEDULE_TOOLS}
    assert "message_person" in SCHEDULE_TOOL_HANDLERS
