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
    long = reply_context.whose(by_executive=False, by_sender=False, name="x" * 200)
    assert long == "an earlier message from " + "x" * reply_context.NAME_MAX


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


# ------------------------------------------------------------ what memory keeps

QUOTE = "(Replying to your earlier message:)\n> I recommend we drop the vendor."


@pytest.fixture
def turn_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The stores a turn reads, on a temp file."""
    from openexecutive.audit import logger as audit_logger
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    path = tmp_path / "turn.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    yield path
    people_registry.invalidate()


def _turn_stubs(stack: Any, executive: Any) -> None:
    """Everything a chat turn calls apart from the Executive, stubbed."""
    from unittest.mock import AsyncMock, patch

    from openexecutive.memory.company_profile import CompanyProfile

    for target, kw in [
        ("openexecutive.orchestrator.executive.Executive", {"new": executive}),
        ("openexecutive.knowledge.retriever.retrieve", {"return_value": ""}),
        ("openexecutive.memory.episodic.format_for_prompt", {"return_value": ""}),
        ("openexecutive.memory.session_store.load_messages", {"return_value": []}),
        ("openexecutive.memory.session_store.create_session", {}),
        ("openexecutive.memory.session_store.save_message", {}),
        ("openexecutive.memory.session_store.update_session_timestamp", {}),
        ("openexecutive.onboarding.profile_builder.load_or_create_profile",
         {"return_value": CompanyProfile()}),
        ("openexecutive.alerts.pipeline.schedule_evaluation", {}),
        ("openexecutive.workflows.inbound_resolver.resolve_inbound_message",
         {"new": AsyncMock(return_value=None)}),
        ("openexecutive.orchestrator.mcp_gateway.get_active_gateway", {"return_value": None}),
    ]:
        stack.enter_context(patch(target, **kw))


@pytest.mark.parametrize("on_roster", [True, False])
def test_google_chat_turn_quotes_for_the_team_and_memory_keeps_their_words(on_roster: bool) -> None:
    import asyncio
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock, patch

    from openexecutive.integrations import google_chat

    executive = MagicMock()
    executive.return_value.chat = AsyncMock(return_value="ok")
    with ExitStack() as stack:
        _turn_stubs(stack, executive)
        stack.enter_context(patch.object(google_chat, "send_reply"))
        stack.enter_context(patch.object(google_chat, "_sender_on_roster", return_value=on_roster))
        asyncio.run(
            google_chat._process_and_reply(
                message_text="Why?",
                sender_name="Dana",
                space_name="spaces/D",
                thread_name="spaces/D/threads/T",
                message_name="spaces/D/messages/M",
                service_account_file="/fake/key.json",
                service_account_email=None,
                sender_email="dana@example.com",
                reply_context=QUOTE,
            )
        )
    kwargs = executive.return_value.chat.await_args.kwargs
    if on_roster:
        assert kwargs["user_message"] == f"{QUOTE}\n\nWhy?"
        assert kwargs["memory_text"] == "Why?"
    else:
        # Someone off the team: their quote is text they chose, so it stays out.
        assert kwargs["user_message"] == "Why?"
        assert kwargs["memory_text"] is None


@pytest.mark.usefixtures("turn_db")
def test_telegram_turn_memory_keeps_their_words() -> None:
    import asyncio
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock, patch

    from openexecutive.integrations import telegram_bot

    executive = MagicMock()
    executive.return_value.chat = AsyncMock(return_value="ok")
    with ExitStack() as stack:
        _turn_stubs(stack, executive)
        stack.enter_context(patch.object(telegram_bot, "send_message", new=AsyncMock(return_value=None)))
        asyncio.run(
            telegram_bot._process_and_reply(
                message_text="Why?",
                sender_name="Dana",
                chat_id=42,
                message_id=1,
                token="t",
                reply_context=QUOTE,
            )
        )
    kwargs = executive.return_value.chat.await_args.kwargs
    assert kwargs["user_message"].startswith(f"{QUOTE}\n\nWhy?")
    assert kwargs["memory_text"] == "Why?"


@pytest.mark.usefixtures("turn_db")
async def test_discord_turn_memory_keeps_their_words() -> None:
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock

    from openexecutive.integrations import discord_bot

    executive = MagicMock()
    executive.return_value.chat = AsyncMock(return_value="ok")
    from openexecutive.people import store as people_store

    people_store.upsert_person(full_name="Dana Doe", is_principal=True, discord_user_id="u1")
    with ExitStack() as stack:
        _turn_stubs(stack, executive)
        await discord_bot._handle_message(
            text="Why?",
            discord_user_id="u1",
            discord_channel="dm-1",
            message_id="m1",
            thread_id=None,
            send_fn=AsyncMock(),
            is_dm=True,
            session_id="discord:user:u1",
            session_title="DM",
            reply_context=QUOTE,
        )
    kwargs = executive.return_value.chat.await_args.kwargs
    assert kwargs["user_message"].startswith(f"{QUOTE}\n\nWhy?")
    assert kwargs["memory_text"] == "Why?"


@pytest.mark.usefixtures("turn_db")
def test_slack_dm_thread_turn_quotes_the_start_and_memory_keeps_their_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from contextlib import ExitStack, suppress
    from unittest.mock import AsyncMock, MagicMock

    from openexecutive.config import get_settings
    from openexecutive.integrations import slack_bot
    from openexecutive.people import store as people_store

    people_store.upsert_person(full_name="Dana Doe", is_principal=True, slack_user_id="U1")
    stub = get_settings().model_copy(update={"slack_bot_token": "xoxb-test", "slack_app_token": "xapp-test"})
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: stub)
    monkeypatch.setattr(
        "slack_sdk.web.async_client.AsyncWebClient.auth_test", AsyncMock(return_value={"user_id": "UBOT"})
    )
    monkeypatch.setattr(slack_bot, "_bot_user_id", None)
    event = {"text": "Why?", "user": "U1", "channel": "D1", "channel_type": "im",
             "ts": "2.0", "thread_ts": "1.0"}
    root = {"ts": "1.0", "user": "UBOT", "text": "I recommend we drop the vendor."}
    executive = MagicMock()
    executive.return_value.chat = AsyncMock(return_value="ok")

    async def _go() -> None:
        app, handler = await slack_bot.create_slack_app()
        try:
            listeners = {x.ack_function.__name__: x.ack_function for x in app._async_listeners}
            client = MagicMock()
            client.conversations_replies = AsyncMock(return_value={"messages": [root, event]})
            await listeners["handle_message"](event=event, say=AsyncMock(), client=client)
        finally:
            with suppress(Exception):
                await handler.close_async()
            http = getattr(app.client, "session", None)
            if http is not None and not http.closed:
                await http.close()

    with ExitStack() as stack:
        _turn_stubs(stack, executive)
        asyncio.run(_go())
    kwargs = executive.return_value.chat.await_args.kwargs
    assert kwargs["user_message"].startswith(QUOTE + "\n\n")
    assert kwargs["user_message"].endswith("Why?")
    assert kwargs["memory_text"] == "Why?"
