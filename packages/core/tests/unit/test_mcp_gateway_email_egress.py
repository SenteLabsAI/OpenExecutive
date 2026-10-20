"""Outbound recipient allow-list for `google_workspace__send_gmail_message`.

The Executive constructs the `to:` argument itself, so prompt injection in
inbound mail could steer replies to arbitrary recipients. The egress gate
in MCPGateway.call_tool blocks any recipient (to/cc/bcc) whose address is
not on the People roster — plus the exec's own address (used by the alert
dispatcher self-send).
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

EXEC_ADDR = "exec@example.com"


@pytest.fixture(autouse=True)
def isolated_people_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh People DB so the `allowed` seed is deterministic."""
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    return db_path


def _settings() -> Any:
    return SimpleNamespace(
        exec_email_address=EXEC_ADDR,
        email_poll_interval_seconds=60,
    )


def _make_gateway() -> tuple[MCPGateway, AsyncMock]:
    """Return a gateway with a stubbed MCP session.call_tool."""
    gateway = MCPGateway()
    session = MagicMock()
    # call_tool is awaited; return a fake MCP result with .content[0].text.
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text='{"ok": true}')]
    session.call_tool = AsyncMock(return_value=fake_result)
    gateway._session = session  # bypass start()
    return gateway, session.call_tool


def _call(
    gateway: MCPGateway,
    arguments: dict[str, Any],
    allowed: list[str],
    tool_name: str = "google_workspace__send_gmail_message",
) -> str:
    # Seed each allowed address as a Person row. The egress gate reads
    # `list_people()` and the exec_email_address; nothing else.
    for i, addr in enumerate(allowed):
        people_store.upsert_person(full_name=f"Allowed {i}", email=addr)
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        return asyncio.run(
            gateway.call_tool({"name": tool_name, "arguments": arguments})
        )


def test_allowed_to_passes_through() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_disallowed_to_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "attacker@evil.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    parsed = json.loads(result)
    assert "error" in parsed
    assert "attacker@evil.com" in parsed["error"]


def test_disallowed_cc_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "cc": "attacker@evil.com",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "attacker@evil.com" in json.loads(result)["error"]


def test_disallowed_bcc_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "bcc": "attacker@evil.com",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "attacker@evil.com" in json.loads(result)["error"]


def test_self_send_permitted_with_empty_allowlist() -> None:
    """Alert dispatcher sends to exec_email_address — must keep working."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": EXEC_ADDR, "subject": "alert", "body": "body"},
        [],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_mixed_case_address_matches() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "Alice <Alice@Example.COM>", "subject": "hi", "body": "body"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_comma_list_partially_disallowed_is_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com, attacker@evil.com",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "attacker@evil.com" in json.loads(result)["error"]


def test_other_tool_bypasses_gate() -> None:
    """Non-email tools must not be affected by this gate."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"query": "is:unread"},
        [],  # empty allow-list — would block email, but this isn't email.
        tool_name="google_workspace__search_gmail_messages",
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


# ---------------------------------------------------------------------------
# Hardening: sibling tools, smuggling vectors, type confusion
# ---------------------------------------------------------------------------


def test_draft_gmail_message_is_gated() -> None:
    """Drafts can carry attacker recipients just like sends — must be gated.
    (workspace-mcp 1.21.1 renamed create_gmail_draft → draft_gmail_message.)"""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "attacker@evil.com", "subject": "hi", "body": "body"},
        ["alice@example.com"],
        tool_name="google_workspace__draft_gmail_message",
    )
    assert session_call.await_count == 0
    assert "attacker@evil.com" in json.loads(result)["error"]


def test_send_with_thread_reply_to_non_roster_is_gated() -> None:
    """A reply is send_gmail_message with thread_id; the recipient still must
    be on the roster."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "attacker@evil.com", "thread_id": "t1", "subject": "re", "body": "b"},
        ["alice@example.com"],
        tool_name="google_workspace__send_gmail_message",
    )
    assert session_call.await_count == 0
    assert "attacker@evil.com" in json.loads(result)["error"]


def test_new_191_arg_keys_allowed() -> None:
    """body_format / from_email / include_signature / quote_original are valid
    1.21.1 params (not recipients) and must NOT be rejected as unknown keys."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "b",
         "body_format": "html", "from_email": "exec-alias@example.com",
         "include_signature": True, "quote_original": False},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert "error" not in json.loads(result)


@pytest.mark.parametrize(
    "tool_name",
    ["google_workspace__send_gmail_message", "google_workspace__draft_gmail_message"],
)
@pytest.mark.parametrize(
    "extra",
    [
        # workspace-mcp derives To/Cc from the thread — never roster-checked.
        {"thread_id": "t1", "reply_all": True},
        # Forwards an arbitrary inbox message (and its attachments) out.
        {"forward_message_id": "m1"},
        {"forward_message_id": "m1", "include_forwarded_attachments": True},
    ],
)
def test_workspace_mcp_1_29_recipient_and_forward_args_rejected(
    tool_name: str, extra: dict[str, Any]
) -> None:
    """workspace-mcp 1.29.0 added reply_all / forward_message_id /
    include_forwarded_attachments to send_gmail_message. They move recipients or
    inbox content past the roster check, so they must stay off the arg
    allow-list even when the explicit `to` is on the roster."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "b", **extra},
        ["alice@example.com"],
        tool_name=tool_name,
    )
    assert session_call.await_count == 0
    assert "not permitted" in json.loads(result)["error"]


def test_from_email_control_char_blocked() -> None:
    """from_email lands in the From header — a control char is header injection."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "b",
         "from_email": "exec@example.com\nBcc: evil@x.com"},
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "control character" in json.loads(result)["error"].lower()


def test_headers_arg_rejected() -> None:
    """Custom headers could smuggle a Bcc past the field-level check."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "headers": {"Bcc": "attacker@evil.com"},
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "forbidden" in json.loads(result)["error"].lower() or "headers" in result.lower()


def test_raw_mime_arg_rejected() -> None:
    """A raw RFC822 blob could carry its own To/Bcc — must be rejected."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "raw": "To: attacker@evil.com\r\nSubject: x\r\n\r\nbody",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "forbidden" in json.loads(result)["error"].lower() or "raw" in result.lower()


def test_non_string_recipient_rejected() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": {"email": "attacker@evil.com"},  # type: ignore[dict-item]
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "non-string" in json.loads(result)["error"].lower()


def test_threading_metadata_keys_allowed() -> None:
    """in_reply_to / references carry Message-IDs (not addresses) and the
    Executive needs them to thread replies correctly. They must pass."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "Re: thing",
            "body": "body",
            "thread_id": "t1",
            "in_reply_to": "<abc@mail.gmail.com>",
            "references": "<abc@mail.gmail.com> <def@mail.gmail.com>",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_unknown_arg_key_rejected() -> None:
    """Allow-list of keys: any unfamiliar key (e.g. `attachments_inline`, a
    typo, or a future smuggling vector) must be rejected so the gate stays
    safe by default."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "additional_headers": {"Bcc": "attacker@evil.com"},
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "additional_headers" in result


def test_malformed_recipient_rejected() -> None:
    """Non-empty input that getaddresses parses to empty addr must fail
    closed — otherwise a lenient downstream mailer might still deliver."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "<>",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "unparseable" in json.loads(result)["error"].lower()


def test_unparseable_garbage_still_blocked() -> None:
    """Even when getaddresses extracts something (e.g. 'garbage' from
    'garbage no at sign'), it still won't match the allow-list and must
    be blocked. This pins the general fail-closed property."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "garbage no at sign here",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "error" in json.loads(result)


def test_crlf_line_endings_in_strip_path() -> None:
    """Sanity: addresses with stray CR (not as line separator) still reject."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com\r\nBcc: attacker@evil.com",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "newline" in json.loads(result)["error"].lower()


def test_newline_in_recipient_rejected() -> None:
    """Header-injection via embedded CRLF in the to: field."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com\nBcc: attacker@evil.com",
            "subject": "hi",
            "body": "body",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "newline" in json.loads(result)["error"].lower()


# ---------------------------------------------------------------------------
# from_name (Gmail "Send As" display name)
#
# Regression: a legitimate reply to a rostered recipient was dropped because
# the Executive passed `from_name` — a tool-documented display-name field that
# wasn't on the arg-key allow-list. It is now allowed but validated, since a
# display name lands verbatim in the From header and is never parsed by
# getaddresses. See session email:19e7225e686d0847.
# ---------------------------------------------------------------------------


def test_from_name_with_valid_recipient_passes() -> None:
    """The exact regression shape: reply to a rostered recipient carrying a
    Send-As display name. Must pass — from_name is not a recipient."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "Re: status",
            "body": "body",
            "from_name": "Open Executive",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_empty_from_name_passes() -> None:
    """An empty display name is harmless and must not be blocked."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {"to": "alice@example.com", "subject": "hi", "body": "body", "from_name": ""},
        ["alice@example.com"],
    )
    assert session_call.await_count == 1
    assert result == '{"ok": true}'


def test_from_name_with_newline_blocked() -> None:
    """Header-injection via a display name like "Exec\\nBcc: evil@x.com"."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "from_name": "Exec\nBcc: attacker@evil.com",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    err = json.loads(result)["error"]
    assert "header-injection" in err
    # Must NOT misdescribe a display-name field as a forbidden recipient.
    assert "EMAIL_ALLOWED_SENDERS" not in err


def test_from_name_with_carriage_return_blocked() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "from_name": "Exec\rBcc: attacker@evil.com",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    err = json.loads(result)["error"]
    assert "header-injection" in err
    assert "EMAIL_ALLOWED_SENDERS" not in err


def test_from_name_with_other_control_char_blocked() -> None:
    """The guard rejects the whole C0 range, not just CR/LF — a NUL or other
    control char in a display name has no legitimate use and is refused."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "from_name": "Exec\x00admin",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "control character" in json.loads(result)["error"]


def test_from_name_non_string_rejected() -> None:
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "from_name": ["Open", "Executive"],  # type: ignore[dict-item]
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    assert "from_name must be a string" in json.loads(result)["error"]


def test_valid_from_name_does_not_relax_recipient_gating() -> None:
    """A benign from_name must not let an unrostered recipient through."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "attacker@evil.com",
            "subject": "hi",
            "body": "body",
            "from_name": "Open Executive",
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    err = json.loads(result)["error"]
    assert "attacker@evil.com" in err
    assert "EMAIL_ALLOWED_SENDERS" in err


def test_forbidden_arg_key_error_is_accurate() -> None:
    """A genuinely disallowed key is still rejected, but the error must
    describe an argument — not a phantom recipient on EMAIL_ALLOWED_SENDERS."""
    gateway, session_call = _make_gateway()
    result = _call(
        gateway,
        {
            "to": "alice@example.com",
            "subject": "hi",
            "body": "body",
            "additional_headers": {"Bcc": "attacker@evil.com"},
        },
        ["alice@example.com"],
    )
    assert session_call.await_count == 0
    err = json.loads(result)["error"]
    assert "additional_headers" in err
    assert "not permitted" in err
    assert "EMAIL_ALLOWED_SENDERS" not in err
