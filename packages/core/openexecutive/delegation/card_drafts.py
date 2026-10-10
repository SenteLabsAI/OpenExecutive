"""Act as me: where a reply the inbox watcher wrote lives, and changing it
from its card.

Two places, by the person's own choice on the Draft replies to my inbox
card (``InboxWatch.mailbox_drafts``):

- On the card alone (the default). Nothing is saved in their mailbox, so
  their Drafts folder doesn't fill up with replies they may never send. The
  card keeps what a draft would hold (``draft_spec``: who it goes to, the
  subject, the thread headers) and the words (``draft_body``). Send makes
  the draft from exactly that, at that moment, and sends it through the same
  checked path as any other (``reply_send``); a refusal before it went takes
  the draft back out of their mailbox.
- Also in their mailbox's Drafts, as before: they can open it there, and
  Send sends it exactly as it is there.

Either way the words can be changed on the card (``edit``) before Send. A
reply in their mailbox is rewritten there too (``update_draft_text``), and
only while it is still the version the card last saw: an edit made in the
mailbox meanwhile is never overwritten. Only the card's own person, from a
request the API can tie to them, can change it, and never while it is being
sent on its own. Nothing here sends anything.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The longest reply that can be typed on a card.
MAX_TEXT_CHARS = 20_000

# Card payload keys for a reply kept on its card alone.
CARD_ONLY = "card_only"
DRAFT_SPEC = "draft_spec"
DRAFT_KEY = "draft_key"
# Set once the person changed the words on the card.
EDITED_ON_CARD = "edited_on_card"
# A reply in their mailbox: its version after the last edit on the card.
EDITED_VERSION = "edited_version"

# Cards whose draft is being made right now in this process, so two taps
# never make two.
_MAKING: set[int] = set()
# Cards whose words are being changed right now in this process: one edit at
# a time, so a second can't rewrite the mailbox draft and then lose the card.
_EDITING: set[int] = set()


def in_mailbox(person_id: int) -> bool:
    """Whether ``person_id``'s replies are also saved in their mailbox's
    Drafts. Unreadable is no: nothing lands in their mailbox unasked."""
    from openexecutive.delegation.inbox import get_watch

    return get_watch(person_id).mailbox_drafts


async def save(person_id: int, client: Any, spec: Any) -> Any:
    """Save ``spec`` in ``person_id``'s Drafts when they keep replies there;
    otherwise nothing is saved and the returned draft has no id (the card
    keeps the reply, ``card_fields``)."""
    from openexecutive.delegation.gmail import CreatedDraft

    if in_mailbox(person_id):
        return await client.create_draft(spec)
    return CreatedDraft(draft_id="", message_id="", thread_id=spec.thread_id or "")


def card_key(message_id: str) -> str:
    """The ``delegation_drafts`` row id of a reply kept on its card, until it
    becomes a real draft (``drafts.rename``)."""
    return f"card-{message_id}"


def card_fields(spec: Any, key: str) -> dict[str, Any]:
    """What a card needs to make the draft itself when it is sent."""
    return {
        CARD_ONLY: True,
        DRAFT_KEY: key,
        DRAFT_SPEC: {
            "to": list(spec.to),
            "cc": list(spec.cc),
            "subject": spec.subject,
            "thread_id": spec.thread_id,
            "in_reply_to": spec.in_reply_to,
            "references": spec.references,
            "from_name": spec.from_name,
            "from_addr": spec.from_addr,
        },
    }


def is_card_only(payload: dict[str, Any]) -> bool:
    """A reply kept on its card that hasn't become a draft yet."""
    return payload.get(CARD_ONLY) is True and not payload.get("draft_id")


def _spec(payload: dict[str, Any]) -> Any:
    from openexecutive.delegation.gmail import DraftSpec

    stored = payload.get(DRAFT_SPEC)
    stored = stored if isinstance(stored, dict) else {}

    def text(key: str) -> str | None:
        value = stored.get(key)
        return value if isinstance(value, str) and value else None

    def addresses(key: str) -> list[str]:
        value = stored.get(key)
        return [a for a in value if isinstance(a, str) and a] if isinstance(value, list) else []

    return DraftSpec(
        to=addresses("to"),
        cc=addresses("cc"),
        subject=text("subject") or str(payload.get("draft_subject") or ""),
        body=str(payload.get("draft_body") or ""),
        thread_id=text("thread_id") or str(payload.get("thread_id") or "") or None,
        in_reply_to=text("in_reply_to"),
        references=text("references"),
        from_name=text("from_name") or "",
        from_addr=text("from_addr"),
    )


async def make_draft(instance: Any, person_id: int, client: Any) -> dict[str, Any]:
    """Send, for a reply kept on its card: save it in the person's Drafts
    exactly as the card shows it, and record that on the card, so the send
    path checks and sends it like any other (and the reconciler can settle
    it). Returns the card's payload as it now is. Raises ``SendRefused``."""
    from openexecutive.delegation import drafts
    from openexecutive.delegation.gmail import GmailError
    from openexecutive.delegation.inbox import card_payload
    from openexecutive.delegation.reply_send import SendRefused
    from openexecutive.memory.decision_ledger import get_decision_instance, update_open_payload

    if instance.id in _MAKING or instance.id in _EDITING:
        raise SendRefused(409, "busy", "Your change to this reply is still being saved. Try again in a moment.")
    _MAKING.add(instance.id)
    try:
        current = get_decision_instance(instance.id)
        payload = card_payload(current) if current is not None else {}
        if current is None or not is_card_only(payload):
            # Made meanwhile (another tap) or no longer waiting.
            raise SendRefused(409, "already_handled", "This reply was already sent or dismissed.")
        spec = _spec(payload)
        if not spec.to or not spec.body.strip():
            raise SendRefused(409, "no_recipients", "This reply isn't addressed to anyone, so it wasn't sent.")
        try:
            created = await client.create_draft(spec)
        except GmailError as exc:
            raise SendRefused(
                502, "gmail_error", "Couldn't reach your mailbox to send it. Nothing was sent; try again in a moment.",
            ) from exc
        made = {**payload, "draft_id": created.draft_id, "draft_message_id": created.message_id}
        if not update_open_payload(instance.id, made, expected_json=current.proposed_payload_json):
            await _delete_quietly(client, created.draft_id)
            raise SendRefused(409, "already_handled", "This reply was already sent or dismissed.")
        try:
            drafts.rename(person_id, str(payload.get(DRAFT_KEY) or ""), created.draft_id, created.message_id)
        except Exception:
            logger.warning("delegation.card_drafts: couldn't record the draft", exc_info=True)
        return made
    finally:
        _MAKING.discard(instance.id)


async def take_back(instance: Any, person_id: int, payload: dict[str, Any], client: Any) -> None:
    """A send refused after ``make_draft``: take the draft back out of the
    person's mailbox and keep the reply on its card again. Best-effort; a
    card that closed meanwhile only loses the draft."""
    from openexecutive.delegation import drafts
    from openexecutive.delegation.inbox import card_payload
    from openexecutive.memory.decision_ledger import get_decision_instance, update_open_payload

    draft_id = str(payload.get("draft_id") or "")
    if not draft_id:
        return
    await _delete_quietly(client, draft_id)
    current = get_decision_instance(instance.id)
    if current is None:
        return
    now = card_payload(current)
    if now.get("draft_id") != draft_id:
        return
    back = {**now, "draft_id": "", "draft_message_id": ""}
    if update_open_payload(instance.id, back, expected_json=current.proposed_payload_json):
        try:
            drafts.rename(person_id, draft_id, str(payload.get(DRAFT_KEY) or ""), "")
        except Exception:
            logger.warning("delegation.card_drafts: couldn't record the draft taken back", exc_info=True)


async def _delete_quietly(client: Any, draft_id: str) -> None:
    try:
        await client.delete_draft(draft_id)
    except Exception:
        logger.warning("delegation.card_drafts: couldn't take a draft back out of the mailbox", exc_info=True)


async def edit(instance: Any, *, caller: Any, resolver: int | None, text: str, gmail: Any = None) -> dict[str, Any]:
    """Change the words of the reply on ``instance`` to ``text``, on the
    person's tap. Returns the card's payload as it now is; raises
    ``SendRefused``. Sends nothing."""
    from openexecutive.audit import rows_for_person
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.inbox import SENDING, card_payload
    from openexecutive.delegation.reply_send import _NOT_YOURS, SendRefused, _check_caller
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.memory.decision_ledger import STATUS_PROPOSED
    from openexecutive.people.store import get_person

    payload = card_payload(instance)
    person = get_person(int(payload.get("person_id") or 0))
    if (
        person is None or person.id is None or not person.email or not can_delegate(person)
        or instance.approver_person_id != person.id or resolver != person.id
    ):
        raise SendRefused(403, "not_yours", _NOT_YOURS)
    with rows_for_person(person.id):
        _check_caller(caller, normalize_email(person.email))
        if instance.status != STATUS_PROPOSED or instance.id in SENDING:
            raise SendRefused(409, "already_handled", "This reply was already sent or dismissed.")
        if getattr(instance, "gate_mode", "") == "auto_execute":
            raise SendRefused(409, "already_handled", "This reply is being sent on its own right now.")
        if not is_enabled(person.id):
            raise SendRefused(409, "inbox_off", "Turn on Act as me to change this reply here.")
        words = text.rstrip()
        if not words.strip():
            raise SendRefused(422, "empty", "Write something before you save it.")
        if len(words) > MAX_TEXT_CHARS:
            raise SendRefused(422, "too_long", f"That's too long to send from here (at most {MAX_TEXT_CHARS:,} characters).")
        if instance.id in _EDITING or instance.id in _MAKING:
            raise SendRefused(409, "busy", "This reply is being saved or sent right now. Try again in a moment.")
        _EDITING.add(instance.id)
        try:
            return await _edit(instance, person, payload, words, gmail)
        finally:
            _EDITING.discard(instance.id)


async def _edit(instance: Any, person: Any, payload: dict[str, Any], words: str, gmail: Any) -> dict[str, Any]:
    """``edit`` once its checks passed, one at a time per card."""
    from openexecutive.delegation.gmail import (
        BLOCKING_CODES,
        STATUS_MESSAGES,
        DraftChanged,
        GmailError,
        GmailNotFound,
        gmail_for,
        gmail_status,
        normalize_email,
    )
    from openexecutive.delegation.inbox import CLOSED, _audit, _close_card
    from openexecutive.delegation.reply_send import SendRefused
    from openexecutive.memory.decision_ledger import update_open_payload

    changed = {**payload, "draft_body": words, EDITED_ON_CARD: True}
    draft_id = str(payload.get("draft_id") or "")
    if draft_id:
        email = normalize_email(person.email)
        client = gmail if gmail is not None else gmail_for(email)
        status = await gmail_status(email, gmail=client)
        if status != "connected":
            raise SendRefused(409, BLOCKING_CODES.get(status, "gmail_error"), STATUS_MESSAGES[status])
        seen = str(payload.get(EDITED_VERSION) or payload.get("draft_message_id") or "")
        try:
            version = await client.update_draft_text(draft_id, seen, words)
        except GmailNotFound:
            _close_card(person.id, instance.id, str(payload.get("message_id") or ""), "draft_gone", CLOSED)
            raise SendRefused(
                409, "draft_gone", "That draft isn't in your mailbox any more: it was sent or deleted there.",
            ) from None
        except DraftChanged as exc:
            if exc.reason == "not_plain":
                raise SendRefused(
                    409, "draft_not_plain", "This draft has files or formatting, so change it in your mailbox.",
                ) from None
            raise SendRefused(
                409, "draft_changed",
                "This draft was changed in your mailbox, so it wasn't changed here. Change it there, or dismiss it.",
            ) from None
        except GmailError as exc:
            raise SendRefused(
                502, "gmail_error", "Couldn't save your change in your mailbox. Try again in a moment.",
            ) from exc
        changed[EDITED_VERSION] = version
    if not update_open_payload(instance.id, changed, expected_json=instance.proposed_payload_json):
        raise SendRefused(409, "already_handled", "This reply changed or was sent meanwhile. Look at it again.")
    _audit("delegation_reply_edited", f"Person {person.id} changed a reply on its card", {
        "person_id": person.id, "decision_id": instance.id, "in_mailbox": bool(draft_id),
    })
    return changed
