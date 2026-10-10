"""The reply cards the inbox watcher leaves for a person (``delegation.inbox``).

``cards`` lists them for ``GET /delegation/replies`` from the database alone:
no Gmail call, so the Today page never waits on Google. ``dismiss`` runs after
a card is rejected (``DecisionClassSpec.after_reject`` for
``delegation_reply``): a draft nobody edited is deleted from their Gmail
Drafts; one they edited there stays. A draft is unedited when its message id
is still the one it was saved with (Gmail gives an edited draft a new one).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReplyCard:
    decision_id: int
    status: str
    created_at: str
    thread_id: str
    draft_id: str
    from_name: str
    from_email: str
    relation: str
    sender_verified: bool
    subject: str
    received_at: str
    they_wrote: str
    draft_to: list[str]
    draft_subject: str
    draft_body: str
    open_questions: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    gmail_link: str = ""
    # Why Handle it for me left it for them, in plain words ("" when it didn't decide).
    waited_because: str = ""
    # "follow_up" when the draft chases the person's own unanswered email
    # (delegation.follow_ups); "" for a reply to someone else's.
    source: str = ""
    # Send + allow: Replies (Follow-ups, on a follow-up) is in training and
    # this sender (these people) isn't allowed yet (delegation.training).
    can_allow: bool = False
    # Drafts is in training: "Do it like this next time" keeps a draft they
    # changed as how they write to this person.
    learns_style: bool = False
    # Also saved in their mailbox's Drafts; False when it lives on this card
    # alone until Send (delegation.card_drafts).
    in_mailbox: bool = True
    # Which of their mailboxes it sends from: "Gmail" or "Outlook".
    mailbox: str = "Gmail"


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def cards(person: Any) -> list[ReplyCard]:
    """``person``'s open reply cards, newest first."""
    from openexecutive.delegation import training
    from openexecutive.delegation.gmail import credential_provider, mailbox_link
    from openexecutive.delegation.handle_it import REASONS
    from openexecutive.delegation.inbox import card_payload, ledger_flags, open_cards

    open_ = open_cards(person.id)
    payloads = [(c, card_payload(c)) for c in open_]
    added = ledger_flags(person.id, [str(p.get("message_id") or "") for _, p in payloads])
    email = (person.email or "").strip().lower()
    trained = training.get(person.id)
    mailbox = "Outlook" if email and credential_provider(email) == "microsoft" else "Gmail"
    out: list[ReplyCard] = []
    for card, payload in payloads:
        thread_id = str(payload.get("thread_id") or "")
        draft_id = str(payload.get("draft_id") or "")
        flags = list(dict.fromkeys([
            *_strings(payload.get("flags")), *added.get(str(payload.get("message_id") or ""), []),
        ]))
        out.append(ReplyCard(
            decision_id=card.id,
            status=card.status,
            created_at=card.created_at,
            thread_id=thread_id,
            draft_id=draft_id,
            from_name=str(payload.get("from_name") or ""),
            from_email=str(payload.get("from_email") or ""),
            relation=str(payload.get("relation") or ""),
            sender_verified=payload.get("sender_verified") is True,
            subject=str(payload.get("subject") or ""),
            received_at=str(payload.get("received_at") or ""),
            they_wrote=str(payload.get("they_wrote") or ""),
            draft_to=_strings(payload.get("draft_to")),
            draft_subject=str(payload.get("draft_subject") or ""),
            draft_body=str(payload.get("draft_body") or ""),
            open_questions=_strings(payload.get("open_questions")),
            flags=flags,
            gmail_link=(
                mailbox_link(email, thread_id=thread_id, draft_id=draft_id) if email and thread_id and draft_id else ""
            ),
            waited_because=REASONS.get(str(payload.get("handle_it_reason") or ""), ""),
            source="follow_up" if payload.get("source") == "follow_up" else "",
            can_allow=_can_allow(person.id, payload, trained),
            learns_style=trained.drafts,
            in_mailbox=bool(draft_id),
            mailbox=mailbox,
        ))
    return out


def _can_allow(person_id: int, payload: dict[str, Any], trained: Any) -> bool:
    from openexecutive.delegation import handle_it

    if payload.get("source") == "follow_up":
        going = _strings(payload.get("draft_to"))
        return bool(trained.follow_ups and going and not handle_it.follow_up_allowed(person_id, going))
    sender = str(payload.get("from_email") or "")
    return bool(trained.replies and sender and not handle_it.allowed_in_training(person_id, sender))


async def dismiss(instance: Any, *, gmail: Any = None) -> str:
    """After a card was rejected: delete its draft when nobody edited it.
    Returns what happened (``deleted``, ``kept_edited``, ``already_gone``,
    ``card_only``, ``not_connected``, ``gmail_error``). Gmail errors propagate after the
    outcome is recorded; the reject already stands."""
    from openexecutive.delegation.gmail import gmail_for, gmail_status
    from openexecutive.delegation.inbox import DISMISSED, _audit, _set_outcome, card_payload
    from openexecutive.people.store import get_person

    payload = card_payload(instance)
    person = get_person(int(payload.get("person_id") or instance.approver_person_id or 0))
    if person is None or person.id is None or not person.email:
        return "not_connected"
    email = person.email.strip().lower()
    client = gmail if gmail is not None else gmail_for(email)
    # Recorded even when Gmail fails, so the ledger never shows a dismissed
    # card as still drafted.
    outcome = "gmail_error"
    try:
        if not payload.get("draft_id"):
            # Kept on its card alone: nothing in their mailbox to remove.
            outcome = "card_only"
        elif await gmail_status(email, gmail=client) != "connected":
            outcome = "not_connected"
        else:
            draft = await client.get_draft(str(payload.get("draft_id") or ""))
            # Changed on its card is theirs to dismiss there; changed in the
            # mailbox stays there.
            seen = {payload.get("draft_message_id"), payload.get("edited_version")} - {None, ""}
            if draft is None:
                outcome = "already_gone"
            elif draft.message.id not in seen:
                outcome = "kept_edited"
            else:
                outcome = "deleted" if await client.delete_draft(draft.draft_id) else "already_gone"
    finally:
        _set_outcome(person.id, str(payload.get("message_id") or ""), DISMISSED, reason=outcome)
        _audit("delegation_reply_dismissed", f"Dismissed a reply card for person {person.id}", {
            "person_id": person.id, "decision_id": instance.id, "draft": outcome,
        })
    return outcome
