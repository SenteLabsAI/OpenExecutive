"""Redaction helpers for audit-log entries that record tool inputs/outputs.

Tool inputs and outputs can contain secrets (OAuth tokens, auth codes,
session cookies) or PII (raw email bodies, calendar attendees, drive file
contents). Persisting them verbatim into the audit table would turn the
audit log itself into a leak vector. These helpers keep the audit trail
useful — "the Executive called gmail.send" — without copying payloads.
"""
from __future__ import annotations

import re
from typing import Any

_INPUT_LEN = 140
_RESULT_LEN = 300

# How much of an exception we keep when recording a failure in an audit
# `details.error`. Long enough to name the failure, short enough to keep an
# audit row scannable. Shared so the orchestrator, the chat tools and the
# inbound adapters cannot drift to three different lengths (they had).
# Note this is the AUDIT cap: what the model is handed for a crashed tool is
# the exception's type only, never its message — see
# `orchestrator.executive._tool_error_result`.
ERROR_DETAIL_LEN = 300

# Tool names whose input AND output are never logged verbatim. Their result
# preview is replaced with "<redacted>". Match is substring, case-insensitive,
# so both `authenticate` and `mcp_plugin_productivity_slack__authenticate`
# match the same rule.
_SENSITIVE_SUBSTRINGS = (
    "authenticate",
    "complete_authentication",
    "oauth",
    "credential",
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    # Email/chat content tools — bodies are too long and may contain PII.
    "get_gmail_message_content",
    "read_file_content",
    "download_file_content",
    "get_thread",
    "search_threads",
    "get_event",
    # Microsoft 365 (ms-365-mcp-server) mail/calendar readers — hyphenated
    # names, so the underscore entries above do not match them. Graph list/get
    # responses carry full bodies, attendee lists and inline attachment bytes.
    # Substrings on purpose: "mail-message" covers get-/list-mail-messages/
    # update-/…, "folder-messages" the folder-scoped listing (its name has no
    # "mail-message" run), "calendar-event" the get/list/specific variants.
    "mail-message",
    "folder-messages",
    "mail-attachment",
    "download-bytes",
    "calendar-event",
    "calendar-view",
    "get-schedule",
    "find-meeting-times",
)

# Keys inside a tool_input dict whose values are stripped before being
# stringified. The key itself stays so the audit row shows that, e.g., a
# password was set, just not what it was.
_SENSITIVE_KEYS = re.compile(
    r"(token|secret|password|api[_-]?key|credential|authorization|cookie|access[_-]?token)",
    re.IGNORECASE,
)


def _is_sensitive_tool(name: str) -> bool:
    lowered = name.lower()
    return any(s in lowered for s in _SENSITIVE_SUBSTRINGS)


def _scrub_input(value: Any) -> Any:
    """Walk a nested input value, replacing sensitive-keyed values with <redacted>."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _SENSITIVE_KEYS.search(k) else _scrub_input(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_input(v) for v in value]
    return value


def audit_tool_input(tool_name: str, tool_input: Any) -> str:
    """Render a tool input for an audit row's `summary` line."""
    if _is_sensitive_tool(tool_name):
        return "<redacted: sensitive tool>"
    scrubbed = _scrub_input(tool_input)
    s = str(scrubbed)
    return s[:_INPUT_LEN]


def audit_tool_result(tool_name: str, result: Any) -> str:
    """Render a tool result preview for an audit row's `details_json`."""
    if _is_sensitive_tool(tool_name):
        return "<redacted: sensitive tool>"
    s = str(result)
    return s[:_RESULT_LEN]


def audit_tool_input_full(tool_name: str, tool_input: Any) -> Any:
    """Un-truncated tool input for the drill-down `full_json` payload.

    Returns the scrubbed input value (keys like `password` still replaced
    with `<redacted>`) for non-sensitive tools, or the redaction marker
    string for tools whose payload is never safe to persist verbatim.
    """
    if _is_sensitive_tool(tool_name):
        return "<redacted: sensitive tool>"
    return _scrub_input(tool_input)


def audit_tool_result_full(tool_name: str, result: Any) -> Any:
    """Un-truncated tool result for the drill-down `full_json` payload.

    Scrubs sensitive keys even for non-sensitive tools: a tool not on the
    sensitive list can still echo back an `access_token`, `password`, or
    `api_key` in its response (auth flows, profile endpoints), and
    persisting that verbatim would turn the audit log into a credential
    dump on its own.
    """
    if _is_sensitive_tool(tool_name):
        return "<redacted: sensitive tool>"
    return _scrub_input(result)
