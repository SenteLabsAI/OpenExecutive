"""Bot replies carry no link previews: the chat service would fetch a link in
the text on its own, and a reply that quotes someone's mail could carry data
out in one (integrations/telegram_bot.py, slack_bot.py, discord_bot.py)."""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.integrations import discord_bot, slack_bot, telegram_bot


def test_telegram_messages_turn_previews_off(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    class Resp:
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"result": {"message_id": 7}}

    class Client:
        async def post(self, url: str, json: dict[str, Any]) -> Resp:
            sent.append(json)
            return Resp()

    monkeypatch.setattr(telegram_bot, "_get_http_client", lambda: Client())
    asyncio.run(telegram_bot.send_message("t", 1, "See https://example.com/x?d=1"))
    assert sent and all(p["link_preview_options"] == {"is_disabled": True} for p in sent)


def test_slack_replies_do_not_unfurl() -> None:
    source = inspect.getsource(slack_bot)
    assert "await say(text=response, thread_ts=thread_ts, unfurl_links=False, unfurl_media=False)" in source


def test_discord_replies_suppress_embeds() -> None:
    source = inspect.getsource(discord_bot)
    for send in (
        "interaction.followup.send(text, suppress_embeds=True)",
        "message.channel.send(text, suppress_embeds=True)",
        "new_thread.send(text, suppress_embeds=True)",
        "message.reply(text, suppress_embeds=True)",
    ):
        assert send in source
    assert "message.channel.send(text)\n" not in source
    assert "message.reply(text)\n" not in source


def test_discord_dms_suppress_embeds(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(200, json={"id": "c1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: SimpleNamespace(discord_bot_token="t"))
    asyncio.run(discord_bot.send_dm("42", "see evil.example"))
    assert posted[-1] == {"content": "see evil.example", "flags": 4}
