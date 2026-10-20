"""alerts.dispatcher.dispatch_email — a self-send through the configured mail backend.

Previously untested (and it returned True even when the send tool reported an
in-band error). Pins both backends' argument dicts, the no-gateway path, and
the soft-error → False fix.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from openexecutive.alerts.dispatcher import dispatch_email
from openexecutive.alerts.models import Alert

EXEC = "exec@example.com"


def _alert() -> Alert:
    return Alert(
        id=7,
        source="email",
        external_id="x-1",
        severity="high",
        headline="Vendor outage",
        body="Acme is down.",
        suggested_action="Call Acme",
        created_at="2026-09-18T00:00:00+00:00",
    )


def _settings(provider: str = "google") -> Any:
    return SimpleNamespace(exec_email_address=EXEC, email_provider=provider)


def _run(provider: str, result: str = '{"ok": true}') -> tuple[bool, Any]:
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=result)
    with (
        patch("openexecutive.config.get_settings", return_value=_settings(provider)),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
    ):
        ok = asyncio.run(dispatch_email(_alert()))
    return ok, gw


def test_google_self_send_arguments() -> None:
    ok, gw = _run("google")
    assert ok is True
    assert gw.call_tool.call_args.args[0] == {
        "name": "google_workspace__send_gmail_message",
        "arguments": {
            "user_google_email": EXEC,
            "to": EXEC,
            "subject": "[HIGH] Vendor outage",
            "body": "Acme is down.\n\nSuggested action: Call Acme",
        },
    }


def test_microsoft_self_send_arguments() -> None:
    ok, gw = _run("microsoft")
    assert ok is True
    assert gw.call_tool.call_args.args[0] == {
        "name": "microsoft_365__send-mail",
        "arguments": {"body": {
            "Message": {
                "subject": "[HIGH] Vendor outage",
                "body": {"contentType": "Text", "content": "Acme is down.\n\nSuggested action: Call Acme"},
                "toRecipients": [{"emailAddress": {"address": EXEC}}],
            },
            "SaveToSentItems": True,
        }},
    }


def test_soft_error_result_is_a_failed_delivery() -> None:
    ok, _ = _run("google", result=json.dumps({"error": "recipient blocked"}))
    assert ok is False


def test_no_gateway_is_a_failed_delivery() -> None:
    with (
        patch("openexecutive.config.get_settings", return_value=_settings()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
    ):
        assert asyncio.run(dispatch_email(_alert())) is False


def test_exception_is_a_failed_delivery() -> None:
    gw = MagicMock()
    gw.call_tool = AsyncMock(side_effect=RuntimeError("down"))
    with (
        patch("openexecutive.config.get_settings", return_value=_settings()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
    ):
        assert asyncio.run(dispatch_email(_alert())) is False
