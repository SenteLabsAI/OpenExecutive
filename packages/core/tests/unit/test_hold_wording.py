"""The holds a person lifts by asking again tell the model to word the reply
as a yes with a reason, never "I can't", and never to promise it early."""

from __future__ import annotations

import json

import pytest

from openexecutive.delegation import lockdown
from openexecutive.orchestrator import outside_content, schedule_tools
from openexecutive.prompts.executive_persona import DELEGATION_ADDENDUM


@pytest.mark.parametrize(
    "text",
    [
        lockdown.REFUSAL,
        outside_content.REFUSAL,
        outside_content.ADDED_REFUSAL,
        schedule_tools.SHARED_CHAT_REFUSAL,
    ],
)
def test_each_hold_asks_for_a_yes_not_i_cant(text: str) -> None:
    assert "never as \"I can't\"" in text
    assert "safety rules" in text
    assert "never say it is done" in text.lower()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (lockdown.REFUSAL, "read their mail"),
        (outside_content.REFUSAL, "read something from outside"),
        (outside_content.ADDED_REFUSAL, "new contact"),
        (schedule_tools.SHARED_CHAT_REFUSAL, "others have written here"),
    ],
)
def test_each_hold_gives_its_reason(text: str, reason: str) -> None:
    assert reason in text


def test_the_tool_errors_carry_the_wording() -> None:
    for error in (
        lockdown.mail_touched_withheld_error("fetch_url"),
        outside_content.added_person_error("person_id 4"),
    ):
        assert "just say \\\"go\\\"" in json.dumps(json.loads(error)["error"])


def test_the_persona_lets_approvals_decide_after_reading_mail() -> None:
    """After reading their mail a step goes or waits on a card as Approvals
    decides: the persona never asks them to say go or ask again."""
    assert "Approvals decides whether a step goes at once or waits on an Approve card" in DELEGATION_ADDENDUM
    assert 'never ask them to say "go" or ask again' in DELEGATION_ADDENDUM
