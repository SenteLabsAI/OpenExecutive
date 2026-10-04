"""Send a reply the inbox watcher drafted, when the person taps Send, or
on its own under Handle it for me.

The one path in Act as me that sends anything, and all it can send is the
exact draft on a ``delegation_reply`` card, by its id
(``DelegateGmail.send_draft`` posts ``{"id": ...}`` to ``drafts.send``;
``DelegateOutlook.send_draft`` posts nothing to that draft's ``send``). Two
callers reach it, and a unit test walks the code to keep it that way:

- ``send_approved_reply``, after the person approved that card
  (``POST /decisions/{id}/approve``, a class only the card's own person sees
  or resolves). Only that route calls it. Sending is the person's own act: it
  works while the Executive is paused.
- ``send_on_its_own``, called only by the inbox watcher (``inbox._consider``)
  for a card it made as ``auto_execute`` after ``handle_it.refusal`` found
  nothing against it. The person turned Handle it for me on themselves, from
  a session the API knows is theirs; this path checks that switch and the
  counted rules (one a thread a day, the daily limit) again, and refuses
  anything the tap path would ask a second yes for.
  The watcher runs inside the scheduler's pause gate, so a pause stops it.

No model, chat turn, workflow or MCP call can reach either.

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
    """Raise unless the API knows the caller is the person at ``email``
    (``verified.caller_refusal``)."""
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal

    refused = caller_refusal(caller, email)
    if refused is None:
        return
    if refused == NOT_YOURS:
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    raise SendRefused(
        409,
        "caller_signing_required",
        "Sending from here needs signed sign-ins, so that nobody else can send as you. "
        "Until they're set up (Settings → Setup status → API protection), send it from your mailbox.",
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


async def send_on_its_own(instance: Any, *, gmail: Any = None, now: datetime | None = None) -> str:
    """Send the draft on ``instance``, a card the inbox watcher made as
    ``auto_execute`` under Handle it for me. Returns the sent message's id;
    raises ``SendRefused`` and then leaves the card for the person, as if the
    switch were off."""
    from openexecutive.audit import rows_for_person

    owner = getattr(instance, "approver_person_id", None)
    with rows_for_person(owner if isinstance(owner, int) else None):
        return await _send(
            instance, caller=None, resolver=None, confirm={}, gmail=gmail,
            now=now or datetime.now(UTC), on_its_own=True,
        )


def _addresses(value: Any) -> list[str] | None:
    from openexecutive.delegation.gmail import normalize_email

    if not isinstance(value, list):
        return None
    return sorted({normalize_email(a) for a in value if isinstance(a, str) and a.strip()})


async def _send(
    instance: Any, *, caller: Any, resolver: int | None, confirm: dict[str, Any], gmail: Any, now: datetime,
    on_its_own: bool = False,
) -> str:
    from openexecutive.config import get_settings
    from openexecutive.delegation import drafts, handle_it
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
        STATUS_EXECUTED,
        claim_for_execution,
        finish_execution,
        release_claim,
    )
    from openexecutive.people.store import get_person

    payload = card_payload(instance)
    person = get_person(int(payload.get("person_id") or 0))
    if (
        person is None or person.id is None or not person.email or not can_delegate(person)
        or instance.approver_person_id != person.id
        or (resolver != person.id if not on_its_own else resolver is not None)
    ):
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    email = normalize_email(person.email)
    if on_its_own:
        # The person's own switch stands in for their tap: it must still be
        # on, for this kind of sender, and the API must still be able to tie
        # it to them.
        handled_as = str(payload.get("handled_as") or "stranger")
        stored = handle_it.get(person.id)
        allowed = (
            stored.follows_up(handled_as) if payload.get("source") == "follow_up"
            else stored.level(handle_it.kind_for(handled_as)) == handle_it.LEVEL_HANDLE
        )
        if getattr(instance, "gate_mode", "") != "auto_execute" or not allowed:
            raise SendRefused(409, "handle_it_off", "Handle it for me is off for this reply.")
        if not handle_it.signing_ok():
            raise SendRefused(409, "caller_signing_required", handle_it.REASONS["signing_off"])
        counted = handle_it.count_refusal(person.id, str(payload.get("thread_id") or ""), now)
        if counted is not None:
            raise SendRefused(409, counted, handle_it.REASONS[counted])
    else:
        _check_caller(caller, email)
    if not is_enabled(person.id) or not get_watch(person.id).enabled:
        raise SendRefused(
            409, "inbox_off",
            "Turn on Act as me and Draft replies to my inbox to send from here, or send it from your mailbox.",
        )
    if _client_slot_active():
        raise SendRefused(409, "client_slot", "Sending from here is paused while a client is active. Send it from your mailbox.")
    client = gmail if gmail is not None else gmail_for(email)
    status = await gmail_status(email, gmail=client)
    if status != "connected":
        raise SendRefused(409, BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])

    message_id = str(payload.get("message_id") or "")
    thread_id = str(payload.get("thread_id") or "")
    unreadable = "Couldn't read the draft in your mailbox. Try again in a moment."
    try:
        draft = await client.get_draft(str(payload.get("draft_id") or ""))
        own = {email, *await client.send_as_addresses()}
    except GmailError as exc:
        raise SendRefused(502, "gmail_error", unreadable) from exc
    if draft is None:
        _close_card(person.id, instance.id, message_id, "draft_gone", CLOSED)
        raise SendRefused(409, "draft_gone", "That draft isn't in your mailbox any more: it was sent or deleted there.")
    try:
        thread = await client.get_thread(thread_id)
    except GmailNotFound:
        _close_card(person.id, instance.id, message_id, "thread_gone", CLOSED)
        raise SendRefused(409, "draft_gone", "That conversation isn't in your mailbox any more.") from None
    except GmailError as exc:
        raise SendRefused(502, "gmail_error", unreadable) from exc
    if draft.message.thread_id != thread_id:
        raise SendRefused(409, "draft_moved", "That draft isn't in the same conversation any more. Send it from your mailbox.")
    later = later_messages(thread, payload, now=now)
    if any("SENT" in m.labels and m.from_addr in own for m in later):
        _close_card(person.id, instance.id, message_id, "you_replied", CLOSED)
        raise SendRefused(409, "you_replied", "You've already replied in this conversation, so this draft wasn't sent.")

    recipients = sorted({*draft.message.to, *draft.message.cc, *draft.message.bcc})
    exec_address = normalize_email(get_settings().exec_email_address)
    if not recipients:
        raise SendRefused(409, "no_recipients", "The draft isn't addressed to anyone. Add someone in your mailbox.")
    if len(recipients) > MAX_RECIPIENTS:
        raise SendRefused(
            409, "too_many_recipients", f"The draft goes to more than {MAX_RECIPIENTS} people. Send it from your mailbox.",
        )
    if exec_address and exec_address in recipients:
        raise SendRefused(
            409, "executive_recipient", "The draft is addressed to the Executive's own mailbox. Change it in your mailbox.",
        )
    if draft.message.from_addr not in own:
        raise SendRefused(409, "not_from_you", "The draft isn't from one of your own addresses. Check it in your mailbox.")

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
    if on_its_own and edited:
        # Someone changed it in the mailbox since it was written: that
        # version was never checked, so the person sends it, not this path.
        raise SendRefused(409, "draft_changed", "The draft changed in your mailbox, so it waits for you.")
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
            raise SendRefused(409, "draft_gone", "That draft isn't in your mailbox any more: it was sent or deleted there.")
        if current.message.id != draft.message.id:
            release_claim(instance.id)
            raise SendRefused(
                409, "draft_changed",
                "The draft changed in your mailbox just now, so nothing was sent. Look it over and tap Send again.",
            )
        try:
            sent = await client.send_draft(draft.draft_id)
        except GmailError as exc:
            if exc.maybe_done:
                # Left executing: the reconciler asks Gmail what happened.
                logger.warning("delegation.reply_send: Gmail didn't confirm a send (%s)", type(exc).__name__)
                raise SendRefused(
                    502, "send_unconfirmed",
                    "Your mailbox didn't confirm it was sent. Check your Sent folder; this card updates on its own.",
                ) from exc
            release_claim(instance.id)
            if isinstance(exc, GmailNotFound):
                SENDING.discard(instance.id)
                _close_card(person.id, instance.id, message_id, "draft_gone", CLOSED)
                raise SendRefused(
                    409, "draft_gone", "That draft isn't in your mailbox any more: it was sent or deleted there.",
                ) from exc
            if isinstance(exc, GmailAuthError):
                raise SendRefused(409, "gmail_needs_reconnect", STATUS_MESSAGES["needs_reconnect"]) from exc
            if isinstance(exc, GmailRateLimited):
                raise SendRefused(429, "rate_limited", "Your mail service asked to slow down. Nothing was sent; try again in a minute.") from exc
            raise SendRefused(
                502, "gmail_error", "Your mailbox refused to send it. Nothing was sent; try again, or send it from your mailbox.",
            ) from exc
        # It went: whatever happens to the bookkeeping, say so. A card left
        # executing is settled by the reconciler from the sent message.
        if on_its_own:
            # First and on its own: the daily limit and the once-a-thread
            # rule count these rows, so nothing else failing may skip it.
            try:
                handle_it.record_handled(person.id, thread_id, instance.id, sent.id or None, now=now)
            except Exception:
                logger.exception("delegation.reply_send: recording a reply sent on its own failed")
        try:
            if on_its_own:
                final = STATUS_EXECUTED
            else:
                final = STATUS_APPROVED_WITH_EDIT if edited else STATUS_APPROVED_UNCHANGED
            if not finish_execution(
                instance.id, final,
                final_payload={
                    "sent_message_id": sent.id, "recipient_count": len(recipients), "edited": edited,
                    **({"on_its_own": True} if on_its_own else {}),
                },
                external_event_id=sent.id or None,
            ):
                logger.warning("delegation.reply_send: a sent reply's card changed under it")
            _set_outcome(person.id, message_id, SENT, reason="handled" if on_its_own else "sent")
            if sent.id:
                drafts.mark_sent(person.id, draft.draft_id, sent.id)
            if on_its_own:
                _audit("delegation_reply_handled", f"Sent a reply as person {person.id}, on its own", {
                    "person_id": person.id, "decision_id": instance.id, "thread_id": thread_id,
                    "sent_message_id": sent.id, "recipient_count": len(recipients),
                })
            else:
                _audit("delegation_reply_sent", f"Sent a reply as person {person.id}, on their tap", {
                    "person_id": person.id, "decision_id": instance.id, "thread_id": thread_id,
                    "sent_message_id": sent.id, "recipient_count": len(recipients), "edited": edited,
                })
        except Exception as exc:
            logger.warning("delegation.reply_send: recording a sent reply failed (%s)", type(exc).__name__)
        if not on_its_own:
            # History notes come from the person's own words; a reply sent on
            # its own isn't that.
            _note_sent(person.id, current.message, thread, recipients, thread_id, now)
        return sent.id
    finally:
        SENDING.discard(instance.id)


# Background note-taking tasks, held so they aren't collected mid-run.
_NOTE_TASKS: set[Any] = set()


def _note_sent(person_id: int, message: Any, thread: Any, recipients: list[str], thread_id: str, now: datetime) -> None:
    """Always in the loop: once a reply went, note what the person told whom
    (``memory.history_notes``), in the background, from the words they sent
    alone. Off unless they turned it on; a failure never touches the send."""
    import asyncio

    try:
        from openexecutive.memory.history import person_settings
        from openexecutive.memory.history_notes import note_sent_reply

        if not person_settings(person_id).reply_notes:
            return
        names = {
            m.from_addr: m.from_name
            for m in getattr(thread, "messages", []) or []
            if getattr(m, "from_addr", "") and getattr(m, "from_name", "")
        }
        task = asyncio.get_running_loop().create_task(note_sent_reply(
            person_id,
            body=str(getattr(message, "text", "") or ""),
            to_names=[names.get(addr, "") for addr in recipients],
            to_addresses=list(recipients),
            subject=str(getattr(message, "subject", "") or ""),
            thread_id=thread_id,
            sent_at=now,
        ))
        _NOTE_TASKS.add(task)
        task.add_done_callback(_NOTE_TASKS.discard)
    except Exception as exc:
        logger.warning("delegation.reply_send: couldn't start noting a sent reply (%s)", type(exc).__name__)
