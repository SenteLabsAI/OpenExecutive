"""A card in Needs you when an email to the Executive got no reply.

Mail from the team, or from the principal, reaches the Executive's mailbox
and is answered by the model calling the mailbox's send tool. When the turn
ends without that send (a tool refused, the model only wrote its answer, a
reply that went to someone else), nobody heard: the sender waits, the
morning brief and the Home header read the inbound row and say they are
waiting, and Needs you, which shows alerts only, stays empty. Nothing raised
one, since an alert depends on the model choosing ``create_alert``.

:func:`file_card` files one card for the principal instead, whatever the
model did; a repeat in the same thread folds into it (the occurrence count),
and :func:`close_card` resolves it once a later turn does reply there.

The card is private to the principal (``PRIVATE_ALERT_TAG``, routed to them):
only their own /today shows it. On their own mail it carries what the
Executive wrote, which never reached them. A teammate's own conversation
with the Executive is theirs alone (``people_tools.teammate_turn_owner``),
so their card names only who is waiting.

Never raises: a card left unfiled costs the card, never the email.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SOURCE = "unanswered_email"
_ANSWER_MAX = 1500
_SUBJECT_MAX = 120


def _dedup_key(thread_key: str) -> str:
    # The `private:` prefix keeps it apart from shared cards, as the
    # pipeline's private alerts are (alerts.pipeline._make_private).
    return f"private:{SOURCE}:{thread_key.strip().lower()}"


def _principal_id() -> int | None:
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return principal.id if principal is not None else None


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def file_card(
    *,
    thread_key: str,
    message_id: str,
    sender_name: str,
    from_principal: bool,
    subject: str = "",
    answer: str = "",
) -> int | None:
    """File (or bump) the card for an email whose sender got no reply.

    ``thread_key`` tells repeats apart (the mailbox thread id, else the
    sender's address). ``subject`` is left out of the card when empty;
    ``answer`` (what the Executive wrote) is kept only on the principal's own
    mail. Returns the card's id, or None when nothing was filed."""
    from openexecutive.alerts import store
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG

    topic = _one_line(subject, _SUBJECT_MAX)
    about = f" about “{topic}”" if topic else ""
    if from_principal:
        headline = f"Your email got no reply{': ' + topic if topic else ''}"
        written = answer.strip()
        if len(written) > _ANSWER_MAX:
            written = written[: _ANSWER_MAX - 1].rstrip() + "…"
        body = (
            f"The Executive worked on your email{about} but its reply never went out."
            + (f" What it wrote:\n\n{written}" if written else "")
        )
        action = "Read what it wrote, or ask it again."
    else:
        name = _one_line(sender_name, 80) or "A teammate"
        headline = f"{name} is waiting on a reply{': ' + topic if topic else ''}"
        body = (
            f"{name} emailed the Executive{about} and no reply went out. "
            f"Reply to {name} yourself, or ask the Executive about it."
        )
        action = f"Reply to {name}."
    dedup_key = _dedup_key(thread_key)
    try:
        hit = store.coalesce_alert(
            source=SOURCE, dedup_key=dedup_key, severity="high", body=body
        )
        if hit is not None:
            return hit[0]
        return store.insert_alert(
            source=SOURCE,
            external_id=f"message:{message_id}" if message_id else f"thread:{thread_key}",
            severity="high",
            headline=headline,
            body=body,
            suggested_action=action,
            topic_tags=["email", PRIVATE_ALERT_TAG],
            dedup_key=dedup_key,
            routed_to_person_id=_principal_id(),
        )
    except Exception:
        logger.exception("unanswered_mail: could not file the card for thread %s", thread_key)
        return None


def close_card(thread_key: str) -> None:
    """Resolve the open card for a thread that has now had its reply."""
    from openexecutive.alerts import store
    from openexecutive.alerts.lifecycle import RESOLVED_STATUS

    try:
        with store._get_conn() as conn:
            conn.execute(
                "UPDATE alerts SET status = ? "
                "WHERE source = ? AND dedup_key = ? AND status = 'unread'",
                (RESOLVED_STATUS, SOURCE, _dedup_key(thread_key)),
            )
    except Exception:
        logger.exception("unanswered_mail: could not close the card for thread %s", thread_key)
