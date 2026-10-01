"""Send a reply the inbox watcher drafted, when the person taps Send.

The one path in Act as me that sends anything, and all it can send is the
exact Gmail draft on a ``delegation_reply`` card, by its id
(``DelegateGmail.send_draft`` posts ``{"id": ...}`` to ``drafts.send``),
after the person approved that card (``POST /decisions/{id}/approve``, a
class only the card's own person sees or resolves). Only that route calls ``send_approved_reply`` (a unit
test walks the code), so no model, chat turn, workflow, scheduled job or MCP
call can reach it. Sending is the person's own act: it works while the
Executive is paused.

At send time it checks again, in this order:

1. The caller is the person the card belongs to, and the API knows it:
   signed callers are on (``CALLER_ASSERTION_PUBLIC_KEYS``) and the caller is
   that signed-in person (or the local operator under local login), or the
   API runs for local login, answering this computer only. Anything else is a
   409 ``caller_signing_required``: with only a shared secret, whoever holds
   it could send as the owner.
2. Act as me and Draft replies to my inbox are both on, and no client slot is
   active.
3. Their Gmail is connected (``gmail_status``).
4. The draft still exists, in the same thread, and they haven't replied
   there themselves since (either way the card closes).
5. Its recipients: someone, at most 10, never the Executive's own address,
   and it is from one of their own addresses.
6. What changed since the card was made needs a second yes: recipients that
   differ from the card's, or a newer message in the thread. The first tap
   gets a 409 ``confirm`` naming the recipients; the second tap sends them
   back (``{"recipients": [...], "thread_moved_on": true}``).

Then it claims the card (``claim_for_execution``, proposed → executing, a
compare-and-set: a double tap sends once), reads the draft once more and sends
only if it is still the version checked above (``drafts.send`` sends whatever
the draft is when Gmail gets the request, so an edit made meanwhile in another
tab is refused, not sent unchecked), and records it (``finish_execution``:
``approved_with_edit`` when the draft was edited in Gmail). A failure that
shows nothing was sent hands the card back. One that leaves it unclear (a
timeout, a 5xx) leaves it ``executing`` for the reconciler
(``inbox._settle_unconfirmed_send``), which asks Gmail what happened. Nothing
is ever retried on its own. Every row it writes is private, and the card's
person's own (``rows_for_person``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class SendRefused(Exception):
    """Why a reply was not sent: an HTTP status, a code, a message for the
    person, and for a confirm the reasons and the recipients."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


_NOT_YOURS = "Only the person this reply was written for can send it."
_CONFIRM_TEXT = {
    "recipients_changed": "The draft's recipients changed since it was written.",
    "thread_moved_on": "A newer message arrived in this conversation after the draft was written.",
}


def _check_caller(caller: Any, email: str) -> None:
    """Raise unless the API knows the caller is the person at ``email``."""
    from openexecutive.api.caller import signing_on
    from openexecutive.utils.deployment import is_local_login

    if signing_on():
        if caller.kind == "user" and caller.email == email:
            return
        if caller.kind == "operator" and is_local_login():
            return
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    if is_local_login():
        # The API answers this computer only (api.main), and the web app
        # sends no caller: the person at the keyboard is the owner.
        if caller.kind == "open" and caller.email in ("", email):
            return
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    raise SendRefused(
        409,
        "caller_signing_required",
        "Sending from here needs signed sign-ins, so that nobody else can send as you. "
        "Until they're set up (Settings → Setup status → API protection), send it from Gmail.",
    )


async def send_approved_reply(
    instance: Any,
    *,
    caller: Any,
    resolver: int | None,
    confirm: dict[str, Any] | None = None,
    gmail: Any = None,
    now: datetime | None = None,
) -> str:
    """Send the draft on ``instance``, a ``delegation_reply`` card the caller
    approved. Returns the sent message's id; raises ``SendRefused``."""
    from openexecutive.audit import rows_for_person

    owner = getattr(instance, "approver_person_id", None)
    with rows_for_person(owner if isinstance(owner, int) else None):
        return await _send(
            instance, caller=caller, resolver=resolver, confirm=confirm or {},
            gmail=gmail, now=now or datetime.now(UTC),
        )


def _addresses(value: Any) -> list[str] | None:
    from openexecutive.delegation.gmail import normalize_email

    if not isinstance(value, list):
        return None
    return sorted({normalize_email(a) for a in value if isinstance(a, str) and a.strip()})


async def _send(
    instance: Any, *, caller: Any, resolver: int | None, confirm: dict[str, Any], gmail: Any, now: datetime
) -> str:
    from openexecutive.config import get_settings
    from openexecutive.delegation import drafts
    from openexecutive.delegation.gmail import (
        BLOCKING_CODES,
        STATUS_MESSAGES,
        GmailAuthError,
        GmailError,
        GmailNotFound,
        GmailRateLimited,
        gmail_for,
        gmail_status,
        normalize_email,
    )
    from openexecutive.delegation.inbox import (
        CLOSED,
        SENDING,
        SENT,
        _add_flag,
        _audit,
        _client_slot_active,
        _close_card,
        _set_outcome,
        card_payload,
        get_watch,
        later_messages,
    )
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.memory.decision_ledger import (
        STATUS_APPROVED_UNCHANGED,
        STATUS_APPROVED_WITH_EDIT,
        claim_for_execution,
        finish_execution,
        release_claim,
    )
    from openexecutive.people.store import get_person

    payload = card_payload(instance)
    person = get_person(int(payload.get("person_id") or 0))
    if (
        person is None or person.id is None or not person.email or not can_delegate(person)
        or resolver != person.id or instance.approver_person_id != person.id
    ):
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    email = normalize_email(person.email)
    _check_caller(caller, email)
    if not is_enabled(person.id) or not get_watch(person.id).enabled:
        raise SendRefused(
            409, "inbox_off",
            "Turn on Act as me and Draft replies to my inbox to send from here, or send it from Gmail.",
        )
    if _client_slot_active():
        raise SendRefused(409, "client_slot", "Sending from here is paused while a client is active. Send it from Gmail.")
    client = gmail if gmail is not None else gmail_for(email)
    status = await gmail_status(email, gmail=client)
    if status != "connected":
        raise SendRefused(409, BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])

    message_id = str(payload.get("message_id") or "")
    thread_id = str(payload.get("thread_id") or "")
    unreadable = "Couldn't read the draft in your Gmail. Try again in a moment."
    try:
        draft = await client.get_draft(str(payload.get("draft_id") or ""))
        own = {email, *await client.send_as_addresses()}
    except GmailError as exc:
        raise SendRefused(502, "gmail_error", unreadable) from exc
    if draft is None:
        _close_card(person.id, instance.id, message_id, "draft_gone", CLOSED)
        raise SendRefused(409, "draft_gone", "That draft isn't in your Gmail any more: it was sent or deleted there.")
    try:
        thread = await client.get_thread(thread_id)
    except GmailNotFound:
        _close_card(person.id, instance.id, message_id, "thread_gone", CLOSED)
        raise SendRefused(409, "draft_gone", "That conversation isn't in your Gmail any more.") from None
    except GmailError as exc:
        raise SendRefused(502, "gmail_error", unreadable) from exc
    if draft.message.thread_id != thread_id:
        raise SendRefused(409, "draft_moved", "That draft isn't in the same conversation any more. Send it from Gmail.")
    later = later_messages(thread, payload, now=now)
    if any("SENT" in m.labels and m.from_addr in own for m in later):
        _close_card(person.id, instance.id, message_id, "you_replied", CLOSED)
        raise SendRefused(409, "you_replied", "You've already replied in this conversation, so this draft wasn't sent.")

    recipients = sorted({*draft.message.to, *draft.message.cc, *draft.message.bcc})
    exec_address = normalize_email(get_settings().exec_email_address)
    if not recipients:
        raise SendRefused(409, "no_recipients", "The draft isn't addressed to anyone. Add someone in Gmail.")
    if len(recipients) > MAX_RECIPIENTS:
        raise SendRefused(
            409, "too_many_recipients", f"The draft goes to more than {MAX_RECIPIENTS} people. Send it from Gmail.",
        )
    if exec_address and exec_address in recipients:
        raise SendRefused(
            409, "executive_recipient", "The draft is addressed to the Executive's own mailbox. Change it in Gmail.",
        )
    if draft.message.from_addr not in own:
        raise SendRefused(409, "not_from_you", "The draft isn't from one of your own addresses. Check it in Gmail.")

    reasons: list[str] = []
    if recipients != _addresses(payload.get("draft_to")) and _addresses(confirm.get("recipients")) != recipients:
        reasons.append("recipients_changed")
    moved_on = any(m.from_addr not in own and "SENT" not in m.labels for m in later)
    if moved_on and confirm.get("thread_moved_on") is not True:
        reasons.append("thread_moved_on")
    if reasons:
        raise SendRefused(
            409, "confirm", " ".join(_CONFIRM_TEXT[r] for r in reasons),
            reasons=reasons, recipients=recipients,
        )

    edited = draft.message.id != str(payload.get("draft_message_id") or "")
    if instance.id in SENDING:
        # Another tap is sending it right now; its mark is its own to clear.
        raise SendRefused(409, "already_handled", "This reply is being sent already.")
    SENDING.add(instance.id)
    try:
        if not claim_for_execution(instance.id, resolver_person_id=resolver):
            raise SendRefused(409, "already_handled", "This reply was already sent or dismissed.")
        if edited:
            _add_flag(person.id, message_id, "edited_in_gmail")
        # drafts.send sends the draft as it is when Gmail gets the request,
        # and everything above checked the version read before the claim:
        # read it once more, so an edit made meanwhile (another tab, a
        # delegate) is never sent unchecked.
        try:
            current = await client.get_draft(draft.draft_id)
        except GmailError as exc:
            release_claim(instance.id)
            raise SendRefused(502, "gmail_error", unreadable) from exc
        if current is None:
            release_claim(instance.id)
            SENDING.discard(instance.id)
            _close_card(person.id, instance.id, message_id, "draft_gone", CLOSED)
            raise SendRefused(409, "draft_gone", "That draft isn't in your Gmail any more: it was sent or deleted there.")
        if current.message.id != draft.message.id:
            release_claim(instance.id)
            raise SendRefused(
                409, "draft_changed",
                "The draft changed in Gmail just now, so nothing was sent. Look it over and tap Send again.",
            )
        try:
            sent = await client.send_draft(draft.draft_id)
        except GmailError as exc:
            if exc.maybe_done:
                # Left executing: the reconciler asks Gmail what happened.
                logger.warning("delegation.reply_send: Gmail didn't confirm a send (%s)", type(exc).__name__)
                raise SendRefused(
                    502, "send_unconfirmed",
                    "Gmail didn't confirm it was sent. Check your Sent folder in Gmail; this card updates on its own.",
                ) from exc
            release_claim(instance.id)
            if isinstance(exc, GmailNotFound):
                SENDING.discard(instance.id)
                _close_card(person.id, instance.id, message_id, "draft_gone", CLOSED)
                raise SendRefused(
                    409, "draft_gone", "That draft isn't in your Gmail any more: it was sent or deleted there.",
                ) from exc
            if isinstance(exc, GmailAuthError):
                raise SendRefused(409, "gmail_needs_reconnect", STATUS_MESSAGES["needs_reconnect"]) from exc
            if isinstance(exc, GmailRateLimited):
                raise SendRefused(429, "rate_limited", "Gmail asked to slow down. Nothing was sent; try again in a minute.") from exc
            raise SendRefused(
                502, "gmail_error", "Gmail refused to send it. Nothing was sent; try again, or send it from Gmail.",
            ) from exc
        # It went: whatever happens to the bookkeeping, say so. A card left
        # executing is settled by the reconciler from the sent message.
        try:
            final = STATUS_APPROVED_WITH_EDIT if edited else STATUS_APPROVED_UNCHANGED
            if not finish_execution(
                instance.id, final,
                final_payload={"sent_message_id": sent.id, "recipient_count": len(recipients), "edited": edited},
                external_event_id=sent.id or None,
            ):
                logger.warning("delegation.reply_send: a sent reply's card changed under it")
            _set_outcome(person.id, message_id, SENT, reason="sent")
            if sent.id:
                drafts.mark_sent(person.id, draft.draft_id, sent.id)
            _audit("delegation_reply_sent", f"Sent a reply as person {person.id}, on their tap", {
                "person_id": person.id, "decision_id": instance.id, "thread_id": thread_id,
                "sent_message_id": sent.id, "recipient_count": len(recipients), "edited": edited,
            })
        except Exception as exc:
            logger.warning("delegation.reply_send: recording a sent reply failed (%s)", type(exc).__name__)
        return sent.id
    finally:
        SENDING.discard(instance.id)
