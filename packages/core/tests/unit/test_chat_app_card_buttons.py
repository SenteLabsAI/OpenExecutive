"""Approve and Skip buttons on cards in a Slack or Discord DM
(integrations/channel_cards.py, slack_bot.handle_card_tap,
discord_bot.handle_card_tap)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import channel_cards, discord_bot, slack_bot
from openexecutive.orchestrator.session import Session

PERSON = SimpleNamespace(id=1, email="olivia@co.example")


@pytest.fixture(autouse=True)
def chat_apps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Any, bool]]:

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CARD_BUTTONS", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-test")
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    resolved: list[tuple[int, Any, bool]] = []

    async def resolve(decision_id: int, person: Any, *, approve: bool) -> str:
        resolved.append((decision_id, person, approve))
        return "Approved." if approve else "Skipped."

    monkeypatch.setattr(channel_cards, "resolve", resolve)
    yield resolved


def _dm(channel: str, session_id: str, person_id: int | None = 1) -> Session:
    return Session(session_id=session_id, origin_channel=channel, origin_channel_ref="U1",
                   caller_person_id=person_id)


def test_slack_and_discord_dms_show_cards(monkeypatch: pytest.MonkeyPatch) -> None:

    assert channel_cards.shows_cards(_dm("slack", "slack:dm:U1"))
    assert channel_cards.shows_cards(_dm("discord", "discord:dm:42"))
    # A channel or thread is everyone in it; nobody on the roster proves nothing.
    assert not channel_cards.shows_cards(_dm("slack", "slack:C1:123.4"))
    assert not channel_cards.shows_cards(_dm("discord", "discord:thread:9"))
    assert not channel_cards.shows_cards(_dm("slack", "slack:dm:U1", person_id=None))
    # Slack needs Interactivity, so the buttons wait for SLACK_CARD_BUTTONS.
    monkeypatch.setenv("SLACK_CARD_BUTTONS", "false")
    assert not channel_cards.shows_cards(_dm("slack", "slack:dm:U1"))


class FakeSlack:
    def __init__(self, dm: str = "D1") -> None:
        self.dm = dm
        self.updates: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    async def conversations_open(self, users: str) -> dict[str, Any]:
        return {"channel": {"id": self.dm}}

    async def chat_update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    async def chat_postMessage(self, **kwargs: Any) -> None:  # noqa: N802 - Slack's name
        self.posts.append(kwargs)


def _slack_tap(channel: str = "D1", value: str = "card:7:a") -> dict[str, Any]:
    return {
        "user": {"id": "U1"}, "channel": {"id": channel},
        "container": {"message_ts": "111.2"},
        "message": {"ts": "111.2", "blocks": slack_bot._card_blocks("Change the workflow", 7)},
        "actions": [{"action_id": "oe_card_approve", "value": value}],
    }


def test_a_slack_tap_in_its_own_dm_decides_the_card(
    chat_apps: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.people.store.find_person_by_slack_id", lambda uid: PERSON)
    client = FakeSlack()
    asyncio.run(slack_bot.handle_card_tap(_slack_tap(), client))
    assert chat_apps == [(7, PERSON, True)]
    update = client.updates[0]
    assert update["ts"] == "111.2" and "Approved." in update["text"]
    assert [b["type"] for b in update["blocks"]] == ["section"]  # no buttons left


def test_a_slack_tap_anywhere_else_is_ignored(chat_apps: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.people.store.find_person_by_slack_id", lambda uid: PERSON)
    # A channel, or a DM that isn't this user's with the bot.
    asyncio.run(slack_bot.handle_card_tap(_slack_tap(channel="C1"), FakeSlack()))
    asyncio.run(slack_bot.handle_card_tap(_slack_tap(channel="D9"), FakeSlack(dm="D1")))
    asyncio.run(slack_bot.handle_card_tap(_slack_tap(value="junk"), FakeSlack()))
    monkeypatch.setattr("openexecutive.people.store.find_person_by_slack_id", lambda uid: None)
    asyncio.run(slack_bot.handle_card_tap(_slack_tap(), FakeSlack()))
    assert chat_apps == []


def test_slack_cards_go_out_as_plain_text_with_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel_cards, "card_text", lambda decision_id: f"*card* {decision_id}")
    client = FakeSlack()
    asyncio.run(slack_bot._send_cards(client, "D1", [3]))
    blocks = client.posts[0]["blocks"]
    assert blocks[0]["text"]["type"] == "plain_text"
    assert [e["value"] for e in blocks[1]["elements"]] == ["card:3:a", "card:3:s"]


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    async def send_message(self, text: str, **_: Any) -> None:
        self.sent.append(text)

    async def defer(self) -> None:
        self.deferred = True


def _discord_tap(dm: bool = True, recipient: str = "42", custom_id: str = "card:7:s") -> Any:
    import discord

    edits: list[dict[str, Any]] = []
    channel: Any
    if dm:
        channel = discord.DMChannel.__new__(discord.DMChannel)
        channel.recipients = [SimpleNamespace(id=int(recipient))]
    else:
        channel = SimpleNamespace()

    async def edit_original_response(**kwargs: Any) -> None:
        edits.append(kwargs)

    return SimpleNamespace(
        data={"custom_id": custom_id}, user=SimpleNamespace(id=42), channel=channel,
        message=SimpleNamespace(content="Change the workflow"), response=FakeResponse(),
        edit_original_response=edit_original_response, edits=edits,
    )


def test_a_discord_tap_in_its_own_dm_decides_the_card(
    chat_apps: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.people.store.find_person_by_discord_id", lambda uid: PERSON)
    tap = _discord_tap()
    asyncio.run(discord_bot.handle_card_tap(tap))
    assert chat_apps == [(7, PERSON, False)]
    assert tap.response.deferred and tap.edits[0]["view"] is None
    assert "Skipped." in tap.edits[0]["content"]


def test_a_discord_tap_anywhere_else_is_turned_away(
    chat_apps: list[Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openexecutive.people.store.find_person_by_discord_id", lambda uid: PERSON)
    for tap in (_discord_tap(dm=False), _discord_tap(recipient="99")):
        asyncio.run(discord_bot.handle_card_tap(tap))
        assert tap.response.sent == ["This card is no longer there."]
    other = _discord_tap(custom_id="something_else")
    asyncio.run(discord_bot.handle_card_tap(other))
    assert other.response.sent == [] and chat_apps == []
