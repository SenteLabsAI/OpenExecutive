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
   principal's primary address, carrying a pending token, and passing DMARC
   as Gmail recorded it (``authenticated_by_gmail``); the change is then
   applied (``fact_tools.apply_confirmed``) and the principal is told what
   happened.

The request itself must pass the same check (``Session.email_authenticated``),
so mail that only claims the principal's address cannot fill their inbox with
confirmation requests.

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
from email.message import Message
from email.parser import HeaderParser
from email.utils import parseaddr
from typing import Any, Literal

from openexecutive.memory import facts

logger = logging.getLogger(__name__)

_CONFIRM_WORDS = re.compile(r"\b(confirm|confirmed|yes|approve|approved)\b", re.IGNORECASE)
_CANCEL_WORDS = re.compile(r"\b(cancel|cancelled|no|reject|don['’]?t|do not)\b", re.IGNORECASE)
# Anything that holds back a confirmation: a negation ("Not approved", "I
# haven't confirmed") or a pause ("Confirm — actually wait").
_HOLD_WORDS = re.compile(
    r"\b(not|never|unable|cannot|wait|hold|stop)\b|n['’]t\b", re.IGNORECASE,
)
# Where a signature starts: "-- ", a rule, or a phone client's footer line.
# Everything from there on is read past ("Sent from my iPhone. Please don't
# forward" must not turn a CONFIRM into unclear).
_SIGNATURE_START = re.compile(
    r"^[ \t]*(--[ \t]*|_{3,}|—{2,}|sent from my\b.*|get outlook for\b.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# Harmless "no"s, removed before reading the reply: "Confirm, no rush".
_SOFT_NO = re.compile(
    r"\bno (rush|hurry|problems?|worries|worry|changes?|need|issues?|further)\b", re.IGNORECASE,
)


def principal_address() -> str:
    """The principal's primary address — where confirmations go, and the only
    address a confirming reply may come from."""
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return (principal.email or "").strip().lower() if principal is not None else ""


# Where get_gmail_message_content(body_format="raw") starts the RFC 5322 text:
# a line of its own after a blank line. Everything above it is header values
# the sender wrote (Subject, To, References…), so the separator is matched
# whole: a bare "--- RAW MIME ---" in a Subject must not move where the raw
# message is read from.
_RAW_MIME_SEPARATOR = "\n\n--- RAW MIME ---\n"
# The authserv-id Gmail stamps on the Authentication-Results of mail it receives.
_GMAIL_AUTHSERV = "mx.google.com"
_HEADER_FROM = re.compile(r"\bheader\.from=([^\s;()]+)")
# Marks of an automatic reply (an out-of-office, a vacation responder) in the
# raw headers. The printed headers the poller reads carry only Precedence and
# the List-* ones, so an Exchange out-of-office — Auto-Submitted only — would
# otherwise pass as the principal's answer.
_AUTO_HEADERS = ("x-auto-response-suppress", "x-autoreply", "x-autorespond", "x-autoresponder")
_AUTO_SUBJECT = re.compile(
    r"^\s*(auto:|(automatic reply|auto(matic)?[- ]?(reply|response)|out of (the )?office|"
    r"autoreply|vacation|away)\b)",
    re.IGNORECASE,
)


def _raw_headers(raw: str) -> Message | None:
    """The headers of the raw message in a ``body_format="raw"`` read, or
    None when there is none or it can't be parsed."""
    _before, separator, mime = raw.partition(_RAW_MIME_SEPARATOR)
    if not separator:
        return None
    try:
        return HeaderParser().parsestr(mime.lstrip("\r\n"), headersonly=True)
    except Exception:  # noqa: BLE001 - unreadable counts as absent.
        return None


_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"\((?:[^()\\]|\\.)*\)")


def _strip_quotes_and_comments(value: str) -> str | None:
    """``value`` with RFC 8601 quoted strings and (nested) comments removed,
    or None when they don't balance. Gmail writes sender-chosen text into its
    own Authentication-Results — the envelope sender in ``smtp.mailfrom=`` and
    the SPF comment, DKIM tags — and a quoted local part such as
    ``"x;dmarc=pass header.from=victim.com "@attacker.com`` would otherwise
    read as a verdict of its own."""
    value = _QUOTED.sub('""', value)
    while True:
        stripped = _COMMENT.sub(" ", value)
        if stripped == value:
            break
        value = stripped
    if '"' in value.replace('""', "") or "(" in value or ")" in value:
        return None
    return value


def authenticated_by_gmail(raw: str, from_addr: str) -> bool:
    """Whether a raw message (``get_gmail_message_content`` with
    ``body_format="raw"``) shows Gmail found ``from_addr``'s domain
    authenticated. Only the topmost Authentication-Results header counts:
    Gmail adds it on receipt, above every header the sender wrote, so a forged
    one sits below it. It must be Gmail's (``mx.google.com``) and report
    ``dmarc=pass`` for the From domain, and the raw From must be
    ``from_addr``. Anything missing or unreadable — no header, ``dmarc=none``,
    a temporary error — is False: this gate fails closed."""
    address = from_addr.strip().lower()
    headers = _raw_headers(raw)
    if headers is None or "@" not in address:
        return False
    try:
        _name, raw_from = parseaddr(str(headers.get("From", "")))
        results = headers.get_all("Authentication-Results") or []
    except Exception:  # noqa: BLE001 - an unreadable message is not authenticated.
        return False
    if raw_from.strip().lower() != address or not results:
        return False
    newest = _strip_quotes_and_comments(" ".join(str(results[0]).split()).lower())
    if newest is None:
        return False
    authserv, _sep, rest = newest.partition(";")
    if authserv.strip() != _GMAIL_AUTHSERV:
        return False
    verdicts = [c.strip() for c in rest.split(";") if c.strip().startswith("dmarc=")]
    if len(verdicts) != 1:
        return False
    [dmarc] = verdicts
    header_from = _HEADER_FROM.search(dmarc)
    return (
        re.match(r"dmarc=pass\b", dmarc) is not None
        and header_from is not None
        and header_from.group(1) == address.rsplit("@", 1)[1]
    )


def automatic_reply(raw: str) -> bool:
    """Whether a raw message is an automatic reply: ``Auto-Submitted`` other
    than ``no``, an out-of-office header, a bulk / auto-reply ``Precedence``,
    or an out-of-office subject. An unreadable message counts as automatic:
    nobody can be shown to have written it."""
    headers = _raw_headers(raw)
    if headers is None:
        return True
    try:
        names = {str(k).lower() for k in headers}
        auto = str(headers.get("Auto-Submitted", "no")).strip().lower()
        precedence = str(headers.get("Precedence", "")).strip().lower()
        subject = " ".join(str(headers.get("Subject", "")).split())
    except Exception:  # noqa: BLE001 - unreadable counts as automatic.
        return True
    return (
        (auto not in ("", "no"))
        or any(h in names for h in _AUTO_HEADERS)
        or precedence in ("bulk", "auto_reply", "list", "junk")
        or _AUTO_SUBJECT.match(subject) is not None
    )


async def read_raw(gateway: Any, message_id: str) -> str:
    """Message ``message_id`` read as raw MIME (``body_format="raw"``): the
    text the poller reads doesn't carry Authentication-Results or
    Auto-Submitted — workspace-mcp prints a fixed set of headers. "" on any
    failure. Never raises."""
    from openexecutive.config import get_settings

    if gateway is None or not message_id:
        return ""
    try:
        raw = await gateway.call_tool({
            "name": "google_workspace__get_gmail_message_content",
            "arguments": {
                "message_id": message_id,
                "user_google_email": get_settings().exec_email_address,
                "body_format": "raw",
            },
        })
    except Exception:  # noqa: BLE001 - read as nothing, which fails closed.
        logger.warning("fact_confirmation: reading message %s raw failed", message_id, exc_info=True)
        return ""
    return str(raw or "")


async def sender_authenticated(gateway: Any, message_id: str, from_addr: str) -> bool:
    """Whether Gmail authenticated the sender of message ``message_id``
    (``authenticated_by_gmail`` on ``read_raw``). False on any failure."""
    return authenticated_by_gmail(await read_raw(gateway, message_id), from_addr)


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
        to = await asyncio.to_thread(principal_address)
        if not to:
            return "there is no principal email address on the People list to confirm this with"
        held = await asyncio.to_thread(facts.hold_confirmation, action, summary)
        if held is None:
            return (
                f"{facts.MAX_PENDING_CONFIRMATIONS} emailed changes are already waiting for "
                "the principal to confirm them. Ask them to answer those first, or make "
                "this change in the web app."
            )
        conf_id, token = held
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
    """"confirm", "cancel" or "" (unclear) from the principal's reply. It
    confirms only when it holds a confirm word and nothing that cancels,
    negates or pauses ("Not approved", "Confirm? No.", "Yes, but wait" are
    all unclear, and they are asked again). A reply that opens with a cancel
    word, or holds only cancel words, cancels. A few harmless phrases ("no
    rush", "no changes") are read past. Every doubt falls on the side of not
    applying. A signature ("-- ", "Sent from my iPhone") is read past."""
    signature = _SIGNATURE_START.search(text)
    text = text[: signature.start()] if signature else text
    words = _SOFT_NO.sub(" ", text[:400])
    opening = words.lstrip(" \t\r\n>*_-\"',.;:!")[:20]
    if _CANCEL_WORDS.match(opening):
        return "cancel"
    yes, no = bool(_CONFIRM_WORDS.search(words)), bool(_CANCEL_WORDS.search(words))
    if yes and not no and not _HOLD_WORDS.search(words):
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
    primary address, exactly, and carry the token of a pending confirmation;
    anything else is left to the ordinary path, where the token is hidden
    from the model. A matched reply that Gmail did not authenticate is
    refused (the principal is told), and an automatic reply is consumed
    quietly; only an authenticated reply a person wrote can decide."""
    from openexecutive.integrations.email_poller import _split_gmail_content, sender_new_text
    from openexecutive.integrations.roster_intake import _auto_or_bulk_headers
    from openexecutive.orchestrator.fact_tools import apply_confirmed

    tokens = facts.find_confirmation_tokens(raw)
    if not tokens:
        return False
    principal = await asyncio.to_thread(principal_address)
    if not principal or from_addr.strip().lower() != principal:
        return False
    conf = None
    for token in tokens:
        conf = await asyncio.to_thread(facts.find_confirmation, token)
        if conf is not None:
            break
    if conf is None:
        # A spent, expired or made-up token: not an answer. The mail goes on
        # to the ordinary path with the token hidden, and gets no reply here,
        # so an old token cannot make the Executive email the principal.
        _audit(
            "A reply to a fact confirmation carried no live reference",
            {"message_id": message_id, "status": "no_live_token"},
        )
        return False
    # Only after a live token matched: a forged mail without one never makes
    # the Executive email the principal, or fetch anything.
    printed_auto = _auto_or_bulk_headers(raw)
    raw_mime = "" if printed_auto else await read_raw(gateway, message_id)
    if not printed_auto and not authenticated_by_gmail(raw_mime, from_addr):
        _audit(
            "A reply to a fact confirmation was not authenticated",
            {"message_id": message_id, "confirmation_id": conf.id, "status": "refused_unauthenticated"},
        )
        await _reply(principal, (
            "A reply to one of my confirmation emails claimed to be from you, but "
            "I couldn't confirm it came from your mail server (Gmail's DMARC check "
            "didn't pass, or I couldn't read it), so I didn't act on it. The change is still waiting; if it was "
            "you, reply again from your own mailbox.\n\n"
            f"  {conf.summary}"
        ))
        return True
    if printed_auto or automatic_reply(raw_mime):
        # An out-of-office or vacation reply echoes the token and may say
        # "confirm" ("I'll confirm on my return"); nobody answered. Consumed
        # quietly: no reply (it would bounce between two responders), no
        # model turn, and the change keeps waiting.
        _audit(
            "An automatic reply to a fact confirmation was ignored",
            {"message_id": message_id, "confirmation_id": conf.id, "status": "auto_reply_ignored"},
        )
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
    try:
        result = await asyncio.to_thread(apply_confirmed, conf.action)
    except Exception:
        # The token is already spent: say so rather than leave the principal
        # thinking it is still waiting, or done.
        logger.exception("fact_confirmation: applying confirmation %s failed", conf.id)
        result = json.dumps({"error": "something went wrong applying it; ask me again or make the change in the web app"})
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
    "authenticated_by_gmail",
    "automatic_reply",
    "principal_address",
    "read_raw",
    "request_confirmation",
    "sender_authenticated",
    "try_email_fact_confirmation",
]
