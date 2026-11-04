"""Telegram: a message sent with Reply carries the message it answers."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.integrations import telegram_bot
from openexecutive.integrations.telegram_bot import _reply_context

TOKEN = "123456789:AAH" + "x" * 32
ME = {"id": 42, "is_bot": False, "first_name": "Dana"}
BOT = {"id": 123456789, "is_bot": True, "first_name": "Executive"}


def _reply(replied: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "message_id": 7,
        "chat": {"id": 42, "type": "private"},
        "from": ME,
        "text": "What is she wanting to send?",
        "reply_to_message": {"message_id": 6, "chat": {"id": 42}, **replied},
        **extra,
    }


def test_no_reply_no_context() -> None:
    assert _reply_context({"message_id": 1, "from": ME, "text": "hi"}) == ""


def test_a_reply_to_the_executive_quotes_its_message() -> None:
    ctx = _reply_context(_reply({"from": BOT, "text": "Maya wants to send the lease.\nShall I reply?"}), TOKEN)
    assert ctx == (
        "(Replying to your earlier message:)\n"
        "> Maya wants to send the lease.\n"
        "> Shall I reply?"
    )


def test_a_highlighted_quote_is_used_in_place_of_the_whole() -> None:
    message = _reply({"from": BOT, "text": "One. Two. Three."}, quote={"text": "Two."})
    assert _reply_context(message, TOKEN) == "(Replying to your earlier message:)\n> Two."


def test_another_bots_post_is_not_the_executives() -> None:
    other_bot = {"id": 555, "is_bot": True, "first_name": "Alerts"}
    assert "an earlier message from Alerts" in _reply_context(_reply({"from": other_bot, "text": "x"}), TOKEN)


def test_their_own_message_and_someone_elses_are_told_apart() -> None:
    assert "their own earlier message" in _reply_context(_reply({"from": ME, "text": "x"}), TOKEN)
    other = {"id": 5, "is_bot": False, "first_name": "Sam", "last_name": "Lee"}
    assert "an earlier message from Sam Lee" in _reply_context(_reply({"from": other, "text": "x"}), TOKEN)


def test_a_caption_or_a_bare_file_still_says_what_it_answers() -> None:
    assert "> the plan" in _reply_context(_reply({"from": BOT, "caption": "the plan", "photo": [{}]}), TOKEN)
    assert "> [a file]" in _reply_context(_reply({"from": ME, "document": {"file_id": "f"}}), TOKEN)
    assert _reply_context(_reply({"from": ME}), TOKEN) == ""


def test_a_long_message_is_cut() -> None:
    ctx = _reply_context(_reply({"from": BOT, "text": "a" * 5000}), TOKEN)
    assert len(ctx) < 1600 and ctx.endswith("…")


def test_the_webhook_hands_the_reply_context_to_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    process = AsyncMock()
    app = FastAPI()
    app.include_router(telegram_bot.router)
    update = {"message": _reply({"from": BOT, "text": "Maya wants to send the lease."})}
    with (
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=object()),
        patch.object(telegram_bot, "_process_and_reply", new=process),
    ):
        assert TestClient(app).post("/webhook/telegram", json=update).status_code == 200
    kwargs = process.await_args.kwargs
    assert kwargs["message_text"] == "What is she wanting to send?"
    assert kwargs["reply_context"] == "(Replying to your earlier message:)\n> Maya wants to send the lease."
