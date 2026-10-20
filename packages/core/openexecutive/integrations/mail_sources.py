"""Mail the Executive reads from somewhere other than its own mailbox.

The Executive's own mailbox (Gmail or Outlook, ``EMAIL_PROVIDER``) is read by
``integrations/email_poller.py``. Anything else that receives mail for it (a
hosted inbound address, a relay, another mailbox) is a *mail source*: code that
fetches each message, hands it to ``handle_inbound`` with an ``InboundMail``,
and lets the same rules decide what happens to it as for the mailbox's own
mail: who it is from (the People list, a contact, a stranger held for the
principal), what is private to the principal, what is labelled untrusted, and
what the Executive's turn sees.

A source registers itself before the app starts (``register_mail_source``);
the app's lifespan starts every registered source that says it is enabled
(``start_mail_sources``) and cancels it on shutdown. Nothing here is tied to a
vendor, and nothing is registered unless a deployment adds a source.

What a source decides for itself, because only it knows: how messages arrive,
whether a sender's address is proven theirs (``prove_sender``), how the
answer goes out (``send_answer``), how attachments are read
(``read_attachments``), and when a message counts as done.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openexecutive.integrations.workspace.mail import InboundMessage

if TYPE_CHECKING:
    from openexecutive.integrations.email_attachments import EmailAttachments
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

# The key a held message's payload names its source under, so a replay goes
# back to the source that read it. The mailbox's own held mail carries none.
SOURCE_KEY = "mail_source"
# What ``handle_inbound`` returns while the Executive is paused: nothing was
# done, so the source keeps the message for later.
PAUSED = "paused"


async def _unproven() -> bool:
    return False


@dataclass
class InboundMail:
    """One message a mail source hands ``handle_inbound``.

    ``message`` carries the sender as a bare address (parsed, never a raw
    From header). ``source`` names the source in the audit log and in a held
    message's payload. ``own_address`` is the address the mail came to; mail
    from it is dropped, so the Executive never answers itself.
    """

    message: InboundMessage
    source: str
    own_address: str
    # How the turn's answer goes out: ``send_answer(to, body, in_thread)``
    # mails ``body`` (Markdown, the AI line already in it) from the address,
    # threaded under the message when ``in_thread``, and returns None once
    # sent, else what went wrong in a line. The Executive has no mail tool to
    # call; its answer goes to the sender, or to the principal when the
    # sender is a contact or a teammate nothing proves.
    send_answer: Callable[[str, str, bool], Awaitable[str | None]]
    # Whether the From address is proven the sender's (DMARC, or SPF and DKIM
    # aligned). Unproven, the principal's own mail can't ask for a standing
    # fact, a teammate's turn isn't theirs alone, and a stranger isn't told
    # their mail is waiting.
    prove_sender: Callable[[], Awaitable[bool]] = _unproven
    # The readable attachments, for the turn: their text ("" for none), or an
    # ``EmailAttachments`` when there are images too. Called only for a
    # sender the principal knows.
    read_attachments: Callable[[], Awaitable[str | EmailAttachments]] | None = None
    # What a held message keeps so ``MailSource.replay`` can handle it again
    # once the principal says who the sender is.
    hold_payload: dict[str, Any] = field(default_factory=dict)
    # The conversation this message belongs to ("" → one per thread id).
    session_id: str = ""
    # Ceiling on the Executive's turn, in seconds (0 → EMAIL_TURN_TIMEOUT_S).
    turn_timeout_s: float = 0.0


@dataclass(frozen=True)
class MailSource:
    """A registered mail source.

    ``run`` is its loop, started as a task in the app's lifespan when
    ``enabled()`` says so and cancelled on shutdown. ``replay`` handles a
    message held for the principal (its ``hold_payload``) once its sender is
    on the People list; it returns whether it did.
    """

    name: str
    run: Callable[[], Coroutine[Any, Any, None]]
    enabled: Callable[[], bool] = lambda: True
    replay: Callable[[dict[str, Any]], Awaitable[bool]] | None = None


_SOURCES: dict[str, MailSource] = {}


def register_mail_source(source: MailSource) -> None:
    """Add ``source`` (replacing one of the same name). Call it before the
    app starts."""
    if not source.name or source.name in ("google", "microsoft"):
        raise ValueError(f"a mail source can't be named {source.name!r}")
    _SOURCES[source.name] = source


def registered() -> list[MailSource]:
    return list(_SOURCES.values())


def start_mail_sources() -> list[asyncio.Task[None]]:
    """Start every enabled source's loop; the caller cancels the tasks on
    shutdown. A source whose ``enabled`` raises is left off."""
    tasks: list[asyncio.Task[None]] = []
    sources = registered()
    if sources:
        from openexecutive.integrations.roster_intake import register_replayer

        register_replayer("email", replay_held_mail)
    for source in sources:
        try:
            on = source.enabled()
        except Exception:
            logger.exception("mail source %s: could not tell whether it is enabled", source.name)
            continue
        if on:
            logger.info("mail source %s: starting", source.name)
            task = asyncio.create_task(source.run(), name=f"mail-source-{source.name}")
            task.add_done_callback(_log_if_crashed)
            tasks.append(task)
    return tasks


def _log_if_crashed(task: asyncio.Task[None]) -> None:
    """A source's loop that ended with an error stops reading mail: say so
    when it happens, not at shutdown."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("%s stopped: no more mail is read from it", task.get_name(), exc_info=exc)


async def replay_held_mail(message: Any, request: Any) -> bool:
    """The ``email`` replayer: a held message goes back to the source that
    read it, and the mailbox's own (no source named) to the email poller."""
    payload = message.payload if isinstance(getattr(message, "payload", None), dict) else {}
    name = payload.get(SOURCE_KEY)
    if not name:
        from openexecutive.integrations.email_poller import replay_held_email

        return await replay_held_email(message, request)
    source = _SOURCES.get(str(name))
    if source is None or source.replay is None:
        logger.warning("mail source %s: a held message can't be replayed (not registered)", name)
        return False
    return await source.replay(payload)


async def handle_inbound(mail: InboundMail, gateway: MCPGateway | None = None) -> str:
    """Handle one message from a mail source under the mailbox's rules;
    returns what happened, for the log. While the Executive is paused it does
    nothing and returns ``PAUSED``: keep the message and hand it over again
    later. ``gateway`` is the MCP gateway the Executive's turn may use (the
    active one when None). ``mail.source`` must be registered."""
    from openexecutive.integrations import email_poller

    return await email_poller.handle_inbound(mail, gateway)


__all__ = [
    "PAUSED", "InboundMail", "MailSource", "SOURCE_KEY", "handle_inbound", "register_mail_source",
    "registered", "replay_held_mail", "start_mail_sources",
]
