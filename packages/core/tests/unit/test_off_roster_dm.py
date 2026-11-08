"""A direct message to someone not in People: below Hands-off it's an Approve
card for the owner; on Hands-off it goes. A turn about the principal's
private mail still reaches the principal only."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.orchestrator import levels, schedule_tools


class _Settings:
    telegram_bot_token = "tg-test"
    slack_bot_token = "xoxb-test"
    discord_bot_token = "discord-test"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(schedule_tools, "_record_send_to_activity", lambda **_: None)
    monkeypatch.setattr(schedule_tools, "_record_outbound_context", lambda **_: None)
    monkeypatch.setattr(schedule_tools, "_recover_channel_id_from_person_id", lambda *_a: None)
    monkeypatch.setattr(schedule_tools, "_guard_outbound", lambda **_: None)
    monkeypatch.setattr(
        "openexecutive.orchestrator.people_tools.contacts_reachable_now", lambda: False
    )
    monkeypatch.setattr("openexecutive.orchestrator.people_tools.private_turn_holds", lambda: False)
    for finder in ("find_person_by_telegram_chat_id", "find_person_by_slack_id", "find_person_by_discord_id"):
        monkeypatch.setattr(f"openexecutive.people.store.{finder}", lambda *_a, **_k: None)


def _send(tool: str) -> tuple[str, AsyncMock]:
    """Run ``tool`` to an id nobody in People has; returns the result and the send."""
    if tool == "send_telegram_message":
        send = AsyncMock(return_value="42")
        with patch("openexecutive.integrations.telegram_bot.send_message", new=send):
            out = asyncio.run(schedule_tools.handle_send_telegram_message({"chat_id": 999, "text": "hi"}))
        return out, send
    if tool == "send_discord_dm":
        send = AsyncMock(return_value=None)
        with patch("openexecutive.integrations.discord_bot.send_dm", new=send):
            out = asyncio.run(
                schedule_tools.handle_send_discord_dm({"discord_user_id": "999999", "text": "hi"})
            )
        return out, send
    send = AsyncMock(return_value={"ok": True, "ts": "1.2"})
    client = MagicMock()
    client.return_value.chat_postMessage = send
    with patch("slack_sdk.web.async_client.AsyncWebClient", client):
        out = asyncio.run(schedule_tools.handle_send_slack_dm({"user_id": "U999", "text": "hi"}))
    return out, send


TOOLS = ["send_telegram_message", "send_slack_dm", "send_discord_dm"]


@pytest.mark.parametrize("tool", TOOLS)
def test_below_hands_off_an_off_roster_dm_is_a_card(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    cards: list[tuple[str, str]] = []

    def card(blocked: str, name: str, _args: dict[str, Any], *, reason: str, mcp: bool = True) -> str:
        cards.append((name, reason))
        return json.dumps({"status": "waiting_for_approval"})

    monkeypatch.setattr(levels, "relaxed", lambda: False)
    monkeypatch.setattr(levels, "card_or", card)
    out, send = _send(tool)
    assert json.loads(out)["status"] == "waiting_for_approval"
    assert cards == [(tool, levels.NEW_PERSON)]
    send.assert_not_awaited()


@pytest.mark.parametrize("tool", TOOLS)
def test_on_hands_off_an_off_roster_dm_goes(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.setattr(levels, "relaxed", lambda: True)
    monkeypatch.setattr(levels, "card_or", lambda *a, **k: pytest.fail("no card on Hands-off"))
    out, send = _send(tool)
    assert json.loads(out)["status"] == "sent"
    send.assert_awaited_once()


def test_a_private_mail_turn_reaches_the_principal_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even on Hands-off, a turn about the principal's own mail can't DM
    someone else on Slack."""
    from openexecutive.orchestrator.people_tools import PRIVATE_TURN_REFUSAL

    teammate = MagicMock(is_principal=False)
    monkeypatch.setattr(levels, "relaxed", lambda: True)
    monkeypatch.setattr("openexecutive.orchestrator.people_tools.private_turn_holds", lambda: True)
    monkeypatch.setattr("openexecutive.people.store.find_person_by_slack_id", lambda *_a, **_k: teammate)
    out, send = _send("send_slack_dm")
    assert json.loads(out)["error"] == PRIVATE_TURN_REFUSAL
    send.assert_not_awaited()
