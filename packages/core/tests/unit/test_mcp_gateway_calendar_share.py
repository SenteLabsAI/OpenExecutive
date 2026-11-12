"""Who `google_workspace__manage_calendar` may share the calendar with.

workspace-mcp 2.4 added calendar sharing. A grant to someone not in People,
or to a whole domain or everyone, is a step for the owner's Approvals level,
like an off-roster invite; creating a calendar, listing its sharing and
removing a rule pass.
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
TOOL = "google_workspace__manage_calendar"


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Alice", email="alice@example.com")
    return db_path


def _call(arguments: dict[str, Any]) -> tuple[str, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="ok")]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    with patch.object(gw_module, "get_settings", return_value=SimpleNamespace(exec_email_address=EXEC_ADDR)):
        result = asyncio.run(gateway.call_tool({"name": TOOL, "arguments": arguments}))
    return result, session.call_tool


@pytest.mark.parametrize("arguments", [
    {"action": "share", "scope_type": "user", "scope_value": "alice@example.com", "role": "reader"},
    {"action": "share", "scope_value": "Alice@Example.com", "role": "reader"},
    {"action": "update_sharing", "rule_id": "user:alice@example.com", "role": "writer"},
    {"action": "create", "summary": "Board"},
    {"action": "list_sharing"},
    {"action": "unshare", "rule_id": "user:evil@attacker.com"},
])
def test_grants_to_people_and_non_grants_run(arguments: dict[str, Any]) -> None:
    result, call_tool = _call(arguments)
    assert call_tool.await_count == 1, result


@pytest.mark.parametrize(("arguments", "named"), [
    ({"action": "share", "scope_type": "user", "scope_value": "evil@attacker.com", "role": "owner"},
     "evil@attacker.com"),
    ({"action": "share", "scope_type": "group", "scope_value": "all@attacker.com", "role": "reader"},
     "all@attacker.com"),
    ({"action": "share", "scope_type": "domain", "scope_value": "attacker.com", "role": "reader"}, "domain"),
    ({"action": "share", "scope_type": "default", "role": "reader"}, "everyone"),
    ({"action": "Share", "scopeValue": "evil@attacker.com", "role": "reader"}, "evil@attacker.com"),
    ({"action": "update_sharing", "rule_id": "user:evil@attacker.com", "role": "owner"}, "evil@attacker.com"),
    ({"action": "update_sharing", "rule_id": "default", "role": "reader"}, "everyone"),
])
def test_grants_beyond_people_wait(arguments: dict[str, Any], named: str) -> None:
    result, call_tool = _call(arguments)
    assert call_tool.await_count == 0
    assert named in json.loads(result)["error"]
