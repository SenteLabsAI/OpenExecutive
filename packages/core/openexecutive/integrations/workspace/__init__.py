"""Workspace backends: the mailbox and calendar the Executive itself runs on.

The Executive can *discover* any mail/calendar tool its MCP gateway exposes,
but three code paths call a fixed backend without a model in the loop — the
inbound mailbox poller (`integrations.email_poller`), alert email dispatch
(`alerts.dispatcher`) plus the scheduler's email hint, and the typed calendar
booking tools (`orchestrator.calendar_tools`) with their caps and approval
ledger. Those paths go through the two Protocols here so the backend is a
setting (`EMAIL_PROVIDER` / `CALENDAR_PROVIDER`: ``google`` or ``microsoft``)
rather than a Gmail tool name baked into each call site.

Layout mirrors `openexecutive.providers`: one Protocol module per concern
(`mail`, `calendar`), one module per backend (`google`, `microsoft`), and a
`registry` that picks from settings. Defaults are ``google``, so an existing
install changes nothing until an operator opts in.
"""
from openexecutive.integrations.workspace.calendar import CalendarProvider
from openexecutive.integrations.workspace.mail import (
    InboundMessage,
    MailProvider,
    MessageRef,
    render_for_executive,
)
from openexecutive.integrations.workspace.registry import (
    get_calendar_provider,
    get_mail_provider,
)

__all__ = [
    "CalendarProvider",
    "InboundMessage",
    "MailProvider",
    "MessageRef",
    "get_calendar_provider",
    "get_mail_provider",
    "render_for_executive",
]
