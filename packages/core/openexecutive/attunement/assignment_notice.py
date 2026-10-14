"""Tell a person, once, that they were assigned a task.

``assign_open_loop`` (the chat tool and the People page's "Assign a task"
form) used to store the loop silently: the owner first heard of it when the
nudge engine chased them after it fell due. Now, when someone assigns a task
to somebody else, the owner gets one short message on their own channel the
way the reminders reach people (``scheduler.runner.deliver_to_person``: their
Slack or Discord DM, their private Telegram chat, or email).

The text is fixed, never a model's: the assigner's name, the task in the
speaker's own words (the tool already requires that) and the due date. It is
the Executive's own outreach, so the owner's notification settings apply
(``people.notifications.hold_for``): quiet hours or "In my briefs" hold it in
``people.held_messages`` and it goes out later, on any channel. The send is
tagged with the loop's commitment scope, so the owner's "done" that closes the
loop also counts the notice as acted on.

Chasing stays the nudge engine's job; this sends one notice per loop and
never repeats. Returns how it went so the assigner can be told:
``sent`` (with the channel), ``held`` or ``not_reached``. Never raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from openexecutive.attunement.outcomes import SOURCE_TASK_ASSIGNED

logger = logging.getLogger(__name__)

# The email subject / message label.
LABEL = "New task"
# Matches the People form's limit on a task.
_TASK_MAX = 200

NoticeStatus = str  # "sent" | "held" | "not_reached" | "skipped"


@dataclass(frozen=True)
class NoticeResult:
    """How the notice went: ``status`` and, when sent, the channel."""

    status: NoticeStatus
    channel: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, str | None]:
        return {"status": self.status, "channel": self.channel}


def _due_phrase(due_at: str | None) -> str:
    """'Friday 17 October' in the user's timezone, or '' when unreadable."""
    if not due_at:
        return ""
    try:
        from openexecutive.attunement.open_loops import _user_tz

        dt = datetime.fromisoformat(due_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone(_user_tz())
    except (ValueError, TypeError):
        return ""
    return f"{local:%A} {local.day} {local:%B}"


def notice_text(*, assigner_name: str, task: str, due_at: str | None) -> str:
    """The message the owner gets."""
    due = _due_phrase(due_at)
    task = " ".join(task.split())[:_TASK_MAX]
    lines = [f'{assigner_name} asked you to take this on: "{task}".']
    if due:
        lines.append(f"It's due {due}.")
    lines.append("Reply here when it's done, or if you can't do it.")
    return " ".join(lines)


def _audit(owner_id: int, loop_id: int, result: NoticeResult) -> None:
    try:
        from openexecutive.audit import log_event

        log_event(
            "task_assignment_notice",
            f"Task #{loop_id} notice to person {owner_id}: {result.status}",
            actor="attunement",
            details={
                "loop_id": loop_id,
                "owner_person_id": owner_id,
                "status": result.status,
                "channel": result.channel,
                "detail": result.detail[:200],
            },
            private_to_person=owner_id,
        )
    except Exception:
        logger.warning("assignment_notice: audit failed", exc_info=True)


async def notify_assignee(
    *,
    loop_id: int,
    owner_person_id: int,
    assigned_by_person_id: int,
    task: str,
    due_at: str | None,
    now: datetime | None = None,
) -> NoticeResult:
    """Send the owner of loop ``loop_id`` one notice that it was assigned.

    Nothing is sent when the owner assigned it to themselves, or the notice is
    turned off (``ATTUNEMENT_ASSIGN_NOTIFY_ENABLED``)."""
    from openexecutive.config import get_settings

    if owner_person_id == assigned_by_person_id:
        return NoticeResult("skipped", detail="assigned to themselves")
    if not get_settings().attunement_assign_notify_enabled:
        return NoticeResult("skipped", detail="turned off")
    try:
        result = await _notify(
            loop_id=loop_id,
            owner_person_id=owner_person_id,
            assigned_by_person_id=assigned_by_person_id,
            task=task,
            due_at=due_at,
            now=now or datetime.now(UTC),
        )
    except Exception as exc:
        logger.warning("assignment_notice: loop %s notice failed", loop_id, exc_info=True)
        result = NoticeResult("not_reached", detail=type(exc).__name__)
    _audit(owner_person_id, loop_id, result)
    return result


async def _notify(
    *,
    loop_id: int,
    owner_person_id: int,
    assigned_by_person_id: int,
    task: str,
    due_at: str | None,
    now: datetime,
) -> NoticeResult:
    from openexecutive.attunement.outcomes import tag_proactive
    from openexecutive.audit.context import rows_for_person
    from openexecutive.people import held_messages
    from openexecutive.people.notifications import hold_for
    from openexecutive.people.store import get_person
    from openexecutive.scheduler.runner import deliver_to_person

    owner = get_person(owner_person_id)
    assigner = get_person(assigned_by_person_id)
    if owner is None or owner.id is None or owner.archived:
        return NoticeResult("not_reached", detail="owner not on the People list")
    assigner_name = assigner.full_name if assigner is not None else "Someone on your team"
    text = notice_text(assigner_name=assigner_name, task=task, due_at=due_at)
    ref = f"nudge:commitment:{loop_id}"

    hold = hold_for(owner.id, source=SOURCE_TASK_ASSIGNED, now=now)
    if hold is not None:
        held_messages.hold(
            owner.id, text, source=SOURCE_TASK_ASSIGNED, release_at=hold.release_at,
            ref=ref, now=now,
        )
        return NoticeResult("held", detail=hold.reason)

    # The DM handlers re-check the hold under this tag; the answer is the
    # same one hold_for just gave, so nothing is held twice.
    with tag_proactive(SOURCE_TASK_ASSIGNED, ref), rows_for_person(owner.id):
        delivery = await deliver_to_person(owner, text, label=LABEL)
    if delivery.ok:
        return NoticeResult("sent", channel=delivery.channel)
    return NoticeResult("not_reached", detail=delivery.reason)


__all__ = [
    "LABEL",
    "SOURCE_TASK_ASSIGNED",
    "NoticeResult",
    "notice_text",
    "notify_assignee",
]
