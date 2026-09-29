"""Email confirmation for standing facts and company-profile edits.

The principal can ask for a correction by email ("Maple House is 48 units,
not 52"), and the fact tools check it exactly as they would in chat
(``orchestrator.fact_tools``). But a From line proves nothing: anyone can
send mail that claims to be the principal, and a standing fact is read by
every later prompt as their own account. So an emailed change is held, never
applied, until the principal's own mailbox confirms it:

1. ``request_confirmation`` stores the change with the hash of a fresh
   one-time token (``memory.facts.hold_confirmation``) and emails the token
   to the principal's primary address — the address on the roster, not the
   From line. The token never reaches a model: the tool result leaves it out,
   and the MCP gateway hides it from every read of the Executive's mailbox
   (``mcp_gateway.hide_roster_tokens``), where the sent email sits.
2. The principal replies CONFIRM (or CANCEL). The email poller hands every
   inbound message to ``try_email_fact_confirmation`` before any model turn,
   as it does for roster-request answers. A reply counts only from the
   principal's primary address, carrying a pending token, and not failing
   DMARC; the change is then applied (``fact_tools.apply_confirmed``) and the
   principal is told what happened.

A token works once (a compare-and-set, so two replies cannot apply it twice)
and expires after ``memory.facts.CONFIRM_TTL``. At most
``MAX_PENDING_CONFIRMATIONS`` changes wait at once, so a stream of mail cannot
bury the principal's inbox in confirmation requests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from openexecutive.memory import facts

logger = logging.getLogger(__name__)

_CONFIRM_WORDS = re.compile(r"\b(confirm|confirmed|yes|approve|approved)\b", re.IGNORECASE)
_CANCEL_WORDS = re.compile(r"\b(cancel|cancelled|no|reject|don'?t|do not)\b", re.IGNORECASE)


def _principal_address() -> str:
    """The principal's primary address — where confirmations go, and the only
    address a confirming reply may come from."""
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return (principal.email or "").strip().lower() if principal is not None else ""


def dmarc_failed(raw: str) -> bool:
    """Whether the headers the Gmail tool printed report a DMARC failure."""
    from openexecutive.integrations.email_poller import _split_gmail_content

    header, _body, _att = _split_gmail_content(raw)
    return any(
        line.strip().lower().startswith("authentication-results:") and "dmarc=fail" in line.lower()
        for line in header
    )


def _audit(summary: str, details: dict[str, Any]) -> None:
    """A private ``fact_confirmation`` row: it names the change the principal
    asked for, which is theirs until they confirm it."""
    try:
        from openexecutive.audit import log_event

        log_event("fact_confirmation", summary, actor="executive", details=details, private=True)
    except Exception:  # noqa: BLE001 - audit must never break the email path.
        logger.warning("fact_confirmation: audit failed", exc_info=True)


def _confirmation_email(summary: str, token: str) -> tuple[str, str]:
    subject = f"Confirm a change to what I keep as fact [{token}]"
    body = (
        "You asked me by email to make this change:\n\n"
        f"  {summary}\n\n"
        "Email can be forged, so nothing has changed yet. Reply CONFIRM to this "
        "email from your own address to apply it, or CANCEL to drop it. If you "
        "didn't ask for this, ignore this email and it expires on its own.\n\n"
        f"(Reference {token} — keep it in your reply. It works once and expires "
        f"in {facts.CONFIRM_TTL.days} days.)"
    )
    return subject, body


async def _send(to: str, subject: str, body: str) -> str:
    """Send one email from the Executive's mailbox, outside any turn's
    outbound context. Returns the tool result (an error payload on failure)."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.orchestrator.schedule_tools import set_session

    gateway = get_active_gateway()
    if gateway is None:
        return json.dumps({"error": "email is not connected"})
    with set_session(None):
        result = await gateway.call_tool({
            "name": "google_workspace__send_gmail_message",
            "arguments": {
                "user_google_email": get_settings().exec_email_address,
                "to": to,
                "subject": subject,
                "body": body,
            },
        })
    return str(result)


async def request_confirmation(action: dict[str, Any], summary: str) -> str | None:
    """Hold ``action`` and email the principal a one-time token to confirm
    it. Returns None when the request went out, else why it did not (the
    change is then dropped). Never raises."""
    from openexecutive.workflows.action_step import looks_like_error

    try:
        if await asyncio.to_thread(facts.pending_confirmation_count) >= facts.MAX_PENDING_CONFIRMATIONS:
            return (
                f"{facts.MAX_PENDING_CONFIRMATIONS} emailed changes are already waiting for "
                "the principal to confirm them. Ask them to answer those first, or make "
                "this change in the web app."
            )
        to = await asyncio.to_thread(_principal_address)
        if not to:
            return "there is no principal email address on the People list to confirm this with"
        conf_id, token = await asyncio.to_thread(facts.hold_confirmation, action, summary)
        subject, body = _confirmation_email(summary, token)
        try:
            result = await _send(to, subject, body)
        except Exception as exc:  # noqa: BLE001 - reported as a failed send below.
            result = json.dumps({"error": type(exc).__name__})
        if looks_like_error(result):
            await asyncio.to_thread(facts.decide_confirmation, conf_id, "cancelled")
            logger.warning("fact_confirmation: confirmation email failed: %s", result[:200])
            return "the confirmation email could not be sent, so nothing was held; ask the principal to make this change in the web app"
        _audit(
            "An emailed change is waiting for the principal's confirmation",
            {"confirmation_id": conf_id, "tool": action.get("tool"), "status": "held"},
        )
        return None
    except Exception:
        logger.exception("fact_confirmation: holding a change failed")
        return "the change could not be held for confirmation; ask the principal to make it in the web app"


def _decision(text: str) -> str:
    """"confirm", "cancel" or "" (unclear) from the principal's reply."""
    words = text[:400]
    yes, no = bool(_CONFIRM_WORDS.search(words)), bool(_CANCEL_WORDS.search(words))
    if yes and not no:
        return "confirm"
    if no and not yes:
        return "cancel"
    return ""


def _parsed(result: str) -> dict[str, Any]:
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return {"error": "unexpected result"}
    return parsed if isinstance(parsed, dict) else {"error": "unexpected result"}


def _applied(result: str) -> bool:
    return "error" not in _parsed(result)


def _outcome_line(summary: str, result: str) -> str:
    parsed = _parsed(result)
    if "error" not in parsed:
        return f"Done. This now holds everywhere:\n\n  {summary}"
    return f"I couldn't apply it: {parsed['error']}\n\nThe change was:\n\n  {summary}"


async def try_email_fact_confirmation(
    gateway: Any, raw: str, from_addr: str, message_id: str
) -> bool:
    """Handle an email that answers a fact confirmation; True when it did
    (the caller marks it read and stops). It must come from the principal's
    primary address, exactly, carry a token this module issued, and not be an
    automatic reply. Anything else is left to the ordinary path, where the
    token is hidden from the model."""
    from openexecutive.integrations.email_poller import _split_gmail_content, sender_new_text
    from openexecutive.integrations.roster_intake import _auto_or_bulk_headers
    from openexecutive.orchestrator.fact_tools import apply_confirmed

    tokens = facts.find_confirmation_tokens(raw)
    if not tokens:
        return False
    principal = await asyncio.to_thread(_principal_address)
    if not principal or from_addr.strip().lower() != principal:
        return False
    conf = None
    for token in tokens:
        conf = await asyncio.to_thread(facts.find_confirmation, token)
        if conf is not None:
            break
    if conf is None:
        _audit(
            "A reply to a fact confirmation carried no known reference",
            {"message_id": message_id, "status": "unknown_token"},
        )
        return False
    # Only after a real token matched: a forged mail with a made-up reference
    # must not be able to make the Executive email the principal.
    if dmarc_failed(raw):
        _audit(
            "A reply to a fact confirmation failed DMARC",
            {"message_id": message_id, "confirmation_id": conf.id, "status": "refused_dmarc"},
        )
        await _reply(principal, (
            "A reply to one of my confirmation emails claimed to be from you but "
            "failed an authenticity check (DMARC), so I didn't act on it. If it was "
            "you, reply again from your own mail."
        ))
        return True
    if _auto_or_bulk_headers(raw):
        return False
    if conf.status != "pending":
        await _reply(principal, (
            "That confirmation isn't waiting any more: it was already used, "
            "cancelled, or it expired. Ask me again if you still want the change.\n\n"
            f"  {conf.summary}"
        ))
        return True

    _header, body, _att = _split_gmail_content(raw)
    decision = _decision(sender_new_text("\n".join(body)))
    if not decision:
        await _reply(principal, (
            "I couldn't tell whether to apply this, so it's still waiting. Reply "
            "CONFIRM to apply it or CANCEL to drop it.\n\n"
            f"  {conf.summary}"
        ))
        return True
    status: Literal["confirmed", "cancelled"] = "confirmed" if decision == "confirm" else "cancelled"
    if not await asyncio.to_thread(facts.decide_confirmation, conf.id, status):
        await _reply(principal, "That confirmation was already answered.")
        return True
    if decision == "cancel":
        _audit("The principal cancelled an emailed change",
               {"confirmation_id": conf.id, "status": "cancelled"})
        await _reply(principal, f"Cancelled. Nothing changed:\n\n  {conf.summary}")
        return True
    result = await asyncio.to_thread(apply_confirmed, conf.action)
    applied = _applied(result)
    _audit(
        "The principal confirmed an emailed change",
        {"confirmation_id": conf.id, "tool": conf.action.get("tool"),
         "status": "applied" if applied else "apply_failed"},
    )
    await _reply(principal, _outcome_line(conf.summary, result))
    return True


async def _reply(to: str, text: str) -> None:
    try:
        await _send(to, "Re: your change to what I keep as fact", text)
    except Exception:
        logger.warning("fact_confirmation: replying to the principal failed", exc_info=True)


__all__ = [
    "dmarc_failed",
    "request_confirmation",
    "try_email_fact_confirmation",
]
