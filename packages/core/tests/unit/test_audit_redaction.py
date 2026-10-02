"""Unit tests for audit log secret/PII redaction."""
from __future__ import annotations

from openexecutive.audit.redaction import audit_tool_input, audit_tool_result


def test_sensitive_tool_name_redacted() -> None:
    assert "<redacted" in audit_tool_input("authenticate", {"code": "abc123"})
    assert "<redacted" in audit_tool_result(
        "complete_authentication", '{"access_token": "ya29.xyz"}'
    )
    assert "<redacted" in audit_tool_result(
        "google_workspace__get_gmail_message_content", "From: secret"
    )
    # Microsoft 365 readers are hyphenated; the Gmail entry must not be the
    # only mail-body rule.
    assert "<redacted" in audit_tool_result(
        "microsoft_365__get-mail-message", '{"body": {"content": "secret"}}'
    )
    assert "<redacted" in audit_tool_result("microsoft_365__list-mail-messages", "[]")
    assert "<redacted" in audit_tool_result("microsoft_365__get-calendar-view", "[]")
    assert "<redacted" in audit_tool_result("microsoft_365__list-mail-attachments", '{"contentBytes": "x"}')
    assert "<redacted" in audit_tool_result("microsoft_365__download-bytes", '{"contentBytes": "x"}')
    assert "<redacted" in audit_tool_result("microsoft_365__list-specific-calendar-events", "[]")
    assert "<redacted" in audit_tool_result("microsoft_365__list-mail-folder-messages", "[]")
    assert audit_tool_result("microsoft_365__send-mail", '{"success": true}') == '{"success": true}'


def test_sensitive_keys_scrubbed_in_input() -> None:
    out = audit_tool_input("ordinary_tool", {"username": "alice", "password": "hunter2"})
    assert "alice" in out
    assert "hunter2" not in out
    assert "<redacted>" in out


def test_non_sensitive_tool_passthrough() -> None:
    out = audit_tool_input("send_message", {"text": "hello world"})
    assert "hello world" in out
    assert audit_tool_result("send_message", '{"ok": true}') == '{"ok": true}'


def test_input_and_result_are_truncated() -> None:
    long_text = "x" * 1000
    assert len(audit_tool_input("plain", {"v": long_text})) <= 140
    assert len(audit_tool_result("plain", long_text)) <= 300


def test_nested_sensitive_keys_scrubbed() -> None:
    out = audit_tool_input(
        "ordinary_tool",
        {"headers": {"Authorization": "Bearer secret"}, "body": "ok"},
    )
    assert "Bearer secret" not in out
    assert "ok" in out
