"""``remind_me``: a reminder for the person the Executive is speaking with,
sent to them alone as plain text when it is due (``delegation.reminders``).

It rides with Act as me's tools (``delegation_tools.DELEGATION_TOOLS``), so it
is offered and re-checked the same way (``_writer``: the pin and the verified
surface), but it opens no mailbox, so calling it never marks a turn as having
read mail. It stays on in a conversation that read mail, unlike
``schedule_followup``: what it stores is cleaned, fixed text, it reaches only
the person who set it, and nothing runs when it fires.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

logger = logging.getLogger(__name__)

REMIND_ME = "remind_me"
REMINDERS_PER_TURN = 5
_DEFAULT_HOUR = time(9, 0)

REMIND_ME_TOOL: dict[str, Any] = {
    "name": REMIND_ME,
    "description": (
        "Set a reminder for the person you are speaking with: at `when` they get "
        "`text` from you as a message on their own channel (Slack, Discord, "
        "Telegram or email). It goes to them alone, exactly as written, with links "
        "and addresses taken out. Use it for \"remind me Friday to reply to Dana\". "
        "Write `text` as what they should do, in a line. `when` is a local date and "
        "time, e.g. 2026-10-10T09:00 (a date alone means 9:00)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind them of, in one line."},
            "when": {"type": "string", "description": "Local date and time, e.g. 2026-10-10T09:00."},
        },
        "required": ["text", "when"],
    },
}


def _error(message: str) -> str:
    return json.dumps({"error": message})


def parse_when(value: str, now: datetime) -> datetime | str:
    """``value`` as an aware time, read in the workspace's timezone when it
    has none, or why it can't be one."""
    from openexecutive.delegation.reminders import HORIZON
    from openexecutive.memory.workspace_settings import get_user_timezone

    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return "`when` must be a date and time like 2026-10-10T09:00."
    if len(raw) == 10:  # a date alone
        parsed = datetime.combine(parsed.date(), _DEFAULT_HOUR)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_user_timezone())
    if parsed <= now + timedelta(minutes=1):
        return "That time has passed. Ask them when they want it."
    if parsed > now + HORIZON:
        return "That is more than a year away; pick a nearer date."
    return parsed


async def handle_remind_me(tool_input: dict[str, Any]) -> str:
    from openexecutive.audit import log_event
    from openexecutive.delegation import reminders
    from openexecutive.orchestrator.delegation_tools import _writer
    from openexecutive.scheduler.runner import delivery_order, email_ready

    writer = _writer(REMIND_ME)
    if isinstance(writer, str):
        return writer
    now = datetime.now(UTC)
    when = parse_when(str(tool_input.get("when") or ""), now)
    if isinstance(when, str):
        return _error(when)
    text = reminders.clean_text(str(tool_input.get("text") or ""))
    if not text or text == "[link]":
        return _error("Pass `text`: what to remind them of, in words.")
    if not delivery_order(writer.person, email_ready=email_ready()):
        return _error("I have no way to reach them on their own (no DM or email set up), so I can't remind them.")
    pinned = writer.pinned
    if getattr(pinned, "reminders", 0) >= REMINDERS_PER_TURN:
        return _error(f"That's {REMINDERS_PER_TURN} reminders this turn. Ask them before setting more.")
    try:
        if reminders.pending_count(writer.person.id) >= reminders.MAX_PENDING:
            return _error(f"They already have {reminders.MAX_PENDING} reminders waiting. Let some go out first.")
    except Exception:
        logger.warning("remind_me: couldn't count reminders", exc_info=True)
        return _error("Couldn't check their reminders just now. Try again in a moment.")
    pinned.reminders = getattr(pinned, "reminders", 0) + 1
    try:
        reminder_id = reminders.add(writer.person.id, text, when, now=now)
    except Exception:
        pinned.reminders -= 1
        logger.warning("remind_me: couldn't store the reminder", exc_info=True)
        return _error("Couldn't save the reminder just now. Try again in a moment.")
    log_event(
        "delegation_reminder_set", f"Set a reminder for person {writer.person.id}",
        actor="executive",
        details={"person_id": writer.person.id, "reminder_id": reminder_id, "due_at": when.isoformat()},
        private=True, private_to_person=writer.person.id,
    )
    return json.dumps({
        "status": "set",
        "when": when.strftime("%a %d %b %Y, %H:%M %Z").strip(),
        "text": text,
        "note": "It goes to them alone, as this text, at that time.",
    })


WATCH_FOR_EMAIL = "watch_for_email"

WATCH_FOR_EMAIL_TOOL: dict[str, Any] = {
    "name": WATCH_FOR_EMAIL,
    "description": (
        "Watch the person's own mailbox for an email they are waiting on, and tell "
        "them on their own channel the moment it lands (sender name and subject, "
        "never the email's text). Use it for \"let me know when Dana's email comes "
        "in\" instead of a reminder or a scheduled check. `from` is the sender's "
        "email address: look it up first (their contacts or past mail) if you only "
        "have a name. `about` is an optional word the subject must contain. It "
        "stops after the first match or after `days` (default 7), and tells them "
        "if nothing came."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "The sender's email address."},
            "about": {"type": "string", "description": "Optional word the subject must contain."},
            "days": {"type": "integer", "description": "How many days to watch, 1-30 (default 7)."},
        },
        "required": ["from"],
    },
}


async def handle_watch_for_email(tool_input: dict[str, Any]) -> str:
    from openexecutive.audit import log_event
    from openexecutive.delegation import mail_watch
    from openexecutive.delegation.gmail import (
        _EMAIL_RE,
        STATUS_MESSAGES,
        gmail_status,
        normalize_email,
    )
    from openexecutive.orchestrator.delegation_tools import NOT_CONNECTED_NOTE, _writer
    from openexecutive.scheduler.runner import delivery_order, email_ready

    writer = _writer(WATCH_FOR_EMAIL)
    if isinstance(writer, str):
        return writer
    sender = normalize_email(str(tool_input.get("from") or ""))
    if not _EMAIL_RE.fullmatch(sender):
        return _error("Pass `from` as the sender's email address. Look it up first if you only have a name.")
    if sender == writer.email:
        return _error("That is their own address. Ask who they are waiting on.")
    try:
        days = int(tool_input.get("days") or mail_watch.DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = mail_watch.DEFAULT_DAYS
    days = max(1, min(days, mail_watch.MAX_DAYS))
    about = mail_watch.clean_about(str(tool_input.get("about") or ""))
    if not delivery_order(writer.person, email_ready=email_ready()):
        return _error("I have no way to reach them on their own (no DM or email set up), so I can't tell them when it lands.")
    # Only whether the mailbox can be read: nothing in it is opened on this
    # turn, so setting a watch never counts as having read their mail.
    status = await gmail_status(writer.email, gmail=writer.mailbox)
    if status != "connected":
        return json.dumps({"error": STATUS_MESSAGES[status], "status": status, "note": NOT_CONNECTED_NOTE})
    try:
        if mail_watch.pending_count(writer.person.id) >= mail_watch.MAX_PENDING:
            return _error(f"They already have {mail_watch.MAX_PENDING} email watches open. Let some finish first.")
        now = datetime.now(UTC)
        watch_id = mail_watch.add(
            writer.person.id, sender, about, now + timedelta(days=days), now=now,
        )
    except Exception:
        logger.warning("watch_for_email: couldn't store the watch", exc_info=True)
        return _error("Couldn't set the watch just now. Try again in a moment.")
    log_event(
        "delegation_mail_watch_set", f"Set an email watch for person {writer.person.id}",
        actor="executive",
        details={"person_id": writer.person.id, "watch_id": watch_id, "days": days},
        private=True, private_to_person=writer.person.id,
    )
    return json.dumps({
        "status": "watching",
        "from": sender,
        "about": about,
        "days": days,
        "note": (
            f"Their mailbox is checked every {int(mail_watch.CHECK_EVERY.total_seconds() // 60)} "
            "minutes; they get one message on their own channel when it lands, or after "
            f"{days} days if it doesn't."
        ),
    })


REMINDER_TOOLS: list[dict[str, Any]] = [REMIND_ME_TOOL, WATCH_FOR_EMAIL_TOOL]
REMINDER_TOOL_HANDLERS: dict[str, Any] = {
    REMIND_ME: handle_remind_me,
    WATCH_FOR_EMAIL: handle_watch_for_email,
}
