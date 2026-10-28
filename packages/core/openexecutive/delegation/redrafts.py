"""Take a chat draft out of the person's Drafts once something replaces it.

A chat draft (``ghostwrite_email``) waits in the person's own mailbox until
they send it. Two things leave it behind with nobody meaning to send it:

- a redraft: they asked for a change and a new draft was saved in the same
  thread, or in the same conversation to the same people about the same
  thing (:func:`replace_earlier`);
- the Executive sent that email itself, from its own mailbox, in the same
  conversation, to the same people and in the same thread or about the same
  thing (:func:`clear_after_send`).

"About the same thing" is a matching subject, its "Re:" / "Fwd:" aside, or
one subject holding the other: another email to the same person in the same
conversation is left alone.

Each such draft is deleted from their mailbox and stops counting as waiting,
so the briefs stop naming it. A draft they edited in their mailbox is theirs
and stays: its version (the message id Gmail gives an edited draft, or
Outlook's change key) is no longer the one it was saved with.

Never raises: a draft left in place costs a stale draft, never the new one
or the send.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from openexecutive.delegation import drafts

logger = logging.getLogger(__name__)


_MIN_SUBJECT = 8


def same_topic(a: str, b: str) -> bool:
    """Whether two subjects are about the same email: equal without their
    prefixes, or one holding the other once that is long enough to mean it."""
    x, y = drafts._same_subject(a), drafts._same_subject(b)
    if not x or not y:
        return False
    if x == y:
        return True
    shorter, longer = sorted((x, y), key=len)
    return len(shorter) >= _MIN_SUBJECT and shorter in longer


def _people(message: Any, *fields: str) -> set[str]:
    return {
        str(a).strip().lower()
        for field in fields
        for a in (getattr(message, field, None) or [])
        if str(a).strip()
    }


async def _retire(client: Any, row: drafts.DraftRow, found: Any, now: datetime) -> str:
    """Delete ``row``'s draft (already read as ``found``) unless it was
    edited; returns ``deleted``, ``already_gone`` or ``kept_edited``."""
    if found is None:
        drafts.mark_gone(row.id, now)
        return "already_gone"
    if str(getattr(found.message, "id", "") or "") != row.message_id:
        return "kept_edited"
    deleted = await client.delete_draft(row.draft_id)
    drafts.mark_gone(row.id, now)
    return "deleted" if deleted else "already_gone"


async def _read(client: Any, row: drafts.DraftRow) -> tuple[bool, Any]:
    """``(readable, draft or None)``: not readable for an id this mailbox
    can't take or a failed read, which leaves the row alone."""
    from openexecutive.delegation.gmail import valid_id as gmail_id

    valid_id = getattr(client, "valid_id", gmail_id)
    if not valid_id(row.draft_id):
        return False, None
    try:
        return True, await client.get_draft(row.draft_id)
    except Exception:
        logger.info("redrafts: couldn't read draft row %s", row.id, exc_info=True)
        return False, None


def _audit(person_id: int, summary: str, outcomes: list[str]) -> None:
    from openexecutive.audit import log_event

    try:
        log_event(
            "delegation_draft_replaced",
            summary,
            actor="executive",
            details={"person_id": person_id, "drafts": outcomes},
            private=True,
            private_to_person=person_id,
        )
    except Exception:
        logger.warning("redrafts: couldn't audit", exc_info=True)


async def replace_earlier(
    person_id: int,
    client: Any,
    *,
    row_id: int,
    thread_id: str | None,
    session_id: str | None,
    to: list[str],
    subject: str = "",
    now: datetime | None = None,
) -> int:
    """After the chat draft ``row_id`` was saved: retire ``person_id``'s
    earlier open chat drafts it replaces, the ones in the same thread and
    the ones written in the same conversation to any of the same people
    (``to``) about the same thing (``subject``). Returns how many were
    deleted."""
    now = now or datetime.now(UTC)
    try:
        rows = drafts.open_chat_drafts(
            person_id=person_id, thread_id=thread_id or None, session_id=session_id, before_id=row_id,
        )
    except Exception:
        logger.warning("redrafts: couldn't read the open drafts", exc_info=True)
        return 0
    wanted = {a.strip().lower() for a in to if a.strip()}
    outcomes: list[str] = []
    for row in rows:
        if row.person_id != person_id:
            continue
        readable, found = await _read(client, row)
        if not readable:
            continue
        same_thread = bool(thread_id) and row.thread_id == thread_id
        if not same_thread and found is not None and not (
            wanted & _people(found.message, "to") and same_topic(subject, found.message.subject)
        ):
            continue  # another email written in the same conversation
        try:
            outcomes.append(await _retire(client, row, found, now))
        except Exception:
            logger.info("redrafts: couldn't retire draft row %s", row.id, exc_info=True)
    if outcomes:
        _audit(person_id, f"Replaced earlier drafts for person {person_id}", outcomes)
    return outcomes.count("deleted")


async def clear_after_send(
    session_id: str, sent_to: set[str], *, subject: str = "", client: Any = None,
    client_person_id: int | None = None, now: datetime | None = None,
) -> int:
    """After the Executive sent an email from its own mailbox in the
    conversation ``session_id`` to ``sent_to`` about ``subject``: retire
    that conversation's open chat drafts to any of the same people (the
    writer themself aside) about the same thing. ``client`` opens the
    mailbox of ``client_person_id`` (anyone, when that is None); everyone
    else's opens through their own connection. Returns how many were
    deleted."""
    if not session_id or not sent_to:
        return 0
    now = now or datetime.now(UTC)
    try:
        rows = drafts.open_chat_drafts(session_id=session_id)
    except Exception:
        logger.warning("redrafts: couldn't read the open drafts", exc_info=True)
        return 0
    if not rows:
        return 0
    from openexecutive.delegation.gmail import gmail_for, normalize_email
    from openexecutive.people.store import get_person

    sent = {a.strip().lower() for a in sent_to if a.strip()}
    deleted = 0
    for person_id in sorted({r.person_id for r in rows}):
        person = get_person(person_id)
        email = normalize_email(getattr(person, "email", "") or "")
        if not email:
            continue
        mine = client is not None and client_person_id in (None, person_id)
        mailbox = client if mine else gmail_for(email)
        outcomes: list[str] = []
        for row in (r for r in rows if r.person_id == person_id):
            readable, found = await _read(mailbox, row)
            if not readable:
                continue
            if found is not None and not (
                (sent - {email}) & _people(found.message, "to", "cc")
                and same_topic(subject, found.message.subject)
            ):
                continue
            try:
                outcomes.append(await _retire(mailbox, row, found, now))
            except Exception:
                logger.info("redrafts: couldn't retire draft row %s", row.id, exc_info=True)
        if outcomes:
            _audit(person_id, f"Removed drafts the Executive sent itself for person {person_id}", outcomes)
            deleted += outcomes.count("deleted")
    return deleted
