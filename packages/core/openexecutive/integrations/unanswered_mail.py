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
Executive wrote, which never reached them, but only when the mailbox proved
the mail is theirs. A teammate's card names only who is waiting: their own
conversation with the Executive is theirs alone, and an unproven mail's
text is whoever sent it. An unproven mail says so on its card.

Never raises: a card left unfiled costs the card, never the email.
"""
from __future__ import annotations

import logging
import sqlite3

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
    verified: bool = True,
    principal_address: bool = False,
    subject: str = "",
    answer: str = "",
) -> int | None:
    """File (or bump) the card for an email whose sender got no reply.

    ``thread_key`` tells repeats apart (the mailbox thread id, else the
    sender's address). ``from_principal`` only for mail proven to be the
    principal's; ``subject`` and ``answer`` (what the Executive wrote) are
    kept only then. ``verified`` False says the mail only claims to be from
    ``sender_name``. Returns the card's id, or None when nothing was filed."""
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
    elif verified:
        name = _one_line(sender_name, 80) or "A teammate"
        headline = f"{name} is waiting on a reply"
        body = (
            f"{name} emailed the Executive and no reply went out. "
            f"Reply to {name} yourself, or ask the Executive about it."
        )
        action = f"Reply to {name}."
    elif principal_address:
        headline = "An email from your address got no reply"
        body = (
            "An email from your address reached the Executive and no reply went out. "
            "The mailbox could not prove you sent it. If you did not, someone may be "
            "using your address."
        )
        action = "Check that you sent it."
    else:
        name = _one_line(sender_name, 80) or "someone on the team"
        headline = f"An email from {name}'s address got no reply"
        body = (
            f"An email from {name}'s address reached the Executive and no reply went out. "
            "The mailbox could not prove they sent it, so check with them before acting on it."
        )
        action = f"Check with {name}."
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


def open_thread_keys() -> set[str]:
    """The threads (lowercased ``thread_key``) whose card is still open: mail
    whose sender has had no reply yet. Empty on any failure."""
    from openexecutive.alerts import store

    prefix = _dedup_key("")
    if not store._resolve_db_path(None).exists():
        return set()  # connecting would create an empty database
    try:
        with store._get_conn() as conn:
            rows = conn.execute(
                "SELECT dedup_key FROM alerts WHERE source = ? AND status = 'unread'",
                (SOURCE,),
            ).fetchall()
    except sqlite3.OperationalError:
        return set()  # no alerts table yet: no cards
    except Exception:
        logger.exception("unanswered_mail: could not read the open cards")
        return set()
    return {
        str(row[0])[len(prefix):] for row in rows
        if str(row[0] or "").startswith(prefix)
    }


REPLIED_SUMMARY = "Replied in the mailbox thread to "


def record_reply(thread_key: str, sender: str, session_id: str | None) -> None:
    """Log that a mailbox email got its reply: the positive evidence the
    briefs read (``briefing.live_signals``) before saying "the Executive
    replied". No card is no proof: a turn that timed out files none, and a
    card read or dismissed is no longer open."""
    from openexecutive.audit import log_event

    try:
        log_event(
            "integration_outbound",
            f"{REPLIED_SUMMARY}{_one_line(sender, 120)}",
            actor="email",
            session_id=session_id,
            details={"channel": "email", "to": sender, "thread_key": thread_key.strip().lower()},
            # Read only by the principal's own brief; names who was answered.
            private=True,
        )
    except Exception:
        logger.exception("unanswered_mail: could not log the reply in thread %s", thread_key)


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
