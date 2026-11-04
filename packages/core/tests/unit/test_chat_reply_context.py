"""Every chat app carries the message a reply answers into the turn."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import reply_context
from openexecutive.integrations.slack_bot import _thread_root_context


def test_quote_and_with_context() -> None:
    block = reply_context.quote(reply_context.EXECUTIVE, "One.\nTwo.")
    assert block == "(Replying to your earlier message:)\n> One.\n> Two."
    assert reply_context.with_context(block, "Why?") == f"{block}\n\nWhy?"
    assert reply_context.with_context("", "Why?") == "Why?"
    assert reply_context.quote(reply_context.SENDER, "   ") == ""
    assert reply_context.whose(by_executive=False, by_sender=False, name=" Sam  Lee ") == (
        "an earlier message from Sam Lee"
    )
    assert reply_context.whose(by_executive=False, by_sender=False) == "an earlier message"


# --------------------------------------------------------------------------- Slack


def test_slack_reply_in_a_dm_thread_quotes_the_thread_start() -> None:
    replies = [
        {"ts": "1.0", "user": "UBOT", "text": "Maya wants to send the lease."},
        {"ts": "2.0", "user": "UDANA", "text": "What is she wanting to send?"},
    ]
    event = {"ts": "2.0", "user": "UDANA"}
    assert _thread_root_context(replies, event, "UBOT") == (
        "(Replying to your earlier message:)\n> Maya wants to send the lease."
    )


def test_slack_thread_start_itself_or_no_thread_quotes_nothing() -> None:
    assert _thread_root_context(None, {"ts": "1.0"}, "UBOT") == ""
    assert _thread_root_context([{"ts": "1.0", "user": "U", "text": "hi"}], {"ts": "1.0"}, "UBOT") == ""


def test_slack_app_post_without_a_user_is_the_executives() -> None:
    replies = [{"ts": "1.0", "bot_id": "B1", "text": "Your brief"}, {"ts": "2.0", "user": "U"}]
    assert "your earlier message" in _thread_root_context(replies, {"ts": "2.0", "user": "U"}, None)


# --------------------------------------------------------------------- Google Chat


def _gchat(snapshot: dict[str, Any] | None, quote_type: str = "REPLY") -> dict[str, Any]:
    message: dict[str, Any] = {"name": "spaces/S/messages/2", "sender": {"name": "users/dana"}, "text": "Why?"}
    if snapshot is not None:
        message["quotedMessageMetadata"] = {"quoteType": quote_type, "quotedMessageSnapshot": snapshot}
    return message


def test_google_chat_quote_is_carried() -> None:
    message = _gchat({"sender": "users/exec", "text": "Maya wants to send the lease."})
    assert reply_context.google_chat(message, own="users/exec") == (
        "(Replying to your earlier message:)\n> Maya wants to send the lease."
    )
    assert "their own earlier message" in reply_context.google_chat(
        _gchat({"sender": "users/dana", "text": "x"})
    )


def test_google_chat_no_quote_or_a_forward_is_nothing() -> None:
    assert reply_context.google_chat(_gchat(None)) == ""
    assert reply_context.google_chat(_gchat({"sender": "users/x", "text": "x"}, "FORWARD")) == ""


# ------------------------------------------------------------------ Discord


@pytest.mark.asyncio
async def test_discord_reply_quotes_the_answered_message() -> None:
    from openexecutive.integrations.discord_bot import _reply_context

    bot = SimpleNamespace(id=9)
    dana = SimpleNamespace(id=42, display_name="Dana")
    answered = SimpleNamespace(content="Maya wants to send the lease.", attachments=[], author=bot)

    async def fetch_message(message_id: int) -> Any:
        assert message_id == 6
        return answered

    message = SimpleNamespace(
        id=7, author=dana,
        reference=SimpleNamespace(message_id=6, resolved=None, cached_message=None),
        channel=SimpleNamespace(fetch_message=fetch_message),
    )
    assert await _reply_context(message, bot) == (
        "(Replying to your earlier message:)\n> Maya wants to send the lease."
    )
    message.reference = None
    assert await _reply_context(message, bot) == ""
