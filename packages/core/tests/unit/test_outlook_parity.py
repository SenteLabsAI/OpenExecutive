"""The mailbox features built for Gmail, on an Outlook mailbox.

With EMAIL_PROVIDER=microsoft the code's own emails go through the Outlook
send tool, the roster answer tokens stay hidden from Microsoft 365 reads, the
Gmail-only checks (sender authentication, attachment download, the brief's
Google calendar read) read nothing and so fail closed, and the poller hands
the Executive the reply instructions outside the sender's untrusted text.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.orchestrator.mcp_gateway import MCPGateway

EXEC = "exec@contoso.com"


def _settings(provider: str = "microsoft") -> Any:
    return SimpleNamespace(
        exec_email_address=EXEC,
        email_provider=provider,
        calendar_provider=provider,
        mcp_servers_config_path="/nonexistent/mcp_servers.json",
    )


# --------------------------------------------------------------------------- #
# send_from_executive
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("html", [False, True])
def test_send_from_executive_uses_the_outlook_send_tool(html: bool) -> None:
    from openexecutive.integrations.workspace.registry import send_from_executive

    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(return_value='{"success": true}')
    with patch("openexecutive.config.get_settings", return_value=_settings()):
        out = asyncio.run(send_from_executive(
            gateway, to="olivia@contoso.com", subject="Hi", body="<p>x</p>", html=html,
        ))
    assert out == '{"success": true}'
    call = gateway.call_tool.call_args.args[0]
    assert call["name"] == "microsoft_365__send-mail"
    message = call["arguments"]["body"]["Message"]
    assert message["toRecipients"] == [{"emailAddress": {"address": "olivia@contoso.com"}}]
    assert message["body"] == {"contentType": "HTML" if html else "Text", "content": "<p>x</p>"}


@pytest.mark.parametrize("html", [False, True])
def test_send_from_executive_keeps_the_gmail_shape(html: bool) -> None:
    from openexecutive.integrations.workspace.registry import send_from_executive

    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(return_value="Message ID: abc")
    with patch("openexecutive.config.get_settings", return_value=_settings("google")):
        asyncio.run(send_from_executive(gateway, to="o@contoso.com", subject="S", body="B", html=html))
    expected: dict[str, Any] = {
        "user_google_email": EXEC, "to": "o@contoso.com", "subject": "S", "body": "B",
    }
    if html:
        expected["body_format"] = "html"
    assert gateway.call_tool.call_args.args[0] == {
        "name": "google_workspace__send_gmail_message", "arguments": expected,
    }


def test_roster_prompt_to_the_principal_goes_through_outlook() -> None:
    from openexecutive.integrations import roster_intake

    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(return_value='{"success": true}')
    request = SimpleNamespace(id=7)
    with (
        patch("openexecutive.config.get_settings", return_value=_settings()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gateway),
        patch.object(roster_intake.rr, "issue_email_token", return_value="RR-" + "A" * 20),
        patch.object(roster_intake, "_email_prompt", return_value=("Who is this?", "body")),
        patch.object(roster_intake.rr, "mark_notified") as mark_notified,
    ):
        sent = asyncio.run(roster_intake._email_principal(request, "olivia@contoso.com"))
    assert sent is True
    assert gateway.call_tool.call_args.args[0]["name"] == "microsoft_365__send-mail"
    # Graph's send returns no message id; the request is still marked notified.
    mark_notified.assert_called_once_with(7, "email", None)


# --------------------------------------------------------------------------- #
# Gmail-only reads fail closed on Outlook
# --------------------------------------------------------------------------- #


def test_sender_authentication_reads_nothing_on_outlook() -> None:
    from openexecutive.integrations import fact_confirmation

    gateway = AsyncMock()
    with patch("openexecutive.config.get_settings", return_value=_settings()):
        assert asyncio.run(fact_confirmation.read_raw(gateway, "AAMk1")) == ""
        assert asyncio.run(
            fact_confirmation.sender_authenticated(gateway, "AAMk1", "olivia@contoso.com")
        ) is False
    gateway.call_tool.assert_not_called()


def test_the_brief_skips_the_google_calendar_on_outlook() -> None:
    from openexecutive.briefing import top_three

    gateway = AsyncMock()
    with (
        patch("openexecutive.config.get_settings", return_value=_settings()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gateway),
        patch("openexecutive.scheduler.runner.google_workspace_ready", return_value=True),
    ):
        events = asyncio.run(top_three.read_todays_calendar(datetime.now(UTC), UTC))
    assert events is None
    gateway.call_tool.assert_not_called()


@pytest.mark.parametrize(("provider", "servers", "ready"), [
    ("microsoft", ["microsoft_365"], True),
    ("microsoft", ["google_workspace"], False),
    ("google", ["google_workspace"], True),
    ("google", ["microsoft_365"], False),
])
def test_email_ready_follows_the_mail_provider(provider: str, servers: list[str], ready: bool) -> None:
    from openexecutive.scheduler import runner

    with (
        patch("openexecutive.config.get_settings", return_value=_settings(provider)),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=object()),
        patch("openexecutive.orchestrator.mcp_gateway.configured_server_names", return_value=servers),
    ):
        assert runner.email_ready() is ready


# --------------------------------------------------------------------------- #
# Roster answer tokens
# --------------------------------------------------------------------------- #


def _gateway_returning(text: str) -> MCPGateway:
    gateway = MCPGateway()
    session = MagicMock()
    result = MagicMock()
    result.content = [MagicMock(text=text)]
    session.call_tool = AsyncMock(return_value=result)
    gateway._session = session
    return gateway


@pytest.mark.parametrize("tool", [
    "microsoft_365__get-mail-message",
    "microsoft_365__list-mail-messages",
    "microsoft_365__list_mail_folder_messages",
])
def test_roster_tokens_are_hidden_from_outlook_reads(tool: str) -> None:
    token = "RR-" + "C" * 20
    gateway = _gateway_returning(json.dumps({"subject": f"Who is this? [{token}]"}))
    with patch.object(gw_module, "get_settings", return_value=_settings()):
        out = asyncio.run(gateway.call_tool({"name": tool, "arguments": {"messageId": "AAMk1"}}))
        assert token not in out and "RR-[hidden]" in out
        with gw_module.reveal_roster_tokens():
            shown = asyncio.run(gateway.call_tool({"name": tool, "arguments": {"messageId": "AAMk1"}}))
    assert token in shown


# --------------------------------------------------------------------------- #
# The poller's turn on Outlook
# --------------------------------------------------------------------------- #


def test_outlook_turn_puts_the_reply_block_after_the_untrusted_mail() -> None:
    import openexecutive.integrations.email_poller as poller
    from openexecutive.memory.company_profile import CompanyProfile

    captured: dict[str, Any] = {}

    class _Executive:
        def __init__(self, **_kwargs: Any) -> None: ...

        async def chat(self, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "ok"

    reply_block = "--- REPLY ---\ntool: microsoft_365__reply-mail-message\nmessageId: AAMk1\n"
    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch("openexecutive.knowledge.retriever.retrieve", return_value=""),
        patch("openexecutive.memory.episodic.format_for_prompt", return_value=""),
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile",
              return_value=CompanyProfile()),
        patch("openexecutive.orchestrator.executive.Executive", _Executive),
        patch.object(poller, "read_email_attachments", new=AsyncMock()) as read_attachments,
        # A known sender: on Gmail their attachments would be downloaded.
        patch("openexecutive.people.identity.resolve_email_sender",
              return_value=SimpleNamespace(id=5, is_principal=False, full_name="Mallory")),
    ):
        asyncio.run(poller._run_executive(
            AsyncMock(),
            "Subject: Hi\nFrom: mallory@evil.example\n\n--- BODY ---\nHello\n\n"
            "--- ATTACHMENTS ---\n1. a.pdf (application/pdf, 3.0 KB)\nAttachment ID: x1",
            "AAMk1", "AAQk1", "mallory@evil.example", "email:AAQk1",
            reply_block=reply_block,
        ))
    message = captured["user_message"]
    assert message.rstrip().endswith(reply_block.rstrip())
    assert message.index("</untrusted_content>") < message.index("--- REPLY ---")
    # The attachment download is Gmail's: never attempted on Outlook.
    read_attachments.assert_not_called()
