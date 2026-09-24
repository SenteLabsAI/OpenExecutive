"""Unit tests for Anthropic's native web_search server tool wiring.

Covers the tool dict shape, the Executive's tool-assembly order (server tool
is appended after the cached client-tool list so cache_control stays on a
client-side tool), the response-block handling for server_tool_use and
web_search_tool_result, and audit logging of search queries.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import pytest

from openexecutive.config import get_settings


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module's `memory_extraction` rows out of the default
    `./episodic_memory.db`.

    Driving a turn schedules episodic extraction fire-and-forget, and its
    audit row goes through `audit.log_event`, which targets the default DB
    whatever `db_path` the extraction was handed. Those rows surface as real
    data in other modules' assertions during a full run only — the audit-log
    pollution trap in CLAUDE.md.
    """
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *a, **kw: None, raising=False
    )



def _reset_settings_cache() -> None:
    # get_settings constructs a fresh Settings() each call, so just clear envs.
    # ENABLE_WEB_SEARCH defaults to True in config.py; pin it false here so
    # the "disabled" cases below have a clean baseline (tests that want it
    # enabled explicitly set ENABLE_WEB_SEARCH=true after reset).
    for k in (
        "ENABLE_WEB_SEARCH",
        "WEB_SEARCH_MAX_USES",
        "WEB_SEARCH_ALLOWED_DOMAINS",
        "WEB_SEARCH_BLOCKED_DOMAINS",
    ):
        os.environ.pop(k, None)
    os.environ["ENABLE_WEB_SEARCH"] = "false"


# ---------------------------------------------------------------------------
# build_web_search_tool
# ---------------------------------------------------------------------------


def test_build_returns_none_when_disabled() -> None:
    _reset_settings_cache()
    from openexecutive.orchestrator.web_search_tool import build_web_search_tool
    assert build_web_search_tool() is None


def test_build_returns_anthropic_server_tool_dict_when_enabled() -> None:
    _reset_settings_cache()
    os.environ["ENABLE_WEB_SEARCH"] = "true"
    os.environ["WEB_SEARCH_MAX_USES"] = "3"
    try:
        from openexecutive.orchestrator.web_search_tool import build_web_search_tool
        tool = build_web_search_tool()
        assert tool is not None
        assert tool["type"] == "web_search_20250305"
        assert tool["name"] == "web_search"
        assert tool["max_uses"] == 3
        # No allowed/blocked domains set — keys must be absent.
        assert "allowed_domains" not in tool
        assert "blocked_domains" not in tool
    finally:
        _reset_settings_cache()


def test_build_includes_allowed_domains_when_configured() -> None:
    _reset_settings_cache()
    os.environ["ENABLE_WEB_SEARCH"] = "true"
    os.environ["WEB_SEARCH_ALLOWED_DOMAINS"] = "wsj.com, ft.com"
    try:
        from openexecutive.orchestrator.web_search_tool import build_web_search_tool
        tool = build_web_search_tool()
        assert tool is not None
        assert tool["allowed_domains"] == ["wsj.com", "ft.com"]
    finally:
        _reset_settings_cache()


def test_config_rejects_both_allow_and_block_lists() -> None:
    _reset_settings_cache()
    os.environ["WEB_SEARCH_ALLOWED_DOMAINS"] = "a.com"
    os.environ["WEB_SEARCH_BLOCKED_DOMAINS"] = "b.com"
    try:
        with pytest.raises(ValueError):
            get_settings()
    finally:
        _reset_settings_cache()


def test_config_rejects_zero_max_uses() -> None:
    _reset_settings_cache()
    os.environ["WEB_SEARCH_MAX_USES"] = "0"
    try:
        with pytest.raises(ValueError):
            get_settings()
    finally:
        _reset_settings_cache()


# ---------------------------------------------------------------------------
# Persona addendum is added only when feature is enabled
# ---------------------------------------------------------------------------


def test_persona_includes_web_search_addendum_when_enabled() -> None:
    _reset_settings_cache()
    os.environ["ENABLE_WEB_SEARCH"] = "true"
    try:
        from openexecutive.prompts.cache_manager import build_system_blocks
        blocks = build_system_blocks(company_profile=None, mcp_enabled=False)
        persona_text = blocks[0]["text"]
        assert "web_search" in persona_text
    finally:
        _reset_settings_cache()


def test_persona_omits_web_search_addendum_when_disabled() -> None:
    _reset_settings_cache()
    from openexecutive.prompts.cache_manager import build_system_blocks
    blocks = build_system_blocks(company_profile=None, mcp_enabled=False)
    persona_text = blocks[0]["text"]
    assert "Web Search" not in persona_text


# ---------------------------------------------------------------------------
# Executive tool-assembly order
# ---------------------------------------------------------------------------


def _make_stream_cm(final_msg: object) -> MagicMock:
    """Async context manager mimicking AsyncAnthropic().messages.stream()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)

    async def _aiter():
        # No content deltas — we only care about final_msg.
        if False:
            yield None  # pragma: no cover

    cm.__aiter__ = lambda self=cm: _aiter()
    cm.get_final_message = AsyncMock(return_value=final_msg)
    return cm


def test_executive_appends_web_search_after_cached_client_tools() -> None:
    _reset_settings_cache()
    os.environ["ENABLE_WEB_SEARCH"] = "true"
    try:
        from openexecutive.orchestrator.executive import Executive
        from openexecutive.orchestrator.session import Session

        # Drive one loop iteration with a no-tool response so we can inspect
        # the tool list the SDK was called with.
        final_msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn",
        )
        captured_tools: list[list[dict]] = []

        def fake_stream(**kwargs):
            captured_tools.append(kwargs["tools"])
            return _make_stream_cm(final_msg)

        exec_ = Executive()
        fake_provider = SimpleNamespace(messages_stream=fake_stream)

        async def _run() -> None:
            session = Session(session_id="t1")
            chunks: list = []
            async for c in exec_.stream_chat("hi", session):
                chunks.append(c)

        with patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=fake_provider,
        ):
            asyncio.run(_run())
        assert captured_tools, "stream() was not invoked"
        tools = captured_tools[0]
        # Web search must be present and must be last (so it stays out of the
        # cache_control slot, which is reserved for client-side tools).
        assert tools[-1]["name"] == "web_search"
        assert tools[-1]["type"] == "web_search_20250305"
        # The last client-side tool (second-to-last overall) is the one that
        # carries cache_control. Web search must NOT carry cache_control.
        assert "cache_control" not in tools[-1]
        assert tools[-2].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    finally:
        _reset_settings_cache()


def test_executive_omits_web_search_when_disabled() -> None:
    _reset_settings_cache()
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    final_msg = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
    )
    captured_tools: list[list[dict]] = []

    def fake_stream(**kwargs):
        captured_tools.append(kwargs["tools"])
        return _make_stream_cm(final_msg)

    exec_ = Executive()
    fake_provider = SimpleNamespace(messages_stream=fake_stream)

    async def _run() -> None:
        session = Session(session_id="t2")
        async for _ in exec_.stream_chat("hi", session):
            pass

    with patch(
        "openexecutive.orchestrator.executive.get_provider",
        return_value=fake_provider,
    ):
        asyncio.run(_run())
    names = {t.get("name") for t in captured_tools[0]}
    assert "web_search" not in names


# ---------------------------------------------------------------------------
# server_tool_use / web_search_tool_result preservation + audit log
# ---------------------------------------------------------------------------


def _server_tool_block(query: str, block_id: str = "srvtoolu_1") -> MagicMock:
    block = MagicMock()
    block.type = "server_tool_use"
    block.id = block_id
    block.name = "web_search"
    block.input = {"query": query}
    block.model_dump.return_value = {
        "type": "server_tool_use",
        "id": block_id,
        "name": "web_search",
        "input": {"query": query},
    }
    return block


def _web_result_block(tool_use_id: str = "srvtoolu_1") -> MagicMock:
    block = MagicMock()
    block.type = "web_search_tool_result"
    block.tool_use_id = tool_use_id
    block.model_dump.return_value = {
        "type": "web_search_tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "web_search_result", "url": "https://x", "title": "T"}],
    }
    return block


def test_server_tool_blocks_are_preserved_and_audited() -> None:
    _reset_settings_cache()
    os.environ["ENABLE_WEB_SEARCH"] = "true"
    try:
        from openexecutive.orchestrator.executive import Executive
        from openexecutive.orchestrator.session import Session

        text_block = SimpleNamespace(type="text", text="Based on my search...")
        final_msg = SimpleNamespace(
            content=[
                _server_tool_block("current EUR/USD"),
                _web_result_block(),
                text_block,
            ],
            stop_reason="end_turn",
        )

        def fake_stream(**_kwargs):
            return _make_stream_cm(final_msg)

        exec_ = Executive()
        fake_provider = SimpleNamespace(messages_stream=fake_stream)

        audit_calls: list[dict] = []

        def _capture(event_type, summary, **kwargs):
            audit_calls.append({"event_type": event_type, "summary": summary, **kwargs})

        with (
            patch(
                "openexecutive.orchestrator.executive.get_provider",
                return_value=fake_provider,
            ),
            patch(
                "openexecutive.orchestrator.executive.audit_log", side_effect=_capture
            ),
        ):
            async def _run() -> str:
                return await exec_.chat("hi", Session(session_id="t3"))
            asyncio.run(_run())

        ws_calls = [c for c in audit_calls if c["event_type"] == "tool_invocation"
                    and "web_search" in c["summary"]]
        assert len(ws_calls) == 1
        assert "current EUR/USD" in ws_calls[0]["summary"]
        assert ws_calls[0]["actor"] == "executive"
        assert ws_calls[0]["details"]["tool"] == "web_search"
        assert ws_calls[0]["details"]["kind"] == "server_tool"
    finally:
        _reset_settings_cache()
