"""create_alert in the principal's own live chat.

A question for the person who is in the chat goes in the reply, not on a card
they find later (and that review may never show). Cards for anyone else, or
raised off a live chat, are untouched. And the handler never lets the reply
say a card is waiting when review may still drop it.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator import alert_tools
from openexecutive.orchestrator.alert_tools import handle_create_alert
from openexecutive.orchestrator.schedule_tools import current_session

QUESTION = {
    "subject": "Which plan should the launch email lead with?",
    "body": "The draft names two plans.",
    "needs": "answer",
    "question": "Which plan should the launch email lead with?",
}


def _session(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "origin_channel": "web",
        "shared_chat": False,
        "caller_person_id": 1,
        "private_to_principal": False,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _call(
    tool_input: dict[str, Any], session: Any, *, principal: bool = True,
) -> tuple[dict[str, Any], list[Any]]:
    scheduled: list[Any] = []
    token = current_session.set(session)
    try:
        with (
            patch("openexecutive.alerts.pipeline.schedule_evaluation", scheduled.append),
            patch("openexecutive.audit.log_event"),
            patch(
                "openexecutive.orchestrator.content_trust.principal_speaking",
                lambda _s: principal,
            ),
            patch.object(alert_tools, "_team_member_raising", lambda _s: None),
            patch(
                "openexecutive.orchestrator.outside_content.answers_off_team_mail",
                lambda: False,
            ),
        ):
            out = json.loads(asyncio.run(handle_create_alert(dict(tool_input))))
    finally:
        current_session.reset(token)
    return out, scheduled


@pytest.mark.parametrize("needs", ["answer", "choice"])
def test_question_for_the_principal_in_their_chat_goes_in_the_reply(needs: str) -> None:
    out, scheduled = _call({**QUESTION, "needs": needs, "choices": ["Starter", "Growth"]}, _session())

    assert out["status"] == "ask_in_reply"
    assert "ask them in your reply" in out["message"]
    assert scheduled == []


@pytest.mark.parametrize(
    ("tool_input", "session", "principal"),
    [
        # Not a question: a note to look at later is still a card.
        ({**QUESTION, "needs": "fyi"}, _session(), True),
        ({k: v for k, v in QUESTION.items() if k != "needs"}, _session(), True),
        # Someone else answers it.
        ({**QUESTION, "assigned_to_person_id": 7}, _session(), True),
        ({**QUESTION, "department": "finance"}, _session(), True),
        # Not the principal live in the chat.
        (QUESTION, _session(), False),
        (QUESTION, _session(origin_channel="email"), True),
        (QUESTION, _session(shared_chat=True), True),
    ],
)
def test_other_cards_are_still_raised(
    tool_input: dict[str, Any], session: Any, principal: bool,
) -> None:
    out, scheduled = _call(tool_input, session, principal=principal)

    assert out["status"] == "alert_scheduled"
    assert len(scheduled) == 1


def test_a_workflow_step_still_raises_its_question() -> None:
    with alert_tools.raised_for_workflow_owner(1):
        out, scheduled = _call(QUESTION, _session())

    assert out["status"] == "alert_scheduled"
    assert len(scheduled) == 1


def test_routed_back_to_the_speaker_still_counts_as_asking_them() -> None:
    out, scheduled = _call({**QUESTION, "assigned_to_person_id": 1}, _session(caller_person_id=1))

    assert out["status"] == "ask_in_reply"
    assert scheduled == []


def test_a_raised_card_is_not_promised() -> None:
    out, _ = _call({**QUESTION, "needs": "fyi"}, _session())

    assert "don't tell them a card is waiting" in out["message"]
