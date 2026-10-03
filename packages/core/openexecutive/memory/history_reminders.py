"""Always in the loop reminders: on the day something a person promised by
email falls due, by their own notes (``memory.history``), one short message
to them alone says so.

Only notes from replies they sent with Act as me
(``history.SOURCE_APPROVED_REPLY``): a promise they made in chat is already
an open loop the nudge engine chases when due (``attunement.open_loops``), so
reminding of it here too would say it twice.

The message names nothing from the notes, only how many things are due: it
travels through the ordinary channel senders, whose audit and activity rows
others may read. The person asks for the detail in a conversation only they
can read, where ``recall_history`` answers (``orchestrator.history_tools``).
It goes to their own DM, Telegram chat or email address
(``scheduler.runner.deliver_to_person``), never a shared channel, at most once
a day, during the workspace's daytime.

The scheduler tick calls :func:`maybe_remind`. Never raises; logs codes,
never text.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

REMIND_KINDS: tuple[str, ...] = ("promised", "agreed")
# The workspace's local hours a reminder may go out in.
FIRST_HOUR = 9
LAST_HOUR = 18
SCAN_EVERY = timedelta(minutes=15)

_last_scan_at: datetime | None = None
_task: asyncio.Task[int] | None = None


def reminder_text(count: int, today: date) -> str:
    """The message: how many, never what."""
    things = "one thing" if count == 1 else f"{count} things"
    return (
        f"From your notes: {things} you said you'd do by email {'is' if count == 1 else 'are'} "
        f"due today ({today:%a} {today.day} {today:%b}). Ask me \"what's due today?\" in the "
        "web app or a direct message and I'll go through them."
    )


def _in_hours(now: datetime) -> bool:
    from openexecutive.memory.workspace_settings import get_user_timezone

    return FIRST_HOUR <= now.astimezone(get_user_timezone()).hour < LAST_HOUR


async def remind_due(now: datetime) -> int:
    """Remind each person who keeps notes of today's due promises, once a
    day. Returns how many were reminded. Never raises."""
    from openexecutive.audit import log_event
    from openexecutive.memory import history
    from openexecutive.memory.history_brief import local_today
    from openexecutive.people.store import get_person
    from openexecutive.scheduler.runner import deliver_to_person

    today = local_today(now)
    day = today.isoformat()
    sent = 0
    for person_id in history.people_keeping_notes():
        try:
            person = get_person(person_id)
            if person is None or not history.can_keep_notes(person) or history.reminded(person_id, day):
                continue
            due = history.due_notes(
                person_id, start=today, end=today, kinds=REMIND_KINDS,
                sources=(history.SOURCE_APPROVED_REPLY,), now=now,
            )
            # Claimed before sending, so two workers never both send it.
            if not due or not history.mark_reminded(person_id, day, now=now):
                continue
            delivery = await deliver_to_person(person, reminder_text(len(due), today), label="Due today")
        except Exception:
            logger.warning("history: reminding person %s failed", person_id, exc_info=True)
            continue
        if not delivery.ok:
            logger.info("history: reminder for person %s not sent (%s)", person_id, delivery.reason)
            if delivery.reason == "send_failed":
                # A channel that may work later: try again on a later tick.
                history.unmark_reminded(person_id, day)
            continue
        sent += 1
        log_event(
            "history_reminder_sent", f"Reminded person {person_id} of {len(due)} noted promise(s) due today",
            actor="scheduler",
            details={"person_id": person_id, "count": len(due), "channel": delivery.channel},
            private=True, private_to_person=person_id,
        )
    return sent


def maybe_remind(now: datetime) -> bool:
    """Start a reminder pass when the interval has passed, it is daytime
    locally and none is running. True when one started. Never raises."""
    global _last_scan_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_scan_at is not None and now - _last_scan_at < SCAN_EVERY:
            return False
        if not _in_hours(now):
            return False
        _last_scan_at = now
        from openexecutive.audit.context import unscoped_audit_rows

        with unscoped_audit_rows():
            _task = asyncio.create_task(remind_due(now))
        return True
    except Exception:
        logger.exception("history: couldn't start reminders")
        return False


__all__ = ["maybe_remind", "remind_due", "reminder_text"]
