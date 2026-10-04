"""Follow-ups as you: chase an email of the person's that nobody answered,
from their own mailbox, under Handle it for me.

Part of the inbox watcher's scan (``inbox._scan``), at most once every
``LOOK_EVERY`` and only while Handle it for me is on. Plain code picks the
emails (``unanswered``): the person's own sent mail, ``AFTER`` to ``WITHIN``
old, that asked something (a question mark in their own words), went to at
most ``MAX_RECIPIENTS`` people other than the Executive, and is still the last
message in its thread. Each gets one follow-up, ever: the inbox ledger's row
for that sent message (``inbox._claim``) records it.

The ghostwriter writes a short nudge in their voice, from the thread; as for
replies it has no tools, so the thread's text can at most change the words.
Whether it goes on its own is ``handle_it.follow_up_refusal``, plain code, by
the setting: never on Careful, to their team and contacts on Balanced, to
anyone they wrote to on Bold; only to exactly the people their email went to,
and never on a sensitive topic or with a link or an amount. It goes through
the one send path (``reply_send.send_on_its_own``) on an ordinary
``delegation_reply`` card marked ``source: follow_up``; anything refused
waits on Today, where they send, edit or dismiss it. When anyone answers in
the thread first, the card closes (``inbox._reconcile_card``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

AFTER = timedelta(days=3)
WITHIN = timedelta(days=10)
LOOK_EVERY = timedelta(hours=1)
SENT_LOOKED_AT = 40
PER_LOOK = 2

# How well the person knows someone, lowest first (``inbox.relation_of``).
_TRUST = {"stranger": 0, "correspondent": 1, "contact": 2, "team": 3}
FOLLOW_UP_INTENT = (
    "Write a short, friendly follow-up to the writer's own last email in "
    "<thread>, which nobody has answered yet: ask whether they had a chance "
    "to look at it. Two or three sentences. Don't repeat the whole email, and "
    "add nothing new: no facts, dates, figures, links or promises."
)

# When each person's sent mail was last looked at, in this process.
_LAST_LOOK: dict[int, datetime] = {}


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def recipients(sent: Any, own: set[str], exec_address: str) -> list[str]:
    """Everyone ``sent`` went to, but the person and the Executive."""
    skip = own | ({exec_address} if exec_address else set())
    return [a for a in dict.fromkeys([*sent.to, *sent.cc]) if a and a not in skip]


def unanswered(sent: Any, thread: Any, *, own: set[str], exec_address: str, now: datetime) -> bool:
    """Whether ``sent`` is a question of theirs still waiting for an answer:
    theirs, old enough and not too old, asking something, to a few people,
    and nothing in the thread since."""
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.integrations.email_poller import sender_new_text

    if sent.from_addr not in own or "SENT" not in sent.labels:
        return False
    sent_at = _parse(sent.received_at)
    if sent_at is None or not (now - WITHIN <= sent_at <= now - AFTER):
        return False
    if "?" not in sender_new_text(sent.text or ""):
        return False
    going = recipients(sent, own, exec_address)
    if not going or len(going) > MAX_RECIPIENTS:
        return False
    newest = [m for m in thread.messages if "DRAFT" not in m.labels]
    return bool(newest) and newest[-1].id == sent.id


async def look(person: Any, client: Any, *, own: set[str], exec_address: str, now: datetime, result: Any) -> None:
    """Draft follow-ups for ``person``'s unanswered email, at most
    ``PER_LOOK`` a look, and send the ones Handle it for me may."""
    from openexecutive.delegation import handle_it, inbox
    from openexecutive.delegation.gmail import GmailError

    if not handle_it.get(person.id).enabled:
        return
    last = _LAST_LOOK.get(person.id)
    if last is not None and now - last < LOOK_EVERY:
        return
    _LAST_LOOK[person.id] = now
    carded = {str(inbox.card_payload(c).get("thread_id") or "") for c in inbox.open_cards(person.id)}
    picked = 0
    seen: set[str] = set()
    for sent in await client.list_sent(limit=SENT_LOOKED_AT):
        if picked >= PER_LOOK:
            break
        if sent.thread_id in seen or sent.thread_id in carded:
            continue
        seen.add(sent.thread_id)
        sent_at = _parse(sent.received_at)
        if sent_at is None or sent_at > now - AFTER or sent_at < now - WITHIN:
            continue  # the cheap test first: most sent mail is never read further
        if inbox._recorded(person.id, sent.id):
            continue
        thread = await client.get_thread(sent.thread_id)
        if not unanswered(sent, thread, own=own, exec_address=exec_address, now=now):
            continue
        try:
            if await _follow_up(person, client, sent, thread, own=own, exec_address=exec_address, now=now,
                                result=result):
                picked += 1
        except GmailError:
            raise  # the mailbox itself is failing: the scan backs off
        except Exception as exc:
            logger.warning("delegation.follow_ups: a follow-up failed (%s)", type(exc).__name__)
            inbox._set_outcome(person.id, sent.id, inbox.FAILED, reason="follow_up_failed")


async def _follow_up(
    person: Any, client: Any, sent: Any, thread: Any, *, own: set[str], exec_address: str, now: datetime,
    result: Any,
) -> bool:
    """Draft one follow-up and leave its card, or send it. True when a draft
    was made."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import caps, drafts, handle_it, inbox
    from openexecutive.delegation.ghostwriter import ComposeError, Recipient, compose
    from openexecutive.delegation.gmail import DraftSpec, references_header
    from openexecutive.delegation.inbox_classifier import Verdict
    from openexecutive.delegation.threads import thread_text, writer_said
    from openexecutive.delegation.voice import composer_model, get_voice, render_voice_block

    email = (person.email or "").strip().lower()
    going = recipients(sent, own, exec_address)
    to = [a for a in dict.fromkeys(sent.to) if a in going]
    cc = [a for a in going if a not in to]
    if not to:
        to, cc = cc[:1], cc[1:]
    # Weighed by the least-known person it goes to: a stranger on Cc makes it a stranger's.
    relations = [await inbox.relation_of(a, client, contacts=bool(person.is_principal)) for a in going]
    relation = min(relations, key=lambda r: _TRUST.get(r, 0), default="stranger")
    if not inbox._claim(person.id, sent, relation=relation, outcome=inbox.PROCESSING, now=now):
        return False
    settings = get_settings()
    if caps.reserve(person.id, settings.delegation_max_drafts_per_day) is not None:
        inbox._retry_later(person.id, sent.id, "daily_limit", count=False)
        return False
    saved = False
    try:
        stored = get_voice(person.id)
        names = (person.full_name or "").split()
        try:
            composed = await compose(
                writer_name=" ".join(names) or email,
                voice_block=render_voice_block(stored.profile, first_name=names[0] if names else "them"),
                thread_text=thread_text(thread, email),
                writer_said=writer_said(thread, email),
                reply_subject=sent.subject if (sent.subject or "").lower().startswith("re:")
                else f"Re: {sent.subject or ''}".strip(),
                intent=FOLLOW_UP_INTENT,
                recipients=[Recipient(email=a) for a in going],
                signature=stored.profile.signature,
                exec_name=settings.exec_display_name,
                model=composer_model(),
                now=now,
            )
        except ComposeError:
            inbox._retry_later(person.id, sent.id, "compose_failed")
            return False
        reply = inbox.Reply(
            to=to, cc=cc, subject=composed.subject, body=composed.body,
            open_questions=list(composed.open_questions), flags=list(composed.flags),
            in_reply_to=sent.message_id_header or None,
            references=references_header(sent.references, sent.message_id_header),
        )
        held = handle_it.follow_up_refusal(
            person.id, sent, reply, relation=relation, own=own, exec_address=exec_address, now=now,
        )
        draft = await client.create_draft(DraftSpec(
            to=reply.to, cc=reply.cc, subject=reply.subject, body=reply.body, thread_id=thread.id,
            in_reply_to=reply.in_reply_to, references=reply.references,
            from_name=" ".join(names), from_addr=None if sent.from_addr == email else sent.from_addr,
        ))
        saved = True
        try:
            drafts.record(
                person.id, source=drafts.SOURCE_INBOX, thread_id=thread.id,
                draft_id=draft.draft_id, message_id=draft.message_id, now=now,
            )
        except Exception:
            logger.warning("delegation.follow_ups: couldn't record the draft", exc_info=True)
    finally:
        caps.release(person.id, saved=saved)
    try:
        decision_id = inbox._create_card(
            person, sent, thread, reply, draft, relation=relation, handled_as=relation,
            verdict=Verdict(needs_reply=True, kind="follow_up", confidence=1.0),
            on_its_own=held is None, handle_it_reason=held, source=inbox.FOLLOW_UP_SOURCE,
        )
    except Exception:
        # No card, so no draft either: a draft with no card would wait unseen.
        await client.delete_draft(draft.draft_id)
        raise
    inbox._set_outcome(person.id, sent.id, inbox.DRAFTED, decision_id=decision_id)
    result.drafted += 1
    inbox._audit("delegation_follow_up_drafted", f"Drafted a follow-up as person {person.id}", {
        "person_id": person.id, "thread_id": thread.id, "draft_id": draft.draft_id,
        "decision_id": decision_id, "relation": relation, "handle_it": held or "send",
    })
    if held is None and decision_id is not None:
        await inbox._send_on_its_own(person, client, decision_id, result, now=now)
    return True
