"""Outbound roster allow-list for Google Drive sharing / permission tools.

At the `complete` tool tier the Executive can grant another principal access to
a Drive file. That is an egress vector exactly like sending an email or inviting
a calendar attendee, so `MCPGateway.call_tool` routes Drive-share tools through
`_check_drive_share` before the call reaches the MCP subprocess. This guards
against prompt injection steering the Executive into sharing a file with an
arbitrary external address (or making it public).

Mirrors the structure of test_mcp_gateway_calendar_egress.py.
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
from openexecutive.orchestrator.mcp_gateway import (
    MCPGateway,
    _check_drive_share,
    _is_drive_share_tool,
)
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
    fake_result.content = [MagicMock(text='{"id": "perm123", "status": "ok"}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session
    return gateway, session.call_tool


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed_emails: list[str],
    *,
    name: str = "google_workspace__manage_drive_access",
) -> str:
    for i, addr in enumerate(allowed_emails):
        people_store.upsert_person(full_name=f"Person {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(
            gateway.call_tool({"name": name, "arguments": arguments})
        )


# ---------------------------------------------------------------------------
# manage_drive_access — grant to an email
# ---------------------------------------------------------------------------

def test_roster_grantee_passes() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "email_address": "alice@example.com", "role": "reader",
         "type": "user"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_non_roster_grantee_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "email_address": "evil@attacker.com", "role": "writer",
         "type": "user"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    parsed = json.loads(result)
    assert "error" in parsed
    assert "evil@attacker.com" in parsed["error"]


def test_exec_address_always_allowed() -> None:
    gateway, session_call = _make_gateway()
    args = {"file_id": "abc", "email_address": EXEC_ADDR, "role": "writer"}
    # Direct unit assertion that the *gate itself* decided to allow (not merely
    # that the call reached the mock) — distinguishes "gate allowed" from "no
    # gate present". The exec address is always allowed with an empty roster.
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        assert _check_drive_share("google_workspace__manage_drive_access", args) is None
    result = _call(gateway, args, [])
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_offroster_grantee_in_unknown_field_blocked() -> None:
    """The gate scans every string, not a fixed key allow-list, so a grantee
    email passed through an unrecognized field name is still caught (fail
    closed) — the point of scanning all strings rather than known keys."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "share_with": "evil@attacker.com", "role": "reader"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


def test_email_nested_in_permission_dict_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc",
         "permissions": [{"emailAddress": "evil@attacker.com", "role": "writer"}]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


def test_email_in_dict_key_blocked() -> None:
    """An email-keyed permission map (grantee in key position) is still caught."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "grantees": {"evil@attacker.com": "writer"}},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


def test_mixed_grantees_blocked_if_any_non_roster() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc",
         "permissions": [{"emailAddress": "alice@example.com"},
                         {"emailAddress": "evil@attacker.com"}]},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "evil@attacker.com" in json.loads(result)["error"]


# ---------------------------------------------------------------------------
# Public / whole-domain sharing — blocked regardless of email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["anyone", "anyoneWithLink", "domain"])
def test_public_or_domain_scope_blocked(scope: str) -> None:
    """A public/whole-domain scope expressed as a string value is refused, and
    the error names that as the reason (not some incidental failure)."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "type": scope, "role": "reader"},
        ["alice@example.com"],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 0
    error = json.loads(result)["error"].lower()
    assert "public" in error or "domain" in error


@pytest.mark.parametrize("flag", [True, 1, "anyone"])
def test_public_share_typed_flag_blocked(flag: Any) -> None:
    """A 'make public' flag keyed by a sharing-scope name is refused even when
    it's a boolean/int the string scan can't see (HIGH-severity bypass fix)."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "public": flag},
        ["alice@example.com"],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 0
    assert "public" in json.loads(result)["error"].lower()


def test_allow_file_discovery_flag_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "allowFileDiscovery": True},
        ["alice@example.com"],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 0
    assert "public" in json.loads(result)["error"].lower()


def test_disable_link_sharing_passes() -> None:
    """Turning link sharing OFF is the safe direction — it must not be blocked."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "link_sharing": False, "public": "private"},
        [],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_share_with_no_grantee_passes() -> None:
    """A permission call carrying no email and no public scope (e.g. revoke by
    id) has nothing to leak — it must pass through."""
    gateway, session_call = _make_gateway()
    args = {"file_id": "abc", "permission_id": "p1", "operation": "revoke"}
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        assert _check_drive_share("google_workspace__manage_drive_access", args) is None
    result = _call(gateway, args, [])
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_v2_with_link_flag_blocked() -> None:
    """Legacy Drive v2 link-sharing flag ({"withLink": true}) is blocked."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "withLink": True},
        [],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 0
    assert "public" in json.loads(result)["error"].lower()


def test_benign_metadata_keys_do_not_overblock() -> None:
    """Exact-key matching means benign keys that merely contain a hint word
    (email_domain / published_at / public_id) don't trip the public-share
    block on an otherwise roster-only share."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "email_address": "alice@example.com", "role": "reader",
         "email_domain": "example.com", "published_at": "2026-01-01",
         "public_id": "xyz"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_hyphenated_scope_blocked() -> None:
    """Separator spelling variants ('anyone-with-link') normalize to the same
    blocked scope token."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"file_id": "abc", "type": "anyone-with-link", "role": "reader"},
        [],
        name="google_workspace__set_drive_file_permissions",
    )
    assert session_call.await_count == 0
    error = json.loads(result)["error"].lower()
    assert "public" in error or "domain" in error


# ---------------------------------------------------------------------------
# Tool isolation
# ---------------------------------------------------------------------------

def test_read_only_drive_tools_bypass_gate() -> None:
    """Reads carry no grantee email; they must not be gated even though their
    names contain 'permission'/'access'."""
    gateway, session_call = _make_gateway()
    for name in (
        "google_workspace__get_drive_file_permissions",
        "google_workspace__check_drive_file_public_access",
        "google_workspace__search_drive_files",
    ):
        assert _is_drive_share_tool(name) is False
        with patch.object(gw_module, "get_settings", return_value=_settings()):
            asyncio.run(gateway.call_tool({"name": name, "arguments": {"file_id": "abc"}}))
    assert session_call.await_count == 3


def test_explicit_and_fallback_tool_names_are_gated() -> None:
    assert _is_drive_share_tool("google_workspace__manage_drive_access") is True
    assert _is_drive_share_tool("google_workspace__set_drive_file_permissions") is True
    # Defensive fallback: a renamed/added mutation tool still gets gated.
    assert _is_drive_share_tool("google_workspace__update_drive_permission") is True
    # Non-namespaced or unrelated tools are not gated.
    assert _is_drive_share_tool("fetch__get") is False
    assert _is_drive_share_tool("google_workspace__get_events") is False


def test_hands_off_shares_with_anyone() -> None:
    """Approvals is the one gate: under Hands-off a share to someone not in
    People runs."""
    from openexecutive.orchestrator import levels

    gateway, session_call = _make_gateway()
    with patch.object(levels, "relaxed", return_value=True):
        result = _call(
            gateway,
            {"file_id": "abc", "email_address": "partner@other.example", "role": "reader", "type": "user"},
            ["alice@example.com"],
        )
    assert session_call.await_count == 1
    assert json.loads(result).get("id") == "perm123"


def test_below_hands_off_a_share_to_someone_not_in_people_is_a_card() -> None:
    from openexecutive.orchestrator import levels

    gateway, session_call = _make_gateway()
    seen: list[str] = []

    def card_or(blocked: str, tool: str, arguments: dict[str, Any], *, reason: str) -> str:
        seen.append(reason)
        return '{"status": "waiting_for_approval"}'

    with patch.object(levels, "card_or", side_effect=card_or):
        result = _call(
            gateway,
            {"file_id": "abc", "email_address": "partner@other.example", "role": "reader", "type": "user"},
            ["alice@example.com"],
        )
    assert session_call.await_count == 0
    assert json.loads(result)["status"] == "waiting_for_approval"
    assert seen == [levels.NEW_PERSON]
