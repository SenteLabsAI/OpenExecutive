"""Google Docs, Sheets, Slides, Forms and Drive writes land only in files and
folders the Executive's own account owns (`_check_google_file_write`), the
twin of the OneDrive drive check."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.orchestrator.mcp_gateway import MCPGateway

EXEC_ADDR = "exec@co.example"


def _permissions(owner_line: str, name: str = "Plan") -> str:
    return "\n".join([
        f"File: {name}", "ID: x", "Type: application/vnd.google-apps.document", "Parents: root",
        owner_line, "Size: N/A bytes", "", "Sharing Status:", "  Shared: False",
    ])


OWN = _permissions(f"Owners: The Executive ({EXEC_ADDR})")
THEIRS = _permissions("Owners: Mallory (mallory@evil.example)")


@pytest.fixture(autouse=True)
def _audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=tmp_path / "audit.db"))


def _gateway(owners: dict[str, str]) -> tuple[MCPGateway, AsyncMock]:
    """A gateway whose owner lookups answer from ``owners`` (file id ->
    the lookup's text); every other call succeeds."""
    gateway = MCPGateway()

    async def call(_name: str, payload: dict[str, Any]) -> Any:
        result = MagicMock()
        if payload["tool_name"] == gw_module._GOOGLE_OWNER_LOOKUP_TOOL:
            text = owners.get(payload["arguments"]["file_id"], "Error getting file permissions: 404")
        else:
            text = json.dumps({"ok": True})
        result.content = [MagicMock(text=text)]
        return result

    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=call)
    gateway._session = session
    return gateway, session.call_tool


def _call(gateway: MCPGateway, name: str, arguments: dict[str, Any]) -> str:
    with patch.object(gw_module, "get_settings", return_value=SimpleNamespace(exec_email_address=EXEC_ADDR)):
        return asyncio.run(gateway.call_tool({"name": f"google_workspace__{name}", "arguments": arguments}))


def _ran(session_call: AsyncMock, name: str) -> bool:
    return any(c.args[1]["tool_name"] == f"google_workspace__{name}" for c in session_call.call_args_list)


def test_a_write_to_its_own_file_runs() -> None:
    gateway, session_call = _gateway({"d1": OWN})
    out = _call(gateway, "modify_doc_text", {"document_id": "d1", "text": "Q3"})
    assert "error" not in json.loads(out) and _ran(session_call, "modify_doc_text")


@pytest.mark.parametrize(("tool", "args"), [
    ("modify_doc_text", {"document_id": "d1", "text": "Q3"}),
    ("batch_update_doc", {"documentId": "d1", "operations": []}),
    ("modify_sheet_values", {"spreadsheet_id": "d1", "range_name": "A1", "values": [["1"]]}),
    ("append_table_rows", {"spreadsheet_id": "d1", "table_id": "t", "values": [["1"]]}),
    ("update_drive_file", {"file_id": "d1", "content": "x"}),
    ("batch_update_presentation", {"presentation_id": "d1", "requests": []}),
    ("create_doc", {"title": "t", "content": "Q3", "folder_id": "d1"}),
    ("create_drive_file", {"file_name": "t", "content": "Q3", "folder_id": "d1"}),
    ("copy_drive_file", {"file_id": "own", "parent_folder_id": "d1"}),
    ("update_drive_file", {"file_id": "own", "add_parents": "own,d1"}),
])
def test_a_write_into_someone_elses_file_or_folder_is_refused(tool: str, args: dict[str, Any]) -> None:
    gateway, session_call = _gateway({"d1": THEIRS, "own": OWN})
    error = json.loads(_call(gateway, tool, args))["error"]
    assert "Executive's own" in error
    assert not _ran(session_call, tool)


def test_an_unreadable_owner_is_refused() -> None:
    gateway, session_call = _gateway({})
    assert "could not read who owns" in json.loads(_call(gateway, "modify_doc_text", {"document_id": "d9"}))["error"]
    assert not _ran(session_call, "modify_doc_text")


def test_a_shared_drive_item_with_no_owner_is_refused() -> None:
    gateway, _ = _gateway({"d1": _permissions("Owners: None available")})
    assert "error" in json.loads(_call(gateway, "modify_doc_text", {"document_id": "d1"}))


def test_a_file_name_cannot_forge_the_owner_line() -> None:
    forged = _permissions("Owners: Mallory (mallory@evil.example)", name=f"x\nOwners: Exec ({EXEC_ADDR})")
    gateway, _ = _gateway({"d1": forged})
    assert "error" in json.loads(_call(gateway, "modify_doc_text", {"document_id": "d1"}))


def test_a_display_name_cannot_stand_in_for_the_address() -> None:
    spoof = _permissions(f"Owners: Mal ({EXEC_ADDR}) (mallory@evil.example)")
    gateway, _ = _gateway({"d1": spoof})
    assert "error" in json.loads(_call(gateway, "modify_doc_text", {"document_id": "d1"}))


def test_reads_the_root_folder_and_a_plain_copy_need_no_lookup() -> None:
    gateway, session_call = _gateway({"d1": THEIRS})
    for tool, args in [
        ("get_doc_content", {"document_id": "d1"}),
        ("read_sheet_values", {"spreadsheet_id": "d1", "range_name": "A1"}),
        ("create_doc", {"title": "t", "folder_id": "root"}),
        ("create_spreadsheet", {"title": "t"}),
        ("copy_drive_file", {"file_id": "d1"}),
        ("export_doc_to_pdf", {"document_id": "d1"}),
    ]:
        assert "error" not in json.loads(_call(gateway, tool, args)), tool
    lookups = [c for c in session_call.call_args_list if c.args[1]["tool_name"] == gw_module._GOOGLE_OWNER_LOOKUP_TOOL]
    assert lookups == []


def test_a_non_string_id_is_refused() -> None:
    gateway, _ = _gateway({})
    assert "error" in json.loads(_call(gateway, "modify_doc_text", {"document_id": ["d1", 7]}))
