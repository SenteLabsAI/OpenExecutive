"""Right now: the /today header as Home's card, with when each point arrived.

The header (``narrative_cache``) is rewritten through the day, so a single
"today's brief" no longer describes it. Home shows it as **Right now**: the
bold opener, the points with the time each one first appeared, and how many
are new since the reader last looked. This module holds the two small pieces
of state that needs, next to the header's own table:

- ``briefing_point_seen``: per scope and local day, when a point (keyed by its
  bold subject) first appeared in a written header. Recorded when a header is
  written, so a point keeps its time across rewrites that reword it.
- ``briefing_reader_seen``: per scope, when the reader last looked at it.

``split_narrative`` parses the header's fixed shape (bold opener, bullets, a
``**Move today:**`` line) for the card; text in any other shape comes back as
the opener alone. Nothing here calls a model.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from openexecutive.briefing import narrative_cache

logger = logging.getLogger(__name__)

_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*\S)\s*$")
_MOVE_RE = re.compile(r"^\s*\*\*\s*Move today:?\s*\*\*:?\s*(.*\S)?\s*$", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_KEY_STRIP_RE = re.compile(r"[\W_]+")
# A point with no bold subject is keyed by its opening words.
_KEY_FALLBACK_CHARS = 60


@dataclass
class NarrativeParts:
    lead: str | None = None
    points: list[str] = field(default_factory=list)
    move: str | None = None


def _strip_bold(text: str) -> str:
    return _BOLD_RE.sub(r"\1", text).strip()


def split_narrative(text: str | None) -> NarrativeParts:
    """The header's opener, bullets and Move today line. Never raises."""
    parts = NarrativeParts()
    if not text or not text.strip():
        return parts
    loose: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        move = _MOVE_RE.match(line)
        if move:
            parts.move = (move.group(1) or "").strip() or None
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            parts.points.append(bullet.group(1))
            continue
        if parts.lead is None and not parts.points:
            parts.lead = _strip_bold(line)
        else:
            loose.append(line)
    # A line that is neither the opener, a bullet nor Move today (a model
    # that drifted from the shape) still reaches the reader, as a point.
    parts.points.extend(loose)
    return parts


def point_key(point: str) -> str:
    """Stable key for a point across rewrites: its bold subject, else its
    opening words, casefolded with punctuation dropped (any script)."""
    bold = _BOLD_RE.search(point)
    basis = bold.group(1) if bold else point[:_KEY_FALLBACK_CHARS]
    return _KEY_STRIP_RE.sub(" ", basis.casefold()).strip()


def initialize_db() -> None:
    with narrative_cache._get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_point_seen (
                scope TEXT NOT NULL,
                day TEXT NOT NULL,
                point_key TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                PRIMARY KEY (scope, day, point_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_reader_seen (
                scope TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
            """
        )


def _day_of(iso: str) -> str | None:
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return narrative_cache.local_today(when)


@dataclass
class Recorded:
    """What a written header added today: the points (as written) whose key
    was not recorded before, and whether an earlier header today had any."""

    new: list[str] = field(default_factory=list)
    earlier_today: bool = False


def record_points(scope: str, text: str, written_at: str) -> Recorded:
    """Note when each of this header's points first appeared today. A point
    already recorded keeps its earlier time. Returns what was new (for
    ``briefing.right_now_ping``). Never raises."""
    day = _day_of(written_at)
    if day is None:
        return Recorded()
    by_key: dict[str, str] = {}
    for p in split_narrative(text).points:
        k = point_key(p)
        if k and k not in by_key:
            by_key[k] = p
    if not by_key:
        return Recorded()
    try:
        initialize_db()
        with narrative_cache._get_conn() as conn:
            known = {
                r["point_key"] for r in conn.execute(
                    "SELECT point_key FROM briefing_point_seen WHERE scope = ? AND day = ?", (scope, day),
                ).fetchall()
            }
            conn.executemany(
                "INSERT OR IGNORE INTO briefing_point_seen "
                "(scope, day, point_key, first_seen) VALUES (?, ?, ?, ?)",
                [(scope, day, k, written_at) for k in sorted(by_key)],
            )
            # Times are only read for the local today; earlier days go.
            conn.execute("DELETE FROM briefing_point_seen WHERE day < ?", (day,))
    except Exception:
        logger.exception("right_now: recording points failed (scope=%s)", scope)
        return Recorded()
    return Recorded(new=[p for k, p in by_key.items() if k not in known], earlier_today=bool(known))


def point_times(scope: str, day: str) -> dict[str, str]:
    """``point_key`` → when it first appeared, for one scope and local day."""
    try:
        initialize_db()
        with narrative_cache._get_conn() as conn:
            rows = conn.execute(
                "SELECT point_key, first_seen FROM briefing_point_seen "
                "WHERE scope = ? AND day = ?",
                (scope, day),
            ).fetchall()
    except Exception:
        logger.exception("right_now: reading point times failed (scope=%s)", scope)
        return {}
    return {r["point_key"]: r["first_seen"] for r in rows}


def mark_seen(scope: str, at: str | None = None) -> str | None:
    """Record that the reader looked at Right now; returns the time stored,
    or None when it could not be written. Never raises."""
    at = at or narrative_cache.utc_now_iso()
    try:
        initialize_db()
        with narrative_cache._get_conn() as conn:
            conn.execute(
                "INSERT INTO briefing_reader_seen (scope, seen_at) VALUES (?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET seen_at = excluded.seen_at",
                (scope, at),
            )
    except Exception:
        logger.exception("right_now: recording a look failed (scope=%s)", scope)
        return None
    return at


def seen_at(scope: str) -> str | None:
    """When the reader last looked, or None. Never raises."""
    try:
        initialize_db()
        with narrative_cache._get_conn() as conn:
            row = conn.execute(
                "SELECT seen_at FROM briefing_reader_seen WHERE scope = ?", (scope,)
            ).fetchone()
    except Exception:
        logger.exception("right_now: reading seen time failed (scope=%s)", scope)
        return None
    return row["seen_at"] if row else None


__all__ = [
    "NarrativeParts",
    "Recorded",
    "initialize_db",
    "mark_seen",
    "point_key",
    "point_times",
    "record_points",
    "seen_at",
    "split_narrative",
]
