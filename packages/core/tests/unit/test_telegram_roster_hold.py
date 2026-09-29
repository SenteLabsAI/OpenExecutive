"""Telegram: someone off the roster in a private chat is held and told.

Only on a webhook that verifies Telegram sent the update (a valid
TELEGRAM_WEBHOOK_SECRET) and only in a private chat: a group is everyone in
it, and without the secret anyone could forge an update. Everything else
stays silent, as before.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.integrations import telegram_bot

TOKEN = "123456789:AAH" + "x" * 32
HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture(autouse=True)
def _telegram_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)


def _update(chat_type: str = "private", chat_id: int = 777) -> dict[str, Any]:
    return {
        "message": {
            "message_id": 5,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"first_name": "Annamarie", "last_name": "Chen"},
            "text": "Hi, it's Annamarie",
        }
    }


def _post(update: dict[str, Any], secret: str | None) -> AsyncMock:
    hold = AsyncMock()
    app = FastAPI()
    app.include_router(telegram_bot.router)
    headers = {HEADER: secret} if secret else {}
    with (
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=None),
        patch.object(telegram_bot, "_hold_unknown_sender", new=hold),
        patch.object(telegram_bot, "_process_and_reply", new=AsyncMock()),
    ):
        assert TestClient(app).post("/webhook/telegram", json=update, headers=headers).status_code == 200
    return hold


def test_a_private_chat_on_a_verified_webhook_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    hold = _post(_update(), "hook-secret")
    hold.assert_awaited_once()
    kwargs = hold.await_args.kwargs
    assert kwargs["chat_id"] == 777 and kwargs["message_text"] == "Hi, it's Annamarie"
    assert kwargs["sender_name"] == "Annamarie Chen"


def test_a_group_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    _post(_update("group", -100), "hook-secret").assert_not_awaited()


def test_an_unverified_webhook_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    _post(_update(), None).assert_not_awaited()


def test_holding_acknowledges_a_stranger_but_not_a_contact() -> None:
    intake = AsyncMock()
    sent = AsyncMock()
    kwargs: dict[str, Any] = {
        "message_text": "hi", "sender_name": "Annamarie", "chat_id": 777, "message_id": 5,
        "token": TOKEN, "attachment_file_ids": [],
    }
    with (
        patch("openexecutive.integrations.roster_intake.intake", new=intake),
        patch.object(telegram_bot, "send_message", new=sent),
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=None),
    ):
        asyncio.run(telegram_bot._hold_unknown_sender(**kwargs))
        intake.assert_awaited_once()
        assert intake.await_args.args == ("telegram", "777")
        asyncio.run(intake.await_args.kwargs["send_ack"]("received"))
        sent.assert_awaited_once_with(TOKEN, 777, "received")
    intake.reset_mock()
    with (
        patch("openexecutive.integrations.roster_intake.intake", new=intake),
        patch("openexecutive.people.store.find_person_by_telegram_chat_id", return_value=object()),
    ):
        asyncio.run(telegram_bot._hold_unknown_sender(**kwargs))
    intake.assert_not_awaited()


def test_a_held_telegram_message_is_replayed_through_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people.roster_requests import HeldMessage

    handled = AsyncMock()
    monkeypatch.setattr(telegram_bot, "_process_and_reply", handled)
    message = HeldMessage(id=1, request_id=1, external_id="5", payload={
        "message_text": "hi", "sender_name": "A", "chat_id": 777, "message_id": 5,
        "attachment_file_ids": [["f1", "a.pdf", "application/pdf"]],
    })
    assert asyncio.run(telegram_bot._replay_held(message, None)) is True
    kwargs = handled.await_args.kwargs
    assert kwargs["chat_id"] == 777
    assert kwargs["attachment_file_ids"] == [("f1", "a.pdf", "application/pdf")]
