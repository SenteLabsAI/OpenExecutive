"""Outbound attendee allow-list for `google_workspace__manage_event`.

The typed create_calendar_event tool resolves person IDs before calling the
gateway, so this backstop should rarely fire in practice.  It exists to ensure
the calendar gate is bypass-proof: even a raw call_tool invocation can't invite
a non-roster attendee.

Mirrors the structure of test_mcp_gateway_email_egress.py.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.people import store as people_store

EXEC_ADDR = "ceo@example.com"


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


def _settings() -> Any:
    return SimpleNamespace(exec_email_address=EXEC_ADDR)


def _make_gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text='{"id": "evt123", "status": "confirmed"}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed_emails: list[str],
) -> str:
    for i, addr in enumerate(allowed_emails):
        people_store.upsert_person(full_name=f"Person {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(
            gateway.call_tool({
                "name": "google_workspace__manage_event",
                "arguments": arguments,
            })
        )


# ---------------------------------------------------------------------------
# create action — attendees field
# ---------------------------------------------------------------------------

def test_roster_attendee_passes() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync", "start_time": "2025-06-01T10:00:00Z",
         "end_time": "2025-06-01T11:00:00Z", "attendees": ["alice@example.com"]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "evt123"


def test_non_roster_attendee_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync", "start_time": "2025-06-01T10:00:00Z",
         "end_time": "2025-06-01T11:00:00Z", "attendees": ["evil@attacker.com"]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    parsed = json.loads(result)
    assert "error" in parsed
    assert "evil@attacker.com" in parsed["error"]


def test_mixed_attendees_blocked_if_any_non_roster() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync", "start_time": "2025-06-01T10:00:00Z",
         "end_time": "2025-06-01T11:00:00Z",
         "attendees": ["alice@example.com", "evil@attacker.com"]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


def test_empty_attendees_list_passes() -> None:
    """Empty list = organizer-only event; no roster check needed — must pass."""
    gateway, session_call = _make_gateway()
    _call(
        gateway,
        {"action": "create", "summary": "Self", "start_time": "2025-06-01T10:00:00Z",
         "end_time": "2025-06-01T11:00:00Z", "attendees": []},
        [],
    )
    assert session_call.await_count == 1


def test_exec_address_always_allowed() -> None:
    """The organizer's own address must pass even with no roster entries."""
    gateway, session_call = _make_gateway()
    _call(
        gateway,
        {"action": "create", "summary": "Self-sync",
         "start_time": "2025-06-01T10:00:00Z", "end_time": "2025-06-01T11:00:00Z",
         "attendees": [EXEC_ADDR]},
        [],
    )
    assert session_call.await_count == 1


def test_dict_attendee_with_email_key_passes() -> None:
    """manage_event also accepts attendees as [{"email": "..."}] dicts."""
    gateway, session_call = _make_gateway()
    _call(
        gateway,
        {"action": "create", "summary": "Sync",
         "start_time": "2025-06-01T10:00:00Z", "end_time": "2025-06-01T11:00:00Z",
         "attendees": [{"email": "alice@example.com", "displayName": "Alice"}]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1


def test_dict_attendee_non_roster_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync",
         "start_time": "2025-06-01T10:00:00Z", "end_time": "2025-06-01T11:00:00Z",
         "attendees": [{"email": "evil@attacker.com"}]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


def test_newline_in_email_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync",
         "start_time": "2025-06-01T10:00:00Z", "end_time": "2025-06-01T11:00:00Z",
         "attendees": ["alice@example.com\nBcc: evil@a.com"]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "newline" in json.loads(result)["error"].lower()


def test_non_string_attendee_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "create", "summary": "Sync",
         "start_time": "2025-06-01T10:00:00Z", "end_time": "2025-06-01T11:00:00Z",
         "attendees": [42]},  # type: ignore[list-item]
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "non-string" in json.loads(result)["error"].lower()


# ---------------------------------------------------------------------------
# delete / rsvp — no attendee check needed
# ---------------------------------------------------------------------------

def test_delete_passes_without_attendees() -> None:
    """delete action has no attendees — gate should not block it."""
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "delete", "event_id": "evt123"}, [])
    assert session_call.await_count == 1


def test_rsvp_passes_without_attendees() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "rsvp", "event_id": "evt123", "response": "accepted"}, [])
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Tool isolation — other tools must not be affected
# ---------------------------------------------------------------------------

def test_get_events_bypasses_gate() -> None:
    """Read-only calendar tools are not gated."""
    gateway, session_call = _make_gateway()
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        asyncio.run(
            gateway.call_tool({
                "name": "google_workspace__get_events",
                "arguments": {"calendar_id": "primary"},
            })
        )
    assert session_call.await_count == 1


def test_gmail_gate_unaffected() -> None:
    """Calendar gate must not interfere with the Gmail gate."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    # This goes to google_workspace__manage_event which is the calendar gate, not
    # the gmail gate. The attendees field is missing so no block is triggered
    # (the "to" key is not checked by the calendar gate). The call goes through.
    # For Gmail gate behaviour, see test_mcp_gateway_email_egress.py.
    assert "error" not in json.loads(result) or "not on the People roster" in json.loads(result).get("error", "")


# ---------------------------------------------------------------------------
# rsvp_comment — free text mailed to the organizer, who may be off-roster
# ---------------------------------------------------------------------------


def test_rsvp_comment_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"action": "rsvp", "event_id": "e1", "rsvp_response": "accepted",
         "rsvp_comment": "see you there"},
        [],
    )
    assert session_call.await_count == 0
    assert "rsvp_comment" in json.loads(result)["error"]


def test_rsvp_comment_refused_whatever_the_action() -> None:
    """The field is checked before the delete/rsvp pass-through."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, {"action": "delete", "event_id": "e1", "rsvp_comment": "x"}, [])
    assert session_call.await_count == 0
    assert "rsvp_comment" in json.loads(result)["error"]


def test_rsvp_without_comment_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "rsvp", "event_id": "e1", "rsvp_response": "declined",
                    "rsvp_comment": None}, [])
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# send_updates — pinned to "none" when no attendee was checked
# ---------------------------------------------------------------------------


def _forwarded(session_call: AsyncMock) -> dict[str, Any]:
    return session_call.await_args.args[1]["arguments"]


def test_update_without_attendees_pins_send_updates_none() -> None:
    """The event's existing guests may be off-roster; the new description must
    not be mailed to them."""
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "update", "event_id": "e1", "description": "new text",
                    "send_updates": "all"}, [])
    assert session_call.await_count == 1
    assert _forwarded(session_call)["send_updates"] == "none"


def test_update_with_empty_attendees_pins_send_updates_none() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "update", "event_id": "e1", "attendees": []}, [])
    assert _forwarded(session_call)["send_updates"] == "none"


def test_action_case_and_spacing_still_pins() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": " Update", "event_id": "e1", "send_updates": "all"}, [])
    assert _forwarded(session_call)["send_updates"] == "none"


def test_update_with_roster_attendees_keeps_send_updates() -> None:
    """Attendees were checked, so notifying them is the caller's choice."""
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "update", "event_id": "e1",
                    "attendees": ["alice@example.com"], "send_updates": "all"},
          ["alice@example.com"])
    assert _forwarded(session_call)["send_updates"] == "all"


def test_delete_keeps_send_updates() -> None:
    """A cancellation carries no new text."""
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "delete", "event_id": "e1", "send_updates": "all"}, [])
    assert _forwarded(session_call)["send_updates"] == "all"


def test_pin_does_not_touch_other_arguments() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, {"action": "update", "event_id": "e1", "summary": "S"}, [])
    fwd = _forwarded(session_call)
    assert fwd["summary"] == "S" and fwd["event_id"] == "e1" and fwd["action"] == "update"
