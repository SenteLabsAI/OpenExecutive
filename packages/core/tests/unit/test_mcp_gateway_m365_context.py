"""Outbound-context linkage recording for Microsoft 365 `send-mail`.

The Microsoft twin of test_mcp_gateway_email_context.py: after a successful
`microsoft_365__send-mail`, one open `outbound_context` row per to/cc recipient
(bare lowercased address) is recorded with the message text, only while a live
session is active, never for the exec's own mailbox, bcc, a soft-error result,
or a tool that only drafts.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.memory.episodic import find_open_outbound_context, initialize_db
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import store as people_store

EXEC_ADDR = "exec@example.com"
BODY = "Can you confirm the Q3 budget numbers?"


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


@pytest.fixture(autouse=True)
def isolated_episodic_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def reset_session() -> Any:
    token = current_session.set(None)
    yield
    current_session.reset(token)


def _settings() -> Any:
    return SimpleNamespace(exec_email_address=EXEC_ADDR, email_poll_interval_seconds=60)


def _make_gateway(result_text: str = '{"ok": true}') -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text=result_text)]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _recipient(addr: str) -> dict[str, Any]:
    return {"emailAddress": {"address": addr, "name": addr.split("@")[0]}}


def _args(
    to: list[str], cc: list[str] | None = None, bcc: list[str] | None = None, content: str = BODY,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "subject": "Budget",
        "body": {"contentType": "Text", "content": content},
        "toRecipients": [_recipient(a) for a in to],
    }
    if cc:
        message["ccRecipients"] = [_recipient(a) for a in cc]
    if bcc:
        message["bccRecipients"] = [_recipient(a) for a in bcc]
    return {"body": {"Message": message, "SaveToSentItems": True}}


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed: list[str],
    tool_name: str = "microsoft_365__send-mail",
) -> str:
    for i, addr in enumerate(allowed):
        people_store.upsert_person(full_name=f"Allowed {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(gateway.call_tool({"name": tool_name, "arguments": arguments}))


def _find(addr: str, db: Path):
    return find_open_outbound_context(
        channel="email", channel_ref=addr, within=timedelta(hours=72), db_path=db
    )


def test_records_one_linkage_per_to_and_cc_recipient(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    result = _call(
        gateway, _args(["Alice@Example.com"], cc=["bob@example.com"]),
        ["alice@example.com", "bob@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'
    for addr in ("alice@example.com", "bob@example.com"):
        row = _find(addr, isolated_episodic_db)
        assert row is not None, f"no linkage for {addr}"
        assert row.originating_session_id == "webchat:principal-1"
        assert row.outbound_text == BODY
        assert row.recipient_person_id is not None


def test_skips_exec_own_address_and_bcc(isolated_episodic_db: Path) -> None:
    gateway, _ = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway, _args(["alice@example.com", EXEC_ADDR], bcc=["carol@example.com"]),
        ["alice@example.com", "carol@example.com"],
    )
    assert _find("alice@example.com", isolated_episodic_db) is not None
    assert _find(EXEC_ADDR, isolated_episodic_db) is None
    assert _find("carol@example.com", isolated_episodic_db) is None


def test_no_linkage_without_a_live_session(isolated_episodic_db: Path) -> None:
    gateway, _ = _make_gateway()
    _call(gateway, _args(["alice@example.com"]), ["alice@example.com"])
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_no_linkage_on_soft_error_result(isolated_episodic_db: Path) -> None:
    gateway, _ = _make_gateway('{"error": "MailboxNotEnabledForRESTAPI"}')
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(gateway, _args(["alice@example.com"]), ["alice@example.com"])
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_draft_tool_records_nothing(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway, {"body": _args(["alice@example.com"])["body"]["Message"]},
        ["alice@example.com"], tool_name="microsoft_365__create-draft-email",
    )
    assert session_call.await_count == 1
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_blank_content_records_nothing(isolated_episodic_db: Path) -> None:
    gateway, _ = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(gateway, _args(["alice@example.com"], content="   "), ["alice@example.com"])
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_recording_failure_never_breaks_the_send(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))

    def _boom(**_: Any) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools._record_outbound_context", _boom)
    result = _call(gateway, _args(["alice@example.com"]), ["alice@example.com"])
    assert session_call.await_count == 1
    assert result == '{"ok": true}'
