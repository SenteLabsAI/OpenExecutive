"""Pick the mail / calendar backend from settings.

``EMAIL_PROVIDER`` and ``CALENDAR_PROVIDER`` are separate on purpose (Outlook
mail with Google Calendar is a legitimate combination). Both default to
``google``. The lookup tolerates a settings object without the fields (test
stubs) by falling back to the default, and never raises on an unknown value —
that is rejected at `Settings` construction by the ``Literal`` type.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openexecutive.integrations.workspace.calendar import CalendarProvider
from openexecutive.integrations.workspace.google import GoogleCalendar, GoogleMail
from openexecutive.integrations.workspace.mail import MailProvider
from openexecutive.integrations.workspace.microsoft import MicrosoftCalendar, MicrosoftMail

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "google"

_MAIL: dict[str, type] = {"google": GoogleMail, "microsoft": MicrosoftMail}
_CALENDAR: dict[str, type] = {"google": GoogleCalendar, "microsoft": MicrosoftCalendar}


def _choice(settings: Any, attr: str, backends: dict[str, type]) -> str:
    if settings is None:
        from openexecutive.config import get_settings

        settings = get_settings()
    value = getattr(settings, attr, DEFAULT_PROVIDER)
    if isinstance(value, str) and value.strip().lower() in backends:
        return value.strip().lower()
    return DEFAULT_PROVIDER


def get_mail_provider(settings: Any = None) -> MailProvider:
    """The mail backend for ``settings.email_provider`` (default ``google``)."""
    provider: MailProvider = _MAIL[_choice(settings, "email_provider", _MAIL)]()
    return provider


def get_calendar_provider(settings: Any = None) -> CalendarProvider:
    """The calendar backend for ``settings.calendar_provider`` (default ``google``)."""
    provider: CalendarProvider = _CALENDAR[_choice(settings, "calendar_provider", _CALENDAR)]()
    return provider


def provider_server_missing(server_name: str, config_path: Path) -> bool:
    """True when the MCP config defines servers but not ``server_name``.

    Used at startup and by the poller to say *why* a provider cannot work,
    instead of a stream of "tool not found" errors. An empty/absent config is
    reported by `main._start_mcp_gateway` already, so it is not "missing" here.
    """
    from openexecutive.orchestrator.mcp_gateway import configured_server_names

    names = configured_server_names(config_path)
    return bool(names) and server_name not in names


def log_provider_status(settings: Any, config_path: Path) -> None:
    """One INFO line per backend at startup, plus a WARNING when the chosen
    backend's MCP server is not in the config (the switch is fail-soft)."""
    mail = get_mail_provider(settings)
    calendar = get_calendar_provider(settings)
    logger.info(
        "workspace providers: email=%s (%s) calendar=%s (%s)",
        mail.name, mail.server_name, calendar.name, calendar.server_name,
    )
    for label, provider in (("EMAIL_PROVIDER", mail), ("CALENDAR_PROVIDER", calendar)):
        if provider_server_missing(provider.server_name, config_path):
            logger.warning(
                "%s=%s but MCP server '%s' is not defined in %s — its mail/calendar "
                "calls will fail until the server entry is added",
                label, provider.name, provider.server_name, config_path,
            )
    # The gateway's Microsoft 365 egress gate is keyed to the literal server
    # name `microsoft_365`. A server that looks like the Microsoft one under a
    # different name would expose the same tools with NO roster gate — say so.
    from openexecutive.orchestrator.mcp_gateway import configured_server_names

    for name in configured_server_names(config_path):
        low = name.lower()
        if name != "microsoft_365" and any(k in low for k in ("365", "microsoft", "outlook", "graph")):
            logger.warning(
                "MCP server '%s' looks like a Microsoft 365 server but is not named "
                "'microsoft_365' — the gateway's roster egress gate only covers tools "
                "under the microsoft_365__ prefix; rename the entry",
                name,
            )


async def send_from_executive(
    gateway: Any, *, to: str, subject: str, body: str, html: bool = False,
) -> str:
    """Send one message from the Executive's own mailbox through the
    configured mail backend (``EMAIL_PROVIDER``) and return the tool result.

    For the fixed messages the code itself writes (a confirmation to the
    principal, a scheduled delivery): the provider spells the argument shape,
    and the gateway's egress gates run on the call as on any other.
    """
    from openexecutive.config import get_settings

    settings = get_settings()
    provider = get_mail_provider(settings)
    result = await gateway.call_tool({
        "name": provider.send_tool_name,
        "arguments": provider.build_send_arguments(
            mailbox=settings.exec_email_address, to=to, subject=subject, body=body, html=html,
        ),
    })
    return str(result)
