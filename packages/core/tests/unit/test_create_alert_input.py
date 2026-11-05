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
