"""What happens around a roster request (``people.roster_requests``).

An inbound adapter that meets a sender who is not on the roster — and is not
one of the principal's contacts — calls ``intake``. That:

1. holds the message on the sender's pending request (opening one if needed);
2. on a new request, puts a private card on the principal's /today and tells
   the principal on their preferred channel (a chat DM they can answer in
   place, or an email carrying a one-time token they can reply to);
3. acknowledges the sender with a fixed text — never the model, never an
   echo of what they wrote — at most once per sender per
   ``ROSTER_ACK_WINDOW_DAYS`` and ``ROSTER_ACK_DAILY_CAP`` a day overall. The
   adapter delivers it where only the sender sees it (a DM, an ephemeral
   Slack message, a private Telegram chat, an email to that one address).

When the principal answers (``answer``: the web card, their chat tool, their
email reply — ``try_email_roster_answer``), the held messages are replayed
through the adapter that received them (``register_replayer``), now that
the sender is on the roster, so the Executive answers them as if they had
been all along. Replays run in a fresh ``contextvars.Context``: the answer
may have come from the principal's own verified turn, and a stranger's
message must never be read with the principal's session (and contacts)
still bound.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from openexecutive.people import roster_requests as rr

logger = logging.getLogger(__name__)

# What an unknown sender is told. Fixed on purpose: no name, no company, no
# echo of their message — so a forged sender gains nothing by triggering it.
ACK_TEXT = (
    "Thanks — your message arrived. I don't recognise this address yet, so it's "
    "waiting for confirmation before I can reply."
)
ACK_SUBJECT = "Your message was received"

# How a chat adapter replays one held message. Returns True when the message
# was handed to the Executive.
Replayer = Callable[[rr.HeldMessage, rr.RosterRequest], Awaitable[bool]]
_REPLAYERS: dict[str, Replayer] = {}
_LOOP: asyncio.AbstractEventLoop | None = None
# Strong references to replay tasks, so they are not collected mid-run.
_TASKS: set[asyncio.Task[Any]] = set()


def register_replayer(channel: str, replayer: Replayer) -> None:
    """Let held messages from ``channel`` be replayed through ``replayer``.
    Called where each adapter starts; also records the running event loop so
    a sync route (a worker thread) can schedule a replay on it."""
    global _LOOP
    _REPLAYERS[channel] = replayer
    with contextlib.suppress(RuntimeError):
        _LOOP = asyncio.get_running_loop()


def bind_loop() -> None:
    """Record the running event loop as the one replays run on (the app's
    lifespan calls this at boot)."""
    global _LOOP
    with contextlib.suppress(RuntimeError):
        _LOOP = asyncio.get_running_loop()


def _audit(event: str, summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event

        log_event(event, summary, actor="roster", details=details, private=True)
    except Exception:
        logger.warning("roster_intake: audit row failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

def _suggest(channel: str, ref: str, display_name: str, profile_email: str | None) -> tuple[bool, int | None]:
    """(on a company domain?, the person it may be) for a new request."""
    from openexecutive.people.identity import is_company_address, resolve_email_sender

    address = ref if channel == "email" else (profile_email or "")
    on_company = bool(address) and is_company_address(address)
    suggested: int | None = None
    if channel != "email" and profile_email:
        # A chat account whose profile email is already someone's (Slack
        # reports it from the workspace, not from anything the sender typed).
        # Never from a display name: anyone can call themselves "Anna Chen".
        person = resolve_email_sender(profile_email, include_contacts=True)
        suggested = person.id if person is not None else None
    return on_company, suggested


async def intake(
    channel: str,
    channel_ref: str,
    *,
    external_id: str,
    payload: dict[str, Any],
    preview: str = "",
    display_name: str = "",
    profile_email: str | None = None,
    send_ack: Callable[[str], Awaitable[Any]] | None = None,
) -> rr.RosterRequest | None:
    """Hold a message from an unknown sender and acknowledge them (see the
    module docstring). None when nothing was held — no principal to ask, a
    recently declined sender, the daily cap. Never raises."""
    try:
        return await _intake(
            channel, channel_ref, external_id=external_id, payload=payload,
            preview=preview, display_name=display_name, profile_email=profile_email,
            send_ack=send_ack,
        )
    except Exception:
        logger.exception("roster_intake: intake failed on %s", channel)
        return None


async def _intake(
    channel: str,
    channel_ref: str,
    *,
    external_id: str,
    payload: dict[str, Any],
    preview: str,
    display_name: str,
    profile_email: str | None,
    send_ack: Callable[[str], Awaitable[Any]] | None,
) -> rr.RosterRequest | None:
    from openexecutive.people.store import find_principal_person

    principal = await asyncio.to_thread(find_principal_person)
    if principal is None:
        return None
    existing = await asyncio.to_thread(_pending_for, channel, channel_ref)
    on_company, suggested = (False, None)
    if existing is None:
        on_company, suggested = await asyncio.to_thread(
            _suggest, channel, channel_ref, display_name, profile_email
        )
    outcome = await asyncio.to_thread(
        rr.hold, channel, channel_ref,
        external_id=external_id, payload=payload, preview=preview,
        display_name=display_name, profile_email=profile_email,
        on_company_domain=on_company, suggested_person_id=suggested,
    )
    if outcome is None:
        return None
    request = outcome.request
    if outcome.created:
        await asyncio.to_thread(rr.surface_card, request, principal.id)
        _audit(
            "roster_request_created",
            f"Roster request {request.id} opened ({channel})",
            {"request_id": request.id, "channel": channel, "channel_ref": request.channel_ref},
        )
        await notify_principal(request)
    if send_ack is not None and await asyncio.to_thread(
        rr.claim_ack, channel, request.channel_ref, request_id=request.id
    ):
        try:
            await send_ack(ACK_TEXT)
            _audit(
                "roster_ack_sent",
                f"Told a new {rr.channel_label(channel)} sender their message is waiting",
                {"request_id": request.id, "channel": channel},
            )
        except Exception:
            logger.warning("roster_intake: acknowledgement on %s failed", channel, exc_info=True)
            await asyncio.to_thread(rr.release_ack, channel, request.channel_ref)
    return request


def _pending_for(channel: str, ref: str) -> rr.RosterRequest | None:
    norm = ref.strip().lower() if channel == "email" else ref.strip()
    for request in rr.list_requests("pending", limit=200):
        if request.channel == channel and request.channel_ref == norm:
            return request
    return None


# --------------------------------------------------------------------------- #
# Telling the principal
# --------------------------------------------------------------------------- #

def _chat_prompt(request: rr.RosterRequest) -> str:
    who = f"“{request.display_name}”" if request.display_name else "them"
    return (
        f"{rr.describe(request)}. I told them their message arrived and is waiting "
        "for you — I haven't replied to what they said.\n"
        f"Tell me who {who} is and I'll add them — for example \"that's Annamarie, "
        "add her to the team\", \"add them as a contact\", \"that's <someone already "
        "on the People list>\", or \"ignore\". The card on your Today page works too."
    )


def _email_prompt(request: rr.RosterRequest, token: str) -> tuple[str, str]:
    subject_who = request.display_name or request.channel_ref
    subject = f"Who is {subject_who}? [{token}]"
    body = (
        f"{rr.describe(request)}.\n\n"
        "I told them their message arrived and is waiting for you. I haven't "
        "replied to what they said.\n\n"
        "Reply to this email from your own address to tell me who they are, for "
        "example:\n"
        "  - \"That's Annamarie Chen, add her to the team\"\n"
        "  - \"Add them as a contact\"\n"
        "  - \"That's <someone already on the People list>\"\n"
        "  - \"Ignore\"\n\n"
        "Or use the card on your Today page.\n\n"
        f"(Reference {token} — keep it in your reply. It works once.)"
    )
    return subject, body


def _message_id(result: str) -> str | None:
    match = re.search(r"Message ID:\s*([\w-]+)", result or "")
    return match.group(1) if match else None


async def notify_principal(request: rr.RosterRequest) -> str | None:
    """Tell the principal about a new request on the first channel that works
    (their preferred chat, then the others, then email). Chat senders and
    senders on the company's own domain are pushed; an outside email sender
    waits on the /today card, as does everyone past the day's cap. Returns
    the channel used, or None. Never raises."""
    try:
        return await _notify_principal(request)
    except Exception:
        logger.exception("roster_intake: telling the principal about request %d failed", request.id)
        return None


async def _notify_principal(request: rr.RosterRequest) -> str | None:
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.scheduler.runner import _delivered_ok, principal_delivery_plan

    if request.channel == "email" and not request.on_company_domain:
        return None
    if await asyncio.to_thread(rr.notified_today) >= get_settings().roster_request_daily_cap:
        return None
    principal, plan = await asyncio.to_thread(principal_delivery_plan)
    if principal is None:
        return None
    text = _chat_prompt(request)
    # No session: nothing sent here is a turn's outbound context.
    with set_session(None):
        for channel in plan:
            try:
                if channel == "slack_dm" and principal.slack_user_id:
                    from openexecutive.orchestrator.schedule_tools import handle_send_slack_dm

                    result = await handle_send_slack_dm(
                        {"user_id": principal.slack_user_id, "text": text}
                    )
                elif channel == "discord_dm" and principal.discord_user_id:
                    from openexecutive.orchestrator.schedule_tools import handle_send_discord_dm

                    result = await handle_send_discord_dm(
                        {"discord_user_id": principal.discord_user_id, "text": text}
                    )
                elif channel == "telegram" and principal.telegram_chat_id:
                    from openexecutive.orchestrator.schedule_tools import (
                        handle_send_telegram_message,
                    )

                    result = await handle_send_telegram_message(
                        {"chat_id": int(principal.telegram_chat_id), "text": text}
                    )
                elif channel == "email" and principal.email:
                    sent = await _email_principal(request, principal.email)
                    if sent:
                        return "email"
                    continue
                else:
                    continue
                if _delivered_ok(result):
                    await asyncio.to_thread(rr.mark_notified, request.id, channel)
                    return channel
            except Exception:
                logger.warning("roster_intake: notifying via %s failed", channel, exc_info=True)
    return None


async def _email_principal(request: rr.RosterRequest, principal_email: str) -> bool:
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway
    from openexecutive.workflows.action_step import looks_like_error

    gateway = get_active_gateway()
    if gateway is None:
        return False
    token = await asyncio.to_thread(rr.issue_email_token, request.id)
    subject, body = _email_prompt(request, token)
    result = await gateway.call_tool({
        "name": "google_workspace__send_gmail_message",
        "arguments": {
            "user_google_email": get_settings().exec_email_address,
            "to": principal_email,
            "subject": subject,
            "body": body,
        },
    })
    if looks_like_error(result):
        logger.warning("roster_intake: confirmation email failed: %s", str(result)[:200])
        return False
    await asyncio.to_thread(rr.mark_notified, request.id, "email", _message_id(result))
    return True


# --------------------------------------------------------------------------- #
# Acknowledging an email sender
# --------------------------------------------------------------------------- #

async def send_email_ack(gateway: Any, to: str) -> None:
    """Send ``ACK_TEXT`` to exactly ``to`` — the one send the gateway lets
    reach an address off the roster, under a one-shot grant that admits only
    this subject, this body and this recipient (``mcp_gateway.roster_ack_grant``).
    Raises when the send fails, so the caller can release its claim."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.mcp_gateway import roster_ack_grant
    from openexecutive.orchestrator.schedule_tools import set_session
    from openexecutive.workflows.action_step import looks_like_error

    with set_session(None), roster_ack_grant(to=to, subject=ACK_SUBJECT, body=ACK_TEXT):
        result = await gateway.call_tool({
            "name": "google_workspace__send_gmail_message",
            "arguments": {
                "user_google_email": get_settings().exec_email_address,
                "to": to,
                "subject": ACK_SUBJECT,
                "body": ACK_TEXT,
            },
        })
    if looks_like_error(result):
        raise RuntimeError("acknowledgement email refused or failed")


# Mail no human is waiting on an answer to. The header checks apply only to
# headers the Gmail tool prints; the address checks always do.
_AUTOMATED_LOCAL = re.compile(
    r"^(?:no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?|notifications?|"
    r"notify|alerts?|newsletters?|news|marketing|updates|digest|info|support|billing|"
    r"receipts?|invoices?|calendar-notification)(?:[+._-].*)?$"
)
_BULK_HEADERS = ("list-unsubscribe:", "list-id:", "feedback-id:", "x-campaign")


def looks_automated(raw: str, from_addr: str) -> bool:
    """Whether an email looks machine-sent (a notification, a newsletter, a
    bounce), so no request is opened and no acknowledgement goes out."""
    local = from_addr.strip().lower().rpartition("@")[0]
    if not local or _AUTOMATED_LOCAL.match(local):
        return True
    return _auto_or_bulk_headers(raw)


def _auto_or_bulk_headers(raw: str) -> bool:
    """Whether the headers the Gmail tool printed mark the mail as automatic
    (an out-of-office, a list, a bulk send) or as failing DMARC."""
    from openexecutive.integrations.email_poller import _split_gmail_content

    header, _body, _att = _split_gmail_content(raw)
    for line in header:
        low = line.strip().lower()
        if low.startswith(_BULK_HEADERS):
            return True
        if low.startswith("auto-submitted:") and not low.endswith(" no"):
            return True
        if low.startswith("precedence:") and any(w in low for w in ("bulk", "list", "junk")):
            return True
        if low.startswith("authentication-results:") and "dmarc=fail" in low:
            return True
    return False


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #

def _match_person_by_name(name: str) -> tuple[Any, bool]:
    """(the one person called ``name`` — full name, or first name when only
    one person has it — or None, whether several matched)."""
    from openexecutive.people.store import list_people

    wanted = " ".join(name.split()).lower()
    if not wanted:
        return None, False
    people = list_people(include_contacts=True)
    full = [p for p in people if " ".join(p.full_name.split()).lower() == wanted]
    if len(full) == 1:
        return full[0], False
    if len(full) > 1:
        return None, True
    first = [p for p in people if (p.full_name.split() or [""])[0].lower() == wanted]
    if len(first) == 1:
        return first[0], False
    return None, len(first) > 1


def default_kind(request: rr.RosterRequest) -> str:
    """The kind a new person gets when whoever answered did not say: team for
    someone on the company's own domain, else contact (the least access)."""
    return request.suggested_kind or "contact"


async def answer(
    request_id: int,
    decision: str,
    *,
    via: str,
    full_name: str | None = None,
    kind: str | None = None,
    role: str = "",
    link_person_id: int | None = None,
    replace_channel_id: bool = False,
) -> rr.RosterRequest:
    """``roster_requests.resolve`` plus what follows it: replay the held
    messages (or drop them). Raises what ``resolve`` raises."""
    done = await asyncio.to_thread(
        rr.resolve, request_id, decision, via=via, full_name=full_name, kind=kind,
        role=role, link_person_id=link_person_id, replace_channel_id=replace_channel_id,
    )
    schedule_replay(done)
    return done


def after_roster_write() -> None:
    """Call after the roster changed outside a request (the People page, the
    ``upsert_person`` tool): close the requests whose sender now matches
    someone, and replay their messages. Never raises."""
    try:
        for done in rr.reconcile_pending():
            schedule_replay(done)
    except Exception:
        logger.exception("roster_intake: reconciling pending requests failed")


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def _spawn(make: Callable[[], Coroutine[Any, Any, None]]) -> None:
    # A fresh, empty context: nothing of the caller's turn (its session, its
    # audit scope) reaches the stranger's replayed message.
    task = asyncio.get_running_loop().create_task(make(), context=contextvars.Context())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def schedule_replay(request: rr.RosterRequest) -> None:
    """Replay (or drop) a resolved request's held messages in the background,
    from any thread. Never raises."""
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if _LOOP is None or _LOOP.is_closed():
                # No loop to run on (a CLI or a test): do it now, still in a
                # fresh context.
                contextvars.Context().run(asyncio.run, replay_request(request))
                return
            _LOOP.call_soon_threadsafe(
                _spawn, lambda: replay_request(request), context=contextvars.Context()
            )
            return
        _spawn(lambda: replay_request(request))
    except Exception:
        logger.exception("roster_intake: scheduling replay for request %d failed", request.id)


async def replay_request(request: rr.RosterRequest) -> None:
    """Hand each held message to its adapter's replayer, oldest first.

    Team: every message. Contact: email only (it takes the private contact
    path; a contact has no chat access, so held chat messages are dropped).
    Declined: all dropped. No replayer for the channel: ``unavailable``."""
    messages = await asyncio.to_thread(rr.claim_messages, request.id)
    if not messages:
        return
    replay = request.status in ("approved", "linked", "superseded") and (
        request.resolved_kind == "team" or request.channel == "email"
    )
    replayer = _REPLAYERS.get(request.channel)
    for message in messages:
        if not replay:
            status = "dropped"
        elif replayer is None:
            status = "unavailable"
        else:
            try:
                status = "replayed" if await replayer(message, request) else "failed"
            except Exception:
                logger.exception("roster_intake: replaying message %d failed", message.id)
                status = "failed"
        await asyncio.to_thread(rr.finish_message, message.id, status)
        _audit(
            "roster_message_replayed",
            f"Held message from roster request {request.id}: {status}",
            {"request_id": request.id, "message_id": message.id, "status": status},
        )


# --------------------------------------------------------------------------- #
# The principal's email answer
# --------------------------------------------------------------------------- #

_PARSE_QUESTION = (
    "The Executive asked the owner who a new sender is, and whether to add them "
    "to the People list: as a teammate, as a contact, as someone already on the "
    "list writing from a new address, or not at all."
)


async def _parse_answer(text: str) -> dict[str, Any]:
    from openexecutive.workflows.wait_for_human import parse_decision

    return await parse_decision(text, "roster_identity", question=_PARSE_QUESTION)


def _principal_address() -> str:
    """The principal's primary address — the one confirmation emails go to."""
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return (principal.email or "").strip().lower() if principal is not None else ""


async def try_email_roster_answer(
    gateway: Any, raw: str, from_addr: str, message_id: str
) -> bool:
    """Handle an email that answers a roster request; True when it did (the
    caller marks it read and stops). An answer must come from the principal's
    primary address — exactly: not an alias, not by the company-domain rule —
    and carry the one-time token of a pending request, which only ever went
    to that mailbox (and which the gateway hides from every other read of the
    Executive's mail, ``mcp_gateway.reveal_roster_tokens``): a From header
    alone proves nothing. An automatic reply (an out-of-office quoting the
    subject) is no answer. Anything else is left to the ordinary path."""
    tokens = rr.find_tokens(raw)
    if not tokens:
        return False
    principal = await asyncio.to_thread(_principal_address)
    if not principal or from_addr.strip().lower() != principal:
        return False
    if _auto_or_bulk_headers(raw):
        return False
    request = None
    for token in tokens:
        request = await asyncio.to_thread(rr.find_pending_by_token, token)
        if request is not None:
            break
    if request is None:
        _audit(
            "roster_email_answer_refused",
            "An email reply to a roster request carried no live reference",
            {"message_id": message_id},
        )
        return False
    from openexecutive.integrations.email_poller import _split_gmail_content, sender_new_text

    header, body, _att = _split_gmail_content(raw)
    for line in header:
        if line.strip().lower().startswith("authentication-results:") and "dmarc=fail" in line.lower():
            _audit(
                "roster_email_answer_refused",
                "An email reply to a roster request failed DMARC",
                {"message_id": message_id, "request_id": request.id},
            )
            return False
    text = sender_new_text("\n".join(body))
    reply = await _apply_answer(request, text, via="email")
    await _reply_to_principal(gateway, from_addr, request, reply)
    return True


async def _apply_answer(request: rr.RosterRequest, text: str, *, via: str) -> str:
    """Parse the principal's words and answer the request; returns what to
    tell them."""
    from openexecutive.workflows.wait_for_human import PARSE_FAILED_KEY

    parsed = await _parse_answer(text) if text.strip() else {"decision": "unrelated"}
    decision = str(parsed.get("decision") or "").lower()
    if parsed.get(PARSE_FAILED_KEY) or decision not in ("approve", "link", "decline"):
        return (
            "I couldn't tell what you'd like me to do, so they're still waiting. "
            "Reply with \"add them to the team\", \"add them as a contact\", "
            "\"that's <name on your People list>\" or \"ignore\"."
        )
    name = " ".join(str(parsed.get("name") or "").split())[:200]
    kind_raw = str(parsed.get("kind") or "").lower()
    kind = kind_raw if kind_raw in ("team", "contact") else None
    try:
        if decision == "decline":
            await answer(request.id, "decline", via=via)
            return "Done — I'll leave them off the People list."
        person, ambiguous = await asyncio.to_thread(_match_person_by_name, name)
        if ambiguous:
            return (
                f"More than one person on your People list is called {name}, so "
                "they're still waiting. Use the full name, or the card on your Today page."
            )
        if person is not None and person.id is not None:
            await answer(request.id, "link", via=via, link_person_id=person.id)
            return f"Done — {rr.channel_label(request.channel)} {request.channel_ref} is now {person.full_name}'s."
        if decision == "link":
            return (
                f"I couldn't find {name or 'that person'} on your People list, so "
                "they're still waiting. Use their full name, or the card on your Today page."
            )
        full_name = name or request.display_name
        if not full_name:
            return "What's their name? Reply with it and I'll add them."
        chosen = kind or default_kind(request)
        await answer(request.id, "approve", via=via, full_name=full_name, kind=chosen)
        role = "the team" if chosen == "team" else "your contacts"
        return f"Done — I added {full_name} to {role}, and I'll pick up their message."
    except rr.RequestNotPending:
        return "That one was already answered."
    except (rr.ChannelIdConflict, ValueError) as exc:
        return f"I couldn't do that: {exc}. They're still waiting."


async def _reply_to_principal(gateway: Any, to: str, request: rr.RosterRequest, text: str) -> None:
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.schedule_tools import set_session

    try:
        with set_session(None):
            await gateway.call_tool({
                "name": "google_workspace__send_gmail_message",
                "arguments": {
                    "user_google_email": get_settings().exec_email_address,
                    "to": to,
                    "subject": f"Re: roster request {request.id}",
                    "body": text,
                },
            })
    except Exception:
        logger.warning("roster_intake: replying to the principal failed", exc_info=True)


__all__ = [
    "ACK_SUBJECT",
    "ACK_TEXT",
    "after_roster_write",
    "answer",
    "default_kind",
    "intake",
    "looks_automated",
    "notify_principal",
    "register_replayer",
    "replay_request",
    "schedule_replay",
    "send_email_ack",
    "try_email_roster_answer",
]
