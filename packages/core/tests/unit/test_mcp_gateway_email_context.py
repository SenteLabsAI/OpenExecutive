"""Outbound-context linkage recording for sent email.

Email has no `send_email` tool — the Executive emails via the raw MCP tool
`google_workspace__send_gmail_message`, so the linkage that lets a reply be
hydrated with the originating conversation is recorded in
`MCPGateway.call_tool` right after a successful send. This mirrors the DM send
handlers: one open `outbound_context` row per `to`/`cc` recipient, keyed by the
bare lowercased address, only when a live session is active, and never for the
Executive's own address or a soft-error result.
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


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed: list[str],
    tool_name: str = "google_workspace__send_gmail_message",
) -> str:
    for i, addr in enumerate(allowed):
        people_store.upsert_person(full_name=f"Allowed {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(
            gateway.call_tool({"name": tool_name, "arguments": arguments})
        )


def _find(addr: str, db: Path):
    return find_open_outbound_context(
        channel="email", channel_ref=addr, within=timedelta(hours=72), db_path=db
    )


def test_records_one_linkage_per_to_and_cc_recipient(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    result = _call(
        gateway,
        {
            "to": "Alice <alice@example.com>",
            "cc": "bob@example.com",
            "subject": "Budget",
            "body": "Can you confirm the Q3 budget numbers?",
        },
        ["alice@example.com", "bob@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'

    for addr in ("alice@example.com", "bob@example.com"):
        row = _find(addr, isolated_episodic_db)
        assert row is not None, f"no linkage for {addr}"
        assert row.originating_session_id == "webchat:principal-1"
        assert row.outbound_text == "Can you confirm the Q3 budget numbers?"
        # email branch of _resolve_recipient_person_id resolved the roster id
        assert row.recipient_person_id is not None


def test_resolve_recipient_person_id_email(isolated_people_db: Path) -> None:
    """_resolve_recipient_person_id maps an email address to a Person via
    find_person_by_email — the branch the gateway recording relies on."""
    from openexecutive.orchestrator.schedule_tools import _resolve_recipient_person_id

    pid = people_store.upsert_person(full_name="Alice", email="alice@example.com")
    assert _resolve_recipient_person_id("email", "alice@example.com") == pid
    # Case-insensitive match (the gateway lowercases, but be defensive).
    assert _resolve_recipient_person_id("email", "ALICE@example.com") == pid
    # Unknown address → None, never an error.
    assert _resolve_recipient_person_id("email", "ghost@example.com") is None


def test_skips_self_send_address(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {"to": EXEC_ADDR, "subject": "alert", "body": "self note"},
        [],  # self-send passes the egress gate without a roster entry
    )
    assert session_call.await_count == 1
    assert _find(EXEC_ADDR, isolated_episodic_db) is None


def test_records_nothing_without_active_session(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    # current_session stays None (reset_session) — e.g. the email poller's own
    # reply path. No originating conversation, so no linkage.
    _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_no_linkage_on_soft_error_result(isolated_episodic_db: Path) -> None:
    """A soft-error payload means nothing was sent — recording a linkage would
    hydrate a reply that can never arrive."""
    gateway, _ = _make_gateway(result_text='{"error": "gmail 500"}')
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    assert "error" in result
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_empty_body_records_nothing(isolated_episodic_db: Path) -> None:
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "   "},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_blank_body_falls_back_to_html_body(isolated_episodic_db: Path) -> None:
    """A whitespace-only `body` must not shadow a real `html_body` — the
    linkage is recorded with the html_body text."""
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "   ",
            "html_body": "<p>Confirm the Q3 budget?</p>",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    row = _find("alice@example.com", isolated_episodic_db)
    assert row is not None
    assert row.outbound_text == "<p>Confirm the Q3 budget?</p>"


def test_non_send_gmail_tool_records_nothing(isolated_episodic_db: Path) -> None:
    """Only the send tool records. A draft passes the egress gate but isn't a
    sent message, so it must not create a linkage."""
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
        tool_name="google_workspace__create_gmail_draft",
    )
    assert session_call.await_count == 1
    assert _find("alice@example.com", isolated_episodic_db) is None


def test_address_normalization_roundtrips(isolated_episodic_db: Path) -> None:
    """A display-name + mixed-case outbound address records as the bare
    lowercased form, so a later inbound reply (bare lowercased from_addr) hits."""
    gateway, _ = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {"to": "Alice <ALICE@Example.COM>", "subject": "hi", "body": "ping"},
        ["alice@example.com"],
    )
    assert _find("alice@example.com", isolated_episodic_db) is not None


def test_recording_failure_does_not_break_send(isolated_episodic_db: Path) -> None:
    """The recording path is best-effort: a fault while persisting the linkage
    must never turn a successful send into an error."""
    gateway, session_call = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    with patch(
        "openexecutive.orchestrator.schedule_tools._record_outbound_context",
        side_effect=RuntimeError("disk full"),
    ):
        result = _call(
            gateway,
            {"to": "alice@example.com", "subject": "hi", "body": "body"},
            ["alice@example.com"],
        )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'
    assert _find("alice@example.com", isolated_episodic_db) is None


def _kept(addr: str) -> list[str]:
    from openexecutive.integrations import email_replies

    return [b for _w, b in email_replies.recent_elsewhere("none", [addr])]


def test_a_gmail_send_is_kept_for_the_persons_next_email() -> None:
    gateway, _ = _make_gateway()
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(
        gateway,
        {"to": "alice@example.com", "thread_id": "t9", "body": "The Q3 budget is final."},
        ["alice@example.com"],
    )
    from openexecutive.integrations import email_replies

    # Kept under its thread.
    assert [b for _w, b in email_replies.recent("email:t9", "alice@example.com")] == ["The Q3 budget is final."]


def test_a_failed_send_is_not_kept() -> None:
    gateway, _ = _make_gateway("Error: the mailbox refused the message")
    current_session.set(MagicMock(session_id="webchat:principal-1"))
    _call(gateway, {"to": "alice@example.com", "body": "Not sent."}, ["alice@example.com"])
    assert _kept("alice@example.com") == []


def test_an_outlook_send_is_kept_too() -> None:
    gw_module._keep_sent_mail(
        "microsoft_365__send-mail",
        "microsoft_365__send_mail",
        {"body": {"Message": {
            "toRecipients": [{"emailAddress": {"address": "Bob@Example.com"}}],
            "bccRecipients": [{"emailAddress": {"address": "cy@example.com"}}],
            "body": {"contentType": "HTML", "content": "<p>The plan is attached.</p>"},
        }}},
    )
    assert [b.strip() for b in _kept("bob@example.com")] == ["The plan is attached."]
    assert [b.strip() for b in _kept("cy@example.com")] == ["The plan is attached."]
