"""Warm-store parallel fetch used by Discord/Telegram/GChat/email."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_fetch_turn_context_gathers_on_warm_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.integrations import turn_context

    calls: dict[str, Any] = {}

    def fake_retrieve(*, query: str, store: Any = None) -> str:
        calls["retrieve"] = {"query": query, "store": store}
        return "rag"

    def fake_format(*, session_id: str = "", **_: Any) -> str:
        calls["format"] = {"session_id": session_id}
        return "epi"

    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve", fake_retrieve
    )
    monkeypatch.setattr(
        "openexecutive.memory.episodic.format_for_prompt", fake_format
    )
    monkeypatch.setattr(
        "openexecutive.mcp_server.server.get_store", lambda: "warm-store"
    )

    retrieved, episodic = await turn_context.fetch_turn_context(
        "hello", session_id="discord:dm:1"
    )
    assert retrieved == "rag"
    assert episodic == "epi"
    assert calls["retrieve"] == {"query": "hello", "store": "warm-store"}
    assert calls["format"] == {"session_id": "discord:dm:1"}
