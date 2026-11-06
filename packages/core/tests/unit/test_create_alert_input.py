"""create_alert called without the fields its schema requires.

A chat script or saved workflow calls the handler directly, past the tool
schema, so a missing subject or body has to come back as an error the caller
can fix rather than a KeyError.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator.alert_tools import handle_create_alert


def _call(tool_input: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    scheduled: list[Any] = []
    with (
        patch("openexecutive.alerts.pipeline.schedule_evaluation", scheduled.append),
        patch("openexecutive.audit.log_event"),
    ):
        out = json.loads(asyncio.run(handle_create_alert(tool_input)))
    return out, scheduled


@pytest.mark.parametrize(
    ("tool_input", "named"),
    [
        ({"body": "Server is down"}, "subject"),
        ({"title": "Server down", "body": "Server is down"}, "subject"),
        ({"subject": "Server down"}, "body"),
        ({"subject": "  ", "body": ""}, "subject and body"),
    ],
)
def test_missing_field_is_named_and_nothing_is_raised(
    tool_input: dict[str, Any], named: str,
) -> None:
    out, scheduled = _call(tool_input)

    assert out["error"].startswith(f"create_alert needs {named}:")
    assert scheduled == []


def test_complete_input_still_schedules() -> None:
    out, scheduled = _call({"subject": "Server down", "body": "Server is down"})

    assert out["status"] == "alert_scheduled"
    assert len(scheduled) == 1


def test_mail_from_one_sender_shares_one_card() -> None:
    """Two emails from the same person, even on different threads or to
    different addresses, key to one open card; the follow-up adds to it."""
    from types import SimpleNamespace

    from openexecutive.alerts.models import AlertEvent
    from openexecutive.orchestrator.alert_tools import _one_card_per_sender

    def keyed(session: Any, raised_by: str = "") -> AlertEvent:
        event = AlertEvent.model_validate({
            "source": "email", "external_id": "m-1", "subject": "s", "body": "b", "raised_by": raised_by,
        })
        _one_card_per_sender(event, session)
        return event

    mail = SimpleNamespace(origin_channel="email", email_from="Casey@Example.com")
    first, second = keyed(mail), keyed(SimpleNamespace(origin_channel="email", email_from="casey@example.com"))
    assert first.dedup_hint == second.dedup_hint == "email-from:casey@example.com"
    assert first.follow_up is True
    # Chat turns, unknown senders and a teammate's own raise keep the model's key.
    assert keyed(SimpleNamespace(origin_channel="slack", email_from="")).dedup_hint == ""
    assert keyed(SimpleNamespace(origin_channel="email", email_from="")).dedup_hint == ""
    assert keyed(mail, raised_by="Dana").dedup_hint == ""
    # Mail routed to one person keeps a card apart from the shared one.
    routed = AlertEvent.model_validate({
        "source": "email", "external_id": "m-2", "subject": "s", "body": "b", "user": "person:7",
    })
    _one_card_per_sender(routed, mail)
    assert routed.dedup_hint == "email-from:casey@example.com:person:7"


def _sender_event(private: bool = False) -> Any:
    from openexecutive.alerts.models import AlertEvent

    return AlertEvent.model_validate({
        "source": "email", "external_id": "m-3", "subject": "s", "body": "b", "private": private,
        "dedup_hint": "email-from:casey@example.com", "follow_up": True,
    })


def _card(card_id: int, key: str, question: str = "") -> Any:
    from openexecutive.alerts.models import Alert

    return Alert.model_validate({
        "id": card_id, "source": "email", "external_id": f"x-{card_id}", "severity": "medium",
        "headline": f"Card {card_id}", "body": "b", "dedup_key": key, "question": question,
        "created_at": "2026-11-06T13:00:00+00:00",
    })


def test_a_second_ask_from_one_sender_is_asked_which_card_it_joins() -> None:
    """With the sender's card open, nothing is raised until the turn says
    whether this is the same matter: one answer never settles two asks."""
    from openexecutive.orchestrator.alert_tools import _sender_card_choice

    event = _sender_event()
    with patch(
        "openexecutive.alerts.store.open_alerts_keyed",
        return_value=[_card(227, "email-from:casey@example.com", "Approve the plan?")],
    ):
        out = _sender_card_choice(event, None)
    assert out is not None
    told = json.loads(out)
    assert told["status"] == "sender_has_open_card"
    assert told["open_cards"] == [{"id": 227, "headline": "Card 227"}]
    assert "Approve the plan?" in told["message"]


@pytest.mark.parametrize("private", [False, True])
def test_the_same_matter_joins_its_card_and_a_new_one_gets_its_own(private: bool) -> None:
    from openexecutive.orchestrator.alert_tools import _sender_card_choice

    prefix = "private:" if private else ""
    cards = [
        _card(227, f"{prefix}email-from:casey@example.com"),
        _card(230, f"{prefix}email-from:casey@example.com#ab12cd34"),
    ]
    with patch("openexecutive.alerts.store.open_alerts_keyed", return_value=cards):
        same = _sender_event(private)
        assert _sender_card_choice(same, 230) is None
        assert same.dedup_hint == "email-from:casey@example.com#ab12cd34"
        assert same.follow_up is True

        different = _sender_event(private)
        assert _sender_card_choice(different, 0) is None
        assert different.dedup_hint.startswith("email-from:casey@example.com#")
        assert different.dedup_hint not in {"email-from:casey@example.com#ab12cd34"}

        # An id that isn't one of the sender's open cards starts a card too.
        stray = _sender_event(private)
        assert _sender_card_choice(stray, 99) is None
        assert stray.dedup_hint.startswith("email-from:casey@example.com#")


def test_no_open_card_raises_straight_away() -> None:
    from openexecutive.orchestrator.alert_tools import _sender_card_choice

    event = _sender_event()
    with patch("openexecutive.alerts.store.open_alerts_keyed", return_value=[]):
        assert _sender_card_choice(event, None) is None
    assert event.dedup_hint == "email-from:casey@example.com"
