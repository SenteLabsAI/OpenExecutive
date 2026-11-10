"""How a person really answers: a reply time and a channel to suggest.

A person's "replies within" (``response_sla_hours``) and how to reach them
(``preferred_channel``) are typed by hand on their page. This reads how they
actually answered the Executive's own messages and, when that differs enough
from what is set, offers it as a suggestion the owner can take (**Use it**,
which writes the field as an edit would) or turn down (**Keep mine**).

The evidence is ``outbound_context``: every message the Executive sends a
rostered person on a chat app or by email is recorded there, and a reply
matched to it marks the row consumed. The reply came on the same channel
(linkage is per channel), so each consumed row gives one reply's channel and
how long it took. Replies to the owner's own mail are not recorded anywhere,
so they are not counted. No model call: plain arithmetic over stored rows.

Keep mine is remembered per person and kind with the value it turned down.
That suggestion stays hidden until the evidence moves materially: a reply
time at least twice as far from the kept-against one, or a different channel.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

Kind = Literal["reply_time", "channel"]
KINDS: tuple[Kind, ...] = ("reply_time", "channel")

# outbound_context.channel -> the person's preferred_channel value.
_CHANNELS: dict[str, str] = {
    "email": "email",
    "slack_dm": "slack",
    "telegram": "telegram",
    "discord_dm": "discord",
}
CHANNEL_LABELS: dict[str, str] = {
    "email": "Email",
    "slack": "Slack",
    "telegram": "Telegram",
    "discord": "Discord",
}
# What the person's page calls each setting, "any" included.
_SETTING_LABELS: dict[str, str] = {**CHANNEL_LABELS, "any": "Where they last talked"}

# The replies read: their latest, within this window.
WINDOW_DAYS = 90
MAX_REPLIES = 20
# Fewer replies than this and nothing is suggested.
MIN_REPLIES = 5
# A channel needs this many of the quick replies, and more than half of them.
MIN_CHANNEL_QUICK = 3
# A reply time is suggested only when it is at least this many times away
# from what is set (5h against 24h is; 18h against 24h is not).
RATIO = 2.0

TABLE = "reply_habit_kept"


@dataclass(frozen=True)
class Reply:
    channel: str  # a preferred_channel value: email, slack, telegram, discord
    hours: float


@dataclass(frozen=True)
class Suggestion:
    kind: Kind
    # reply_time: whole hours, as a string; channel: a preferred_channel value.
    value: str
    headline: str
    evidence: str


def _db(db_path: Path | None) -> Path:
    from openexecutive.memory.episodic import _resolve_db_path

    return _resolve_db_path(db_path)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608
        "person_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, "
        "kept_at TEXT NOT NULL, PRIMARY KEY (person_id, kind))"
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def recent_replies(
    person_id: int, *, now: datetime | None = None, db_path: Path | None = None
) -> list[Reply]:
    """Their latest replies to the Executive's messages, newest first.
    Never raises (no table yet reads as none)."""
    since = ((now or datetime.now(UTC)) - timedelta(days=WINDOW_DAYS)).isoformat()
    try:
        from openexecutive.memory.episodic import _get_conn

        resolved = _db(db_path)
        if not resolved.exists():
            return []
        with _get_conn(resolved) as conn:
            rows = conn.execute(
                "SELECT channel, created_at, consumed_at FROM outbound_context "
                "WHERE recipient_person_id = ? AND status = 'consumed' "
                "AND consumed_at IS NOT NULL AND created_at >= ? "
                "ORDER BY consumed_at DESC LIMIT ?",
                (person_id, since, MAX_REPLIES * 2),
            ).fetchall()
    except Exception:
        logger.warning("reply_habits: couldn't read replies", exc_info=True)
        return []
    out: list[Reply] = []
    for row in rows:
        channel = _CHANNELS.get(row["channel"])
        sent, answered = _parse(row["created_at"]), _parse(row["consumed_at"])
        if channel is None or sent is None or answered is None or answered < sent:
            continue
        out.append(Reply(channel=channel, hours=(answered - sent).total_seconds() / 3600))
        if len(out) >= MAX_REPLIES:
            break
    return out


def _round_hours(hours: float) -> int:
    """A reply time as a "replies within" value: whole hours under two days,
    whole days after."""
    if hours < 48:
        return max(1, round(hours))
    return max(2, round(hours / 24)) * 24


def describe_hours(hours: int) -> str:
    """"about an hour", "about 5 hours", "about 3 days"."""
    if hours <= 1:
        return "about an hour"
    if hours < 48:
        return f"about {hours} hours"
    return f"about {hours // 24} days"


def _short_hours(hours: int) -> str:
    return f"{hours // 24}d" if hours >= 48 and hours % 24 == 0 else f"{hours}h"


def _far(a: int, b: int) -> bool:
    low, high = sorted((max(a, 1), max(b, 1)))
    return high >= low * RATIO


def _kept(person_id: int, db_path: Path | None) -> dict[str, str]:
    try:
        from openexecutive.memory.episodic import _get_conn

        resolved = _db(db_path)
        if not resolved.exists():
            return {}
        with _get_conn(resolved) as conn:
            _ensure(conn)
            rows = conn.execute(
                f"SELECT kind, value FROM {TABLE} WHERE person_id = ?", (person_id,)  # noqa: S608
            ).fetchall()
        return {str(r["kind"]): str(r["value"]) for r in rows}
    except Exception:
        logger.warning("reply_habits: couldn't read what was kept", exc_info=True)
        return {}


def _reply_time(person: Person, replies: list[Reply], kept: str | None) -> Suggestion | None:
    if len(replies) < MIN_REPLIES:
        return None
    hours = _round_hours(statistics.median(r.hours for r in replies))
    if not _far(hours, person.response_sla_hours):
        return None
    if kept is not None and kept.isdigit() and not _far(hours, int(kept)):
        return None
    return Suggestion(
        kind="reply_time",
        value=str(hours),
        headline=f"Usually replies in {describe_hours(hours)}",
        evidence=(
            f"You have {_short_hours(person.response_sla_hours)} set. "
            f"From their last {len(replies)} replies to me."
        ),
    )


def _channel(person: Person, replies: list[Reply], kept: str | None) -> Suggestion | None:
    if len(replies) < MIN_REPLIES:
        return None
    median = statistics.median(r.hours for r in replies)
    quick = [r for r in replies if r.hours <= median]
    counts: dict[str, int] = {}
    for r in quick:
        counts[r.channel] = counts.get(r.channel, 0) + 1
    best, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if n < MIN_CHANNEL_QUICK or n * 2 <= len(quick):
        return None
    if best == person.preferred_channel or best == kept:
        return None
    label = CHANNEL_LABELS[best]
    have = (
        f"You have {_SETTING_LABELS[person.preferred_channel]} set."
        if person.preferred_channel in _SETTING_LABELS else "You have no channel set."
    )
    return Suggestion(
        kind="channel",
        value=best,
        headline=f"Answers fastest on {label}",
        evidence=f"{have} {n} of their last {len(quick)} quick replies were on {label}.",
    )


def suggestions(
    person: Person, *, now: datetime | None = None, db_path: Path | None = None
) -> list[Suggestion]:
    """What to suggest for ``person``: up to one reply time and one
    channel, each only with enough replies behind it and when it differs
    enough from what is set (and from what was last kept). Never raises."""
    if person.id is None:
        return []
    replies = recent_replies(person.id, now=now, db_path=db_path)
    if len(replies) < MIN_REPLIES:
        return []
    kept = _kept(person.id, db_path)
    out = [
        _reply_time(person, replies, kept.get("reply_time")),
        _channel(person, replies, kept.get("channel")),
    ]
    return [s for s in out if s is not None]


def keep_mine(person: Person, kind: Kind, *, db_path: Path | None = None) -> bool:
    """Turn down today's ``kind`` suggestion for ``person``: it stays
    hidden until the evidence moves materially. False when there was
    nothing to turn down."""
    current = next((s for s in suggestions(person, db_path=db_path) if s.kind == kind), None)
    if current is None or person.id is None:
        return False
    from openexecutive.memory.episodic import _get_conn

    with _get_conn(_db(db_path)) as conn:
        _ensure(conn)
        conn.execute(
            f"INSERT INTO {TABLE} (person_id, kind, value, kept_at) VALUES (?, ?, ?, ?) "  # noqa: S608
            "ON CONFLICT(person_id, kind) DO UPDATE SET value = excluded.value, kept_at = excluded.kept_at",
            (person.id, kind, current.value, datetime.now(UTC).isoformat()),
        )
    return True
