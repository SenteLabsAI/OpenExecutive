"""Inbox mail with no sender, or with no real message behind it (Gmail id
"0"), is left alone before any turn runs. A listing that gave id "0" fetched
an error text with no From line, and that became an empty Executive turn."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import openexecutive.integrations.email_poller as poller
from openexecutive.integrations.mail_sources import InboundMail
from openexecutive.integrations.workspace.google import GoogleMail
from openexecutive.integrations.workspace.mail import InboundMessage


async def _no_send(_to: str, _body: str, _in_thread: bool) -> str | None:
    return None


def _mail(message_id: str, from_addr: str) -> InboundMail:
    return InboundMail(
        message=InboundMessage(
            message_id=message_id, thread_id="", from_addr=from_addr,
            raw_text="Error: message not found (404)",
        ),
        source="mailbox",
        own_address="exec@example.com",
        send_answer=_no_send,
    )


def _handle(message_id: str, from_addr: str, from_mailbox: bool, monkeypatch: Any) -> tuple[str, AsyncMock]:
    run = AsyncMock(side_effect=AssertionError("a turn ran"))
    monkeypatch.setattr(poller, "_run_executive", run)
    done = AsyncMock()
    gateway = MagicMock()
    gateway.call_tool = AsyncMock(side_effect=AssertionError("a tool was called"))
    outcome = asyncio.run(
        poller._handle_mail(
            _mail(message_id, from_addr),
            gateway,
            mailbox=GoogleMail() if from_mailbox else None,
            done=done,
        )
    )
    run.assert_not_awaited()
    return outcome, done


@pytest.mark.parametrize("message_id", ["0", "", "000"])
@pytest.mark.parametrize("from_mailbox", [True, False])
def test_a_placeholder_id_never_runs_a_turn_and_is_not_marked(
    message_id: str, from_mailbox: bool, monkeypatch: Any
) -> None:
    outcome, done = _handle(message_id, "ann@example.com", from_mailbox, monkeypatch)
    assert outcome == poller._SKIPPED
    done.assert_not_awaited()  # no real message to mark read


def test_mailbox_mail_with_no_sender_is_marked_read_without_a_turn(monkeypatch: Any) -> None:
    # Left unread it would come back in every capped unread listing and,
    # ten of them, starve newer mail.
    outcome, done = _handle("18c0ffee00000001", "", True, monkeypatch)
    assert outcome == poller._SKIPPED
    done.assert_awaited_once()


def test_source_mail_with_no_sender_is_left_to_the_source(monkeypatch: Any) -> None:
    outcome, done = _handle("18c0ffee00000001", "", False, monkeypatch)
    assert outcome == poller._SKIPPED
    done.assert_not_awaited()
