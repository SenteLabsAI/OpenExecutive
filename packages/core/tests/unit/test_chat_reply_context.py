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


def test_slack_another_apps_post_is_not_the_executives() -> None:
    replies = [{"ts": "1.0", "bot_id": "B1", "text": "Build passed"}, {"ts": "2.0", "user": "U"}]
    assert _thread_root_context(replies, {"ts": "2.0", "user": "U"}, "UBOT") == (
        "(Replying to an earlier message:)\n> Build passed"
    )


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


def test_google_chat_app_dm_quote_of_the_other_party_is_the_executives() -> None:
    message = _gchat({"sender": "users/123", "text": "Maya wants to send the lease."})
    assert "your earlier message" in reply_context.google_chat(message, two_party=True)
    assert "an earlier message:" in reply_context.google_chat(message)


def test_google_chat_app_id_comes_from_a_bot_mention() -> None:
    message = {"annotations": [
        {"type": "USER_MENTION", "userMention": {"user": {"name": "users/9", "type": "HUMAN"}}},
        {"type": "USER_MENTION", "userMention": {"user": {"name": "users/77", "type": "BOT"}}},
    ]}
    assert reply_context.google_chat_app_id(message) == "users/77"
    assert reply_context.google_chat_app_id({}) == ""
    two_bots = {"annotations": [
        *message["annotations"],
        {"type": "USER_MENTION", "userMention": {"user": {"name": "users/88", "type": "BOT"}}},
    ]}
    assert reply_context.google_chat_app_id(two_bots) == ""


def test_google_chat_webhook_labels_a_quote_of_the_app_in_a_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.integrations import google_chat

    monkeypatch.setattr(google_chat, "verify_google_jwt", lambda token, project: {})
    settings = SimpleNamespace(
        google_chat_project_number="123",
        google_chat_service_account_file="/fake/key.json",
        google_chat_service_account_email=None,
    )
    process = AsyncMock()
    app = FastAPI()
    app.include_router(google_chat.router)
    body = {
        "type": "MESSAGE",
        "space": {"name": "spaces/D", "type": "DM"},
        "user": {"displayName": "Dana", "email": "dana@example.com"},
        "message": {**_gchat({"sender": "users/app1", "text": "Maya wants to send the lease."}),
                    "thread": {"name": "spaces/D/threads/T"}},
    }
    with (
        patch.object(google_chat, "_process_and_reply", new=process),
        patch("openexecutive.integrations.google_chat.get_settings", return_value=settings),
    ):
        response = TestClient(app).post("/webhook/google-chat", json=body, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert process.await_args.kwargs["reply_context"] == (
        "(Replying to your earlier message:)\n> Maya wants to send the lease."
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
