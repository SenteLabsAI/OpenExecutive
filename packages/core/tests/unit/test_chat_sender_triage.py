"""Chat-app messages reach triage under the sender's roster name.

A chat app's name for someone is whatever they typed into it. Linked to a
person, the message is triaged under that person's name, so the principal
writing from an account called something else is not read as a stranger
(and then reported back to them in their own brief). The principal's own
messages to their Executive are not triaged at all.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.agents.triage import _format_event_block
from openexecutive.alerts.models import AlertEvent
from openexecutive.alerts.pipeline import schedule_chat_message_evaluation
from openexecutive.people.models import Person

DANA = Person(id=7, full_name="Dana Whitfield", telegram_chat_id="555")
PRINCIPAL = Person(id=1, full_name="Morgan Lee", is_principal=True, telegram_chat_id="111")


def _scheduled(sender: Person | None, fallback: str = "Captain Dana") -> list[AlertEvent]:
    events: list[AlertEvent] = []
    with patch("openexecutive.alerts.pipeline.schedule_evaluation", side_effect=events.append):
        schedule_chat_message_evaluation(
            source="telegram",
            external_id="9",
            body="Why did I get this on a Sunday?",
            sender=sender,
            fallback_name=fallback,
        )
    return events


def test_a_linked_teammate_is_named_by_their_people_record() -> None:
    [event] = _scheduled(DANA)
    assert event.user == "Dana Whitfield"
    assert event.body == "Why did I get this on a Sunday?"


def test_an_unlinked_sender_keeps_the_chat_apps_name() -> None:
    [event] = _scheduled(None)
    assert event.user == "Captain Dana"


def test_the_principals_own_message_is_not_triaged() -> None:
    assert _scheduled(PRINCIPAL) == []


def test_the_triage_block_labels_the_sender_without_naming_slack() -> None:
    block = _format_event_block(
        AlertEvent(source="telegram", external_id="9", body="hi", user="Dana Whitfield", channel="c1")
    )
    assert "sender: Dana Whitfield" in block
    assert "channel: c1" in block
    assert "slack_user" not in block and "slack_channel" not in block


def _run_telegram_turn(person: Person) -> list[AlertEvent]:
    """One Telegram message from a chat whose profile name differs from the
    linked person's, through the real handler up to the model call."""
    from openexecutive.integrations import telegram_bot

    events: list[AlertEvent] = []
    kwargs: dict[str, Any] = {
        "message_text": "Why did I get this on a Sunday?",
        "sender_name": "Captain Dana",
        "chat_id": int(person.telegram_chat_id or 0),
        "message_id": 9,
        "token": "t",
    }
    with (
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=person),
        patch(
            "openexecutive.workflows.inbound_resolver.resolve_and_acknowledge",
            new=AsyncMock(return_value=False),
        ),
        patch("openexecutive.alerts.pipeline.schedule_evaluation", side_effect=events.append),
        patch("openexecutive.integrations.conversations.conversation_for", return_value="telegram:x"),
        # Stop the turn right after triage was scheduled: the handler catches
        # this and sends its error reply, which is patched too.
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            side_effect=RuntimeError("stop"),
        ),
        patch.object(telegram_bot, "send_message", new=AsyncMock()),
        patch("openexecutive.audit.log_event"),
    ):
        asyncio.run(telegram_bot._process_and_reply.__wrapped__(**kwargs))
    return events


@pytest.mark.parametrize(("person", "expected"), [(DANA, ["Dana Whitfield"]), (PRINCIPAL, [])])
def test_telegram_triages_under_the_linked_persons_name(person: Person, expected: list[str]) -> None:
    assert [e.user for e in _run_telegram_turn(person)] == expected
