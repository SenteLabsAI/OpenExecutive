"""A call through the MCP gateway has a ceiling (MCP_TOOL_TIMEOUT_S).

Without one, a workspace server that never answered held whatever awaited it
for good — the email poller, which handles one message at a time, a brief, a
chat turn. Past the ceiling the model's call comes back as an error that says
the tool may still have run; the gate lookups and tool search raise and are
handled by their callers as any other failure.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import openexecutive.audit
import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import GatewayCallTimeout, MCPGateway


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openexecutive.audit, "log_event", lambda *a, **k: None)


def _settings(timeout_s: float = 0.05) -> Any:
    return SimpleNamespace(
        exec_email_address="ceo@example.com",
        email_poll_interval_seconds=60,
        mcp_tool_timeout_s=timeout_s,
    )


def _result(text: str) -> Any:
    result = MagicMock()
    result.content = [MagicMock(text=text)]
    return result


def _gateway(call_tool: Any) -> MCPGateway:
    gateway = MCPGateway()
    session = MagicMock()
    session.call_tool = call_tool
    gateway._session = session
    return gateway


def test_a_hung_tool_call_comes_back_as_an_error_after_the_ceiling() -> None:
    cancelled = {"seen": False}

    async def hang(_name: str, _args: dict[str, Any]) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    gateway = _gateway(hang)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        text = asyncio.run(asyncio.wait_for(
            gateway.call_tool({"name": "google_workspace__list_calendars", "arguments": {}}),
            timeout=5,
        ))
    payload = json.loads(text)
    assert payload["timed_out"] is True
    assert "did not answer" in payload["error"]
    assert "may still have run" in payload["error"]
    assert gw_module._is_error_payload(text)
    assert cancelled["seen"] is True


def test_a_send_that_times_out_records_no_outbound_linkage() -> None:
    """A send that never answered may not have gone: no reply context for it."""
    async def hang(_name: str, _args: dict[str, Any]) -> Any:
        await asyncio.sleep(30)

    gateway = _gateway(hang)
    with (
        patch.object(gw_module, "get_settings", return_value=_settings()),
        patch.object(gw_module, "_record_email_outbound_context") as record,
        patch.object(gw_module, "_check_gmail_recipients", return_value=None),
        patch.object(gw_module, "_with_disclosure", side_effect=lambda _t, _n, a, **_k: a),
    ):
        text = asyncio.run(gateway.call_tool({
            "name": "google_workspace__send_gmail_message",
            "arguments": {"to": "alice@example.com", "subject": "s", "body": "b"},
        }))
    assert json.loads(text)["timed_out"] is True
    record.assert_not_called()


def test_a_call_inside_the_ceiling_is_unchanged() -> None:
    async def quick(_name: str, _args: dict[str, Any]) -> Any:
        return _result('{"ok": true}')

    gateway = _gateway(quick)
    with patch.object(gw_module, "get_settings", return_value=_settings(timeout_s=5)):
        text = asyncio.run(
            gateway.call_tool({"name": "google_workspace__list_calendars", "arguments": {}})
        )
    assert json.loads(text) == {"ok": True}


def test_a_hung_tool_search_raises() -> None:
    async def hang(_name: str, _args: dict[str, Any]) -> Any:
        await asyncio.sleep(30)

    gateway = _gateway(hang)
    with (
        patch.object(gw_module, "get_settings", return_value=_settings()),
        pytest.raises(GatewayCallTimeout),
    ):
        asyncio.run(gateway.search_tools({"query": "calendar"}))


def test_a_hung_gate_lookup_reads_as_a_failed_lookup() -> None:
    """The M365 gates read an event or message first; a lookup that hangs is
    a lookup that failed (None), which every gate already refuses on."""
    async def hang(_name: str, _args: dict[str, Any]) -> Any:
        await asyncio.sleep(30)

    session = MagicMock()
    session.call_tool = hang
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        got = asyncio.run(gw_module._fetch_m365_json(session, "ms365__get-mail-message", {}))
    assert got is None
