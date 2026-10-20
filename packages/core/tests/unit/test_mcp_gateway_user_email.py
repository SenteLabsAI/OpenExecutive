"""Every Google Workspace call through the gateway acts as the Executive's
own account (orchestrator/mcp_gateway.py `_check_acting_account`)."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openexecutive.orchestrator.mcp_gateway import MCPGateway

EXEC = "ceo.test@example.com"  # tests/conftest.py's EXEC_EMAIL_ADDRESS


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda et, summary, **kw: events.append((et, kw))
    )
    return events


def _gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    result = MagicMock()
    result.content = [MagicMock(text='{"ok": true}')]
    session.call_tool = AsyncMock(return_value=result)
    gateway._session = session
    return gateway, session.call_tool


def _call(name: str, arguments: dict[str, Any]) -> tuple[str, AsyncMock]:
    gateway, forwarded = _gateway()
    return asyncio.run(gateway.call_tool({"name": name, "arguments": arguments})), forwarded


@pytest.mark.parametrize("address", ["olivia@co.example", "someone@elsewhere.example", "", 42])
def test_another_account_is_refused(audit: list[Any], address: object) -> None:
    result, forwarded = _call(
        "google_workspace__search_gmail_messages", {"query": "is:unread", "user_google_email": address}
    )
    assert "act only as the Executive's own account" in json.loads(result)["error"]
    forwarded.assert_not_called()
    assert audit[0][0] == "integration_outbound_blocked"
    assert audit[0][1]["details"]["field"] == "user_google_email"


@pytest.mark.parametrize("arguments", [
    {"query": "is:unread", "user_google_email": EXEC},
    {"query": "is:unread", "user_google_email": f"  {EXEC.upper()} "},
    {"query": "is:unread"},
])
def test_its_own_account_or_none_passes(audit: list[Any], arguments: dict[str, Any]) -> None:
    result, forwarded = _call("google_workspace__search_gmail_messages", arguments)
    assert json.loads(result) == {"ok": True}
    forwarded.assert_awaited_once()
    assert audit == []


def test_other_servers_are_not_its_business(audit: list[Any]) -> None:
    result, forwarded = _call("github__list_prs", {"user_google_email": "x@y.example"})
    assert json.loads(result) == {"ok": True}
    forwarded.assert_awaited_once()


def test_the_pin_runs_before_the_recipient_gate(audit: list[Any]) -> None:
    # A send as another account never reaches the recipient check at all.
    result, forwarded = _call(
        "google_workspace__send_gmail_message",
        {"to": EXEC, "subject": "hi", "body": "x", "user_google_email": "olivia@co.example"},
    )
    assert "own account" in json.loads(result)["error"]
    forwarded.assert_not_called()
