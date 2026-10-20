"""Discord: someone off the roster writing to the bot is held and told.

A DM or a mention is held for the principal to confirm; the sender hears,
privately, that it arrived (in the DM, or by DM for a channel mention). A
thread the bot merely follows, and one of the principal's contacts, stay
silent as before.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.integrations import discord_bot


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)


def _handle(*, is_dm: bool, gate_eligible: bool = False) -> tuple[AsyncMock, AsyncMock]:
    hold = AsyncMock()
    send_fn = AsyncMock()
    with (
        patch("openexecutive.people.store.find_person_by_discord_id", return_value=None),
        patch.object(discord_bot, "_hold_unknown_sender", new=hold),
    ):
        asyncio.run(discord_bot._handle_message(
            text="hi, it's Annamarie", discord_user_id="42", discord_channel="C1",
            message_id="m1", thread_id="C1", send_fn=send_fn, is_dm=is_dm,
            session_id="discord:dm:42", session_title="Discord DM",
            author_display_name="Annamarie", gate_eligible=gate_eligible,
        ))
    send_fn.assert_not_awaited()  # no reply from the Executive
    return hold, send_fn


def test_a_dm_is_held_and_acknowledged_in_the_dm() -> None:
    hold, send_fn = _handle(is_dm=True)
    hold.assert_awaited_once()
    assert hold.await_args.kwargs["send_ack"] is send_fn
    assert hold.await_args.kwargs["text"] == "hi, it's Annamarie"


def test_a_mention_is_held_and_acknowledged_by_dm() -> None:
    hold, _send_fn = _handle(is_dm=False)
    hold.assert_awaited_once()
    assert hold.await_args.kwargs["send_ack"] is None  # _hold_unknown_sender DMs them


def test_a_followed_thread_is_not_held() -> None:
    hold, _ = _handle(is_dm=False, gate_eligible=True)
    hold.assert_not_awaited()


def _hold_kwargs(**over: Any) -> dict[str, Any]:
    return {
        "text": "hi", "discord_user_id": "42", "discord_channel": "C1", "message_id": "m1",
        "thread_id": None, "is_dm": False, "session_id": "s", "session_title": "t",
        "author_display_name": "Annamarie", "send_ack": None, **over,
    }


def test_a_channel_mention_is_acknowledged_by_dm_only() -> None:
    intake = AsyncMock()
    dm = AsyncMock()
    with (
        patch("openexecutive.integrations.roster_intake.intake", new=intake),
        patch("openexecutive.people.store.find_person_by_discord_id", return_value=None),
        patch.object(discord_bot, "send_dm", new=dm),
    ):
        asyncio.run(discord_bot._hold_unknown_sender(**_hold_kwargs()))
        assert intake.await_args.args == ("discord", "42")
        asyncio.run(intake.await_args.kwargs["send_ack"]("received"))
    dm.assert_awaited_once_with("42", "received")


def test_a_contact_is_not_held() -> None:
    intake = AsyncMock()
    with (
        patch("openexecutive.integrations.roster_intake.intake", new=intake),
        patch("openexecutive.people.store.find_person_by_discord_id", return_value=object()),
    ):
        asyncio.run(discord_bot._hold_unknown_sender(**_hold_kwargs()))
    intake.assert_not_awaited()


@pytest.mark.parametrize(("is_dm", "sender"), [(True, "send_dm"), (False, "send_channel_message")])
def test_a_held_message_is_replayed_where_it_came_from(is_dm: bool, sender: str) -> None:
    from openexecutive.people.roster_requests import HeldMessage

    handled = AsyncMock()
    out = AsyncMock()
    message = HeldMessage(id=1, request_id=1, external_id="m1", payload={
        "text": "hi", "discord_user_id": "42", "discord_channel": "C1", "message_id": "m1",
        "thread_id": None, "is_dm": is_dm, "session_id": "discord:dm:42",
        "session_title": "t", "author_display_name": "A",
    })
    with patch.object(discord_bot, "_handle_message", new=handled), \
         patch.object(discord_bot, sender, new=out):
        assert asyncio.run(discord_bot._replay_held(message, None)) is True
        asyncio.run(handled.await_args.kwargs["send_fn"]("the answer"))
    assert out.await_args.args[-1] == "the answer"
