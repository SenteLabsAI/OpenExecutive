"""Each person's own notification settings: quiet hours, digest or instant,
and whether urgent things get through.

Set by the person themselves on Settings → Notifications
(``api/routes/notifications.py``); one row per person in
``person_notification_settings``. A person with no row gets the defaults,
which are today's behaviour: no quiet hours, messages as they happen, urgent
things through.

What the settings hold back is the Executive's *own* outreach on the chat
channels — a Slack, Discord or Telegram DM it sends while a proactive source
is tagged (``attunement.outcomes.tag_proactive``: nudges the scheduler
dispatches, the daily reflection, research findings, alert review). A message
someone asked for in chat, a follow-up someone scheduled, the briefs
themselves and reminders a person set are never held. Mail is not held
either: the mail send tool does not go through the DM send handlers, so an
email nudge is only deferred past quiet hours by nudge routing, never held
for a brief. Two places apply them:

- **Nudge routing** (``people.channel`` with ``quiet_hours=True``): the nudge
  engine and the scheduler's ``proactive_nudge`` dispatch defer the nudge to
  the end of the person's quiet hours, the same way they defer to an
  availability window, so nothing is written at 3am.
- **The outbound guard** (``orchestrator.outbound_guard.check_hold``): a
  proactive DM to someone in quiet hours, or to someone who chose "In my
  briefs", is stored in ``held_messages`` instead of sent, and goes out when
  quiet hours end or with their next brief (``people.held_messages``).

"Let urgent things through" (on by default) exempts approval nudges and
critical alerts: callers mark those with :func:`urgent_send`. A question a
workflow is waiting on (:func:`approval_send`) is never held at all, whatever
the setting: the gate can only be answered by a reply to it.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

TABLE = "person_notification_settings"

QuietDays = Literal["every_day", "weekdays"]
Delivery = Literal["instant", "digest"]
# When Right now changes: a short chat message for a new point that needs
# them, for any new point, or never (``briefing.right_now_ping``).
RightNowPings = Literal["off", "important", "everything"]

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class QuietHours(BaseModel):
    """A nightly quiet window in the person's zone. ``start`` may be later
    than ``end`` (22:00 → 07:00 crosses midnight). ``weekdays`` keeps the
    window on weekday nights and makes Saturday and Sunday quiet all day."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    start: str = Field(default="22:00", pattern=_HHMM)
    end: str = Field(default="07:00", pattern=_HHMM)
    days: QuietDays = "every_day"

    @model_validator(mode="after")
    def _not_empty(self) -> QuietHours:
        if self.start == self.end:
            raise ValueError("quiet hours must start and end at different times")
        return self


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    delivery: Delivery = "instant"
    urgent_through: bool = True
    right_now: RightNowPings = "important"
    # IANA zone the quiet hours are read in; "" means the workspace's zone.
    timezone: str = Field(default="", max_length=64)
    updated_at: str = ""

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        from openexecutive.memory.workspace_settings import load_zone

        value = (value or "").strip()
        if value and load_zone(value) is None:
            raise ValueError(f"unknown time zone {value!r}")
        return value


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 — constant table name
        "person_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def get_notification_settings(
    person_id: int | None, *, db_path: Path | None = None
) -> NotificationSettings:
    """The person's settings, or the defaults when they have none or the row
    can't be read (fail open: the defaults hold nothing back)."""
    if person_id is None:
        return NotificationSettings()
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT settings_json, updated_at FROM {TABLE} WHERE person_id = ?",  # noqa: S608
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("notifications: couldn't read settings for person %s", person_id, exc_info=True)
        return NotificationSettings()
    if row is None:
        return NotificationSettings()
    try:
        data = json.loads(row[0])
        data["updated_at"] = str(row[1])
        return NotificationSettings.model_validate(data)
    except Exception:
        logger.warning("notifications: malformed settings for person %s", person_id)
        return NotificationSettings()


def save_notification_settings(
    person_id: int, settings: NotificationSettings, *, db_path: Path | None = None
) -> NotificationSettings:
    """Replace the person's settings. Raises on a write error."""
    now = datetime.now(UTC).isoformat()
    payload = settings.model_dump(exclude={"updated_at"})
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {TABLE} (person_id, settings_json, updated_at) VALUES (?, ?, ?) "  # noqa: S608
            "ON CONFLICT(person_id) DO UPDATE SET settings_json = excluded.settings_json, "
            "updated_at = excluded.updated_at",
            (person_id, json.dumps(payload), now),
        )
        conn.commit()
    finally:
        conn.close()
    return settings.model_copy(update={"updated_at": now})


# --------------------------------------------------------------------------- #
# Quiet hours
# --------------------------------------------------------------------------- #

def zone_for(settings: NotificationSettings) -> tzinfo:
    """The zone the settings are read in: theirs, else the workspace's."""
    from openexecutive.memory.workspace_settings import get_user_timezone, load_zone

    if settings.timezone:
        zone = load_zone(settings.timezone)
        if zone is not None:
            return zone
    return get_user_timezone()


def _minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def in_quiet_hours(settings: NotificationSettings, now: datetime, *, zone: tzinfo | None = None) -> bool:
    """True while ``now`` falls in the settings' quiet hours."""
    quiet = settings.quiet_hours
    if not quiet.enabled:
        return False
    local = now.astimezone(zone or zone_for(settings))
    if quiet.days == "weekdays" and local.weekday() >= 5:
        return True
    start, end = _minutes(quiet.start), _minutes(quiet.end)
    minute = local.hour * 60 + local.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def quiet_hours_end(settings: NotificationSettings, after: datetime) -> datetime | None:
    """The first moment after ``after`` that is outside quiet hours (UTC), or
    None when quiet hours are off. Quiet hours only ever end at their ``end``
    time or at midnight (a weekend ending), so those are the only candidates."""
    if not settings.quiet_hours.enabled:
        return None
    zone = zone_for(settings)
    if not in_quiet_hours(settings, after, zone=zone):
        return after.astimezone(UTC)
    local = after.astimezone(zone)
    end_h, end_m = (int(p) for p in settings.quiet_hours.end.split(":"))
    candidates: list[datetime] = []
    for offset in range(0, 4):
        day = (local + timedelta(days=offset)).date()
        for hh, mm in ((end_h, end_m), (0, 0)):
            candidate = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone)
            if candidate > local:
                candidates.append(candidate)
    for candidate in sorted(candidates):
        if not in_quiet_hours(settings, candidate, zone=zone):
            return candidate.astimezone(UTC)
    return None


# --------------------------------------------------------------------------- #
# Urgency
# --------------------------------------------------------------------------- #

# None, "urgent" (let through when they let urgent things through) or
# "approval" (never held).
_urgent: contextvars.ContextVar[str | None] = contextvars.ContextVar("notifications_urgent", default=None)


@contextlib.contextmanager
def urgent_send() -> Iterator[None]:
    """Mark the DMs sent inside the block as urgent (an approval nudge, or a
    critical alert): "Let urgent things through" lets them past quiet hours
    and the digest. Wrap only non-yielding code."""
    token = _urgent.set("urgent")
    try:
        yield
    finally:
        _urgent.reset(token)


@contextlib.contextmanager
def approval_send() -> Iterator[None]:
    """Mark the DMs sent inside the block as a question something is waiting
    on (a workflow gate): never held, whatever the person's settings, since
    only a reply to it resolves the wait. Wrap only non-yielding code."""
    token = _urgent.set("approval")
    try:
        yield
    finally:
        _urgent.reset(token)


def is_urgent_send() -> bool:
    return _urgent.get() is not None


def is_approval_send() -> bool:
    return _urgent.get() == "approval"


# Proactive sources that are always urgent: a nudge about something waiting
# on the person's approval (attunement.outcomes.SOURCE_NUDGE_STALLED).
URGENT_SOURCES = frozenset({"nudge_stalled"})
# Sources never held: a follow-up runs at the time someone asked for, so
# neither quiet hours nor a brief may move it.
NEVER_HELD_SOURCES = frozenset({"followup"})


def quiet_applies(person_id: int | None, *, urgent: bool = False) -> NotificationSettings | None:
    """The person's settings when their quiet hours apply to a message of this
    urgency, else None. Never raises."""
    if person_id is None:
        return None
    try:
        settings = get_notification_settings(person_id)
    except Exception:
        return None
    if not settings.quiet_hours.enabled:
        return None
    if urgent and settings.urgent_through:
        return None
    return settings


@dataclass(frozen=True)
class Hold:
    """Why a proactive DM waits, and until when (None: their next brief)."""

    release_at: datetime | None
    reason: str


def hold_for(person_id: int, *, source: str, now: datetime, urgent: bool = False) -> Hold | None:
    """Whether a proactive DM from ``source`` to ``person_id`` should wait,
    by the person's own settings. None means send it now."""
    if source in NEVER_HELD_SOURCES:
        return None
    settings = get_notification_settings(person_id)
    urgent = urgent or source in URGENT_SOURCES
    if urgent and settings.urgent_through:
        return None
    if settings.delivery == "digest":
        return Hold(None, "they asked to get these in their brief; it will be in their next one")
    if in_quiet_hours(settings, now):
        end = quiet_hours_end(settings, now)
        when = end.isoformat() if end is not None else "the end of their quiet hours"
        return Hold(end, f"they have quiet hours on; it will reach them at {when}")
    return None


__all__ = [
    "NEVER_HELD_SOURCES",
    "URGENT_SOURCES",
    "Hold",
    "NotificationSettings",
    "QuietHours",
    "get_notification_settings",
    "hold_for",
    "in_quiet_hours",
    "approval_send",
    "is_approval_send",
    "is_urgent_send",
    "quiet_applies",
    "quiet_hours_end",
    "save_notification_settings",
    "urgent_send",
    "zone_for",
]
