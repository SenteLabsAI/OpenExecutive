from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openexecutive.alerts.models import (
    SEVERITY_RANK,
    AlertChannel,
    AlertSeverity,
    UserPreferences,
)
from openexecutive.alerts.store import DB_PATH, _get_conn  # noqa: PLC2701


def _row_to_prefs(row: sqlite3.Row) -> UserPreferences:
    d = dict(row)
    channels_raw = (d.get("channels_enabled") or "").split(",")
    channels = [
        AlertChannel(c.strip()) for c in channels_raw if c.strip() in AlertChannel._value2member_map_
    ]
    return UserPreferences(
        severity_threshold=AlertSeverity(d.get("severity_threshold", "medium")),
        quiet_hours_start=d.get("quiet_hours_start") or "",
        quiet_hours_end=d.get("quiet_hours_end") or "",
        quiet_hours_tz=d.get("quiet_hours_tz") or "",
        channels_enabled=channels or [
            AlertChannel.WEB,
            AlertChannel.SLACK_DM,
            AlertChannel.PERSISTED,
        ],
    )


def get_preferences(db_path: Path = DB_PATH) -> UserPreferences:
    if not db_path.exists():
        return UserPreferences()
    with _get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM user_preferences WHERE id = 1").fetchone()
    if not row:
        return UserPreferences()
    return _row_to_prefs(row)


def save_preferences(prefs: UserPreferences, db_path: Path = DB_PATH) -> UserPreferences:
    channels_csv = ",".join(c.value for c in prefs.channels_enabled)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_preferences
                (id, severity_threshold, quiet_hours_start, quiet_hours_end,
                 quiet_hours_tz, channels_enabled, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                severity_threshold = excluded.severity_threshold,
                quiet_hours_start = excluded.quiet_hours_start,
                quiet_hours_end = excluded.quiet_hours_end,
                quiet_hours_tz = excluded.quiet_hours_tz,
                channels_enabled = excluded.channels_enabled,
                updated_at = excluded.updated_at
            """,
            (
                prefs.severity_threshold.value,
                prefs.quiet_hours_start,
                prefs.quiet_hours_end,
                prefs.quiet_hours_tz,
                channels_csv,
                now,
            ),
        )
    return prefs


def _parse_hhmm(s: str) -> time | None:
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":", 1)
        return time(hour=int(h), minute=int(m))
    except (ValueError, TypeError):
        return None


def _quiet_hours_zone(prefs: UserPreferences) -> ZoneInfo:
    """The zone quiet hours are read in: a stored one (including an explicit
    "UTC"), else — when none is stored, or it is unknown — the user's zone
    (workspace setting, else USER_TIMEZONE, else UTC)."""
    stored = (prefs.quiet_hours_tz or "").strip()
    if stored:
        try:
            return ZoneInfo(stored)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    from openexecutive.memory.workspace_settings import get_user_timezone

    return get_user_timezone()


def _in_quiet_hours(prefs: UserPreferences, now: datetime | None = None) -> bool:
    start = _parse_hhmm(prefs.quiet_hours_start)
    end = _parse_hhmm(prefs.quiet_hours_end)
    if start is None or end is None:
        return False
    tz = _quiet_hours_zone(prefs)
    current = (now or datetime.now(UTC)).astimezone(tz).time()
    if start <= end:
        return start <= current < end
    # Wraps midnight (e.g. 22:00 → 07:00).
    return current >= start or current < end


# Channel set the user's `channels_enabled` preference does not gate.
# `channels_enabled` governs PERSONAL delivery to the principal (DM me,
# email me) — broadcast channels are an ORG-LEVEL routing decision made
# by the triage agent and shouldn't be silently dropped when the
# principal hasn't opted into them per-account. Severity threshold and
# quiet hours still apply below.
_BROADCAST_CHANNELS = frozenset({
    AlertChannel.DEPARTMENT_CHANNEL,
    AlertChannel.COMPANY_BROADCAST,
})


def resolve_channels(
    requested: list[AlertChannel],
    severity: AlertSeverity,
    prefs: UserPreferences,
    now: datetime | None = None,
) -> list[AlertChannel]:
    """Apply user controls to a Triage-suggested channel set.

    Always keeps PERSISTED so the alert is never silently dropped. Drops other
    channels when below severity threshold or inside quiet hours (unless urgent).
    Broadcast channels (department_channel / company_broadcast) survive the
    `channels_enabled` filter — they're org-routing, not personal preference.
    """
    enabled = set(prefs.channels_enabled)
    enabled.add(AlertChannel.PERSISTED)  # always persist
    intersected = [
        c for c in requested if c in enabled or c in _BROADCAST_CHANNELS
    ]
    if AlertChannel.PERSISTED not in intersected:
        intersected.append(AlertChannel.PERSISTED)

    below_threshold = (
        SEVERITY_RANK[severity.value] < SEVERITY_RANK[prefs.severity_threshold.value]
    )
    in_quiet = _in_quiet_hours(prefs, now=now) and severity != AlertSeverity.URGENT

    if below_threshold or in_quiet:
        return [AlertChannel.PERSISTED]
    return intersected


def matches_mute(topic_tags: list[str], mute_patterns: list[str]) -> bool:
    """Case-insensitive substring match of any tag against any mute pattern."""
    if not topic_tags or not mute_patterns:
        return False
    tags_lc = [t.lower() for t in topic_tags]
    for pat in mute_patterns:
        p = pat.lower().strip()
        if not p:
            continue
        if any(p in t for t in tags_lc):
            return True
    return False
