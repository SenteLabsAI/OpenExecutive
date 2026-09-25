"""The solo morning brief's "Top three today", and a slot for each.

Three things to focus on today, picked in code (not by the model) so the pick
is stable and testable, from what the principal owns:

1. their commitments that are overdue, then due today,
2. goals that are off track, then at risk,
3. commitments due later this week,
4. active projects (initiatives), the longest-untouched first.

When the Executive can read a calendar (the MCP gateway is up and the Google
Workspace server is one it runs), one ``get_events`` call lists today's events
on the principal's calendar — their address as the calendar id, read with the
Executive's account, so it works when the principal shared their calendar with
it or signed the Executive in as themselves. Each item then gets a free block
inside working hours (``CALENDAR_BUSINESS_HOURS_START`` / ``_END``). Without a
calendar there are no slot suggestions. The read has a short timeout, and any
failure (no gateway, no principal address, an error, a timeout, a reply it
cannot read) reads as "no calendar": the brief never fails because of it.

The brief's fingerprint carries the items' keys and only a coarse hash of the
day's calendar (``calendar_hash``: busy blocks rounded to 15 minutes, no
titles, no dates), so an unchanged day still suppresses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

logger = logging.getLogger(__name__)

TOP_THREE_MAX = 3
CALENDAR_TOOL = "google_workspace__get_events"
CALENDAR_TIMEOUT_SECONDS = 4.0
# Most events listed in the brief's calendar block (and read from the tool).
_MAX_EVENTS = 12
_TITLE_MAX = 80
# A suggested block: an hour when the gap allows, never under half an hour.
_BLOCK = timedelta(minutes=60)
_MIN_BLOCK = timedelta(minutes=30)
_DEFAULT_WORK_START = time(9, 0)
_DEFAULT_WORK_END = time(18, 0)

# Ranking tiers (lower first).
_TIER_OVERDUE = 0
_TIER_DUE_TODAY = 1
_TIER_OFF_TRACK = 2
_TIER_AT_RISK = 3
_TIER_DUE_SOON = 4
_TIER_PROJECT = 5


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    # Aware datetimes; None for an all-day event.
    start: datetime | None
    end: datetime | None

    @property
    def all_day(self) -> bool:
        return self.start is None or self.end is None


# --------------------------------------------------------------------------- #
# Picking the three
# --------------------------------------------------------------------------- #


def _clean(text: object, limit: int = 160) -> str:
    return " ".join(str(text or "").split())[:limit]


def focus_candidates(
    *,
    due_soon: list[dict[str, Any]],
    goals: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Every candidate, ranked. Each is ``{key, kind, text, why, tier}``.

    ``due_soon``: ``open_loops.principal_due_soon`` rows. ``goals``: dicts with
    ``id``, ``area``, ``key_result``, ``target``, ``current``, ``status``.
    ``projects``: dicts with ``id``, ``title``, ``updated_at``. ``key`` holds
    no date, so it can go into a fingerprint.
    """
    out: list[dict[str, Any]] = []
    for d in due_soon:
        state = str(d.get("state", ""))
        raw = str(d.get("due_date", ""))[:10]
        if state == "overdue":
            tier, why = _TIER_OVERDUE, f"overdue (was due {raw})"
        elif state == "today":
            tier, why = _TIER_DUE_TODAY, "due today"
        else:
            tier, why = _TIER_DUE_SOON, f"due {raw}"
        out.append({
            "key": f"loop:{int(d.get('loop_id') or 0)}:{state}",
            "kind": "commitment",
            "text": _clean(d.get("description")),
            "why": why,
            "tier": tier,
            "order": str(d.get("due_at") or raw),
        })
    for g in goals:
        status = str(g.get("status", ""))
        if status not in ("off_track", "at_risk"):
            continue
        current = _clean(g.get("current"), 80)
        why = f"{status.replace('_', ' ')}; target {_clean(g.get('target'), 80)}"
        if current:
            why += f", now {current}"
        out.append({
            "key": f"goal:{int(g.get('id') or 0)}:{status}",
            "kind": "goal",
            "text": f"{_clean(g.get('area'), 60)}: {_clean(g.get('key_result'))}",
            "why": why,
            "tier": _TIER_OFF_TRACK if status == "off_track" else _TIER_AT_RISK,
            "order": "",
        })
    for p in projects:
        updated = _parse(p.get("updated_at"))
        days = max(0, (now - updated).days) if updated is not None else None
        why = "active project" if days is None else f"active project, last updated {days}d ago"
        out.append({
            "key": f"project:{int(p.get('id') or 0)}",
            "kind": "project",
            "text": _clean(p.get("title")),
            "why": why,
            "tier": _TIER_PROJECT,
            # Longest-untouched first; unknown last.
            "order": updated.isoformat() if updated is not None else "~",
        })
    out.sort(key=lambda c: (c["tier"], c["order"]))
    return out


def pick_top_three(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first three distinct candidates, without the ranking fields."""
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in candidates:
        if c["key"] in seen or not c["text"]:
            continue
        seen.add(c["key"])
        picked.append({k: c[k] for k in ("key", "kind", "text", "why")})
        if len(picked) >= TOP_THREE_MAX:
            break
    return picked


def gather_goals_and_projects() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Goals at risk (by area) and active projects from the stores. A read
    failure is logged and reads as empty."""
    goals: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    try:
        from openexecutive.departments import registry as dept_registry

        for state in dept_registry.list_states():
            for g in state.goals:
                if g.status in ("off_track", "at_risk"):
                    goals.append({
                        "id": g.id, "area": state.config.title, "key_result": g.key_result,
                        "target": g.target, "current": g.current, "status": g.status,
                    })
    except Exception:
        logger.warning("top_three: goals unreadable", exc_info=True)
    try:
        from openexecutive.memory.episodic import _resolve_db_path, get_active_initiatives

        for i in get_active_initiatives(db_path=_resolve_db_path(None)):
            if i.status == "active":
                projects.append({"id": i.id, "title": i.title, "updated_at": i.updated_at})
    except Exception:
        logger.warning("top_three: projects unreadable", exc_info=True)
    return goals, projects


# --------------------------------------------------------------------------- #
# Reading today's calendar (one call, short timeout, never raises)
# --------------------------------------------------------------------------- #


def _parse(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _event_time(raw: object, tz: tzinfo) -> datetime | None:
    """A timed event's start or end, aware; None for an all-day date."""
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date_time")
    text = str(raw or "").strip()
    if len(text) <= 10:  # "2026-09-25" — a date, i.e. all day
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _event(title: object, start: object, end: object, tz: tzinfo) -> CalendarEvent:
    s, e = _event_time(start, tz), _event_time(end, tz)
    if s is None or e is None or e <= s:
        s = e = None
    return CalendarEvent(title=_clean(title, _TITLE_MAX) or "(untitled)", start=s, end=e)


# workspace-mcp's text listing: `- "Title" (Starts: <iso>, Ends: <iso>) ID: …`
_LINE_RE = re.compile(r'^\s*-\s*"(?P<title>.*)"\s*\(Starts:\s*(?P<start>[^,]+),\s*Ends:\s*(?P<end>[^)]+)\)')
_EMPTY_RE = re.compile(r"^\s*No events found", re.IGNORECASE | re.MULTILINE)
_LISTED_RE = re.compile(r"^\s*Successfully retrieved \d+ events?", re.IGNORECASE | re.MULTILINE)


def parse_events(text: str, tz: tzinfo) -> list[CalendarEvent] | None:
    """Events from a ``get_events`` reply, [] for an empty day, or None when
    the reply is an error or a shape this does not know — read as "no
    calendar", never as a free day."""
    from openexecutive.workflows.action_step import looks_like_error

    if not isinstance(text, str) or not text.strip() or looks_like_error(text):
        return None
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        items = parsed.get("events", parsed.get("items")) if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            return None
        return [
            _event(i.get("summary") or i.get("title"), i.get("start"), i.get("end"), tz)
            for i in items[:_MAX_EVENTS]
            if isinstance(i, dict)
        ]
    if _EMPTY_RE.search(text):
        return []
    events = [
        _event(m["title"], m["start"].strip(), m["end"].strip(), tz)
        for m in (_LINE_RE.match(line) for line in text.splitlines())
        if m is not None
    ]
    if events or _LISTED_RE.search(text):
        return events[:_MAX_EVENTS]
    return None


def _calendar_id(principal_email: str) -> str:
    """The principal's calendar: "primary" when the Executive is signed in as
    the principal, else their address (a calendar they shared with it)."""
    try:
        from openexecutive.config import get_settings

        own = (get_settings().exec_email_address or "").strip().lower()
    except Exception:
        own = ""
    return "primary" if own and own == principal_email.strip().lower() else principal_email


async def read_todays_calendar(now: datetime, tz: tzinfo) -> list[CalendarEvent] | None:
    """Today's events on the principal's calendar, or None when there is no
    calendar to read. One tool call with a short timeout; never raises."""
    try:
        from openexecutive.config import get_settings
        from openexecutive.orchestrator.mcp_gateway import get_active_gateway
        from openexecutive.people.store import find_principal_person
        from openexecutive.scheduler.runner import google_workspace_ready

        if not google_workspace_ready():
            return None
        gateway = get_active_gateway()
        principal = find_principal_person()
        email = (principal.email or "").strip() if principal is not None else ""
        if gateway is None or not email:
            return None
        local_day = now.astimezone(tz).date()
        start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
        end = start + timedelta(days=1)
        raw = await asyncio.wait_for(
            gateway.call_tool({
                "name": CALENDAR_TOOL,
                "arguments": {
                    "user_google_email": get_settings().exec_email_address,
                    "calendar_id": _calendar_id(email),
                    "time_min": start.astimezone(UTC).isoformat(),
                    "time_max": end.astimezone(UTC).isoformat(),
                    "max_results": _MAX_EVENTS,
                },
            }),
            timeout=CALENDAR_TIMEOUT_SECONDS,
        )
    except Exception:
        # TimeoutError included: a slow calendar never holds the brief up.
        logger.info("top_three: calendar unavailable for the brief", exc_info=True)
        return None
    events = parse_events(raw, tz)
    if events is None:
        logger.info("top_three: calendar reply not readable: %s", str(raw)[:200])
    return events


# --------------------------------------------------------------------------- #
# Free slots
# --------------------------------------------------------------------------- #


def _hhmm(raw: str, default: time) -> time:
    try:
        hh, mm = str(raw).strip().split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return default


def working_hours() -> tuple[time, time]:
    """The calendar business hours setting, 09:00–18:00 when unreadable."""
    try:
        from openexecutive.config import get_settings

        s = get_settings()
        start = _hhmm(s.calendar_business_hours_start, _DEFAULT_WORK_START)
        end = _hhmm(s.calendar_business_hours_end, _DEFAULT_WORK_END)
    except Exception:
        return _DEFAULT_WORK_START, _DEFAULT_WORK_END
    return (start, end) if start < end else (_DEFAULT_WORK_START, _DEFAULT_WORK_END)


def free_gaps(
    events: list[CalendarEvent],
    *,
    now: datetime,
    tz: tzinfo,
    work: tuple[time, time],
) -> list[tuple[datetime, datetime]]:
    """Free stretches of at least half an hour left today inside working
    hours, in order. All-day events do not block time."""
    day = now.astimezone(tz).date()
    window_start = datetime.combine(day, work[0], tzinfo=tz)
    window_end = datetime.combine(day, work[1], tzinfo=tz)
    # Not before now, rounded up to the next quarter hour.
    local_now = now.astimezone(tz)
    rounded = local_now.replace(second=0, microsecond=0)
    rounded += timedelta(minutes=(-rounded.minute) % 15)
    if rounded < local_now:
        rounded += timedelta(minutes=15)
    cursor = max(window_start, rounded)
    busy = sorted(
        (e.start, e.end) for e in events
        if e.start is not None and e.end is not None
    )
    gaps: list[tuple[datetime, datetime]] = []
    for b_start, b_end in busy:
        if b_end <= cursor:
            continue
        if b_start >= window_end:
            break
        if b_start - cursor >= _MIN_BLOCK:
            gaps.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if window_end - cursor >= _MIN_BLOCK:
        gaps.append((cursor, window_end))
    return gaps


def assign_slots(
    items: list[dict[str, Any]], gaps: list[tuple[datetime, datetime]], tz: tzinfo
) -> list[dict[str, Any]]:
    """Copies of ``items``, each with ``slot`` — "10:30–11:30" (local), or ""
    when no free block is left for it."""
    out: list[dict[str, Any]] = []
    queue = list(gaps)
    for item in items:
        slot = ""
        while queue:
            g_start, g_end = queue[0]
            end = min(g_start + _BLOCK, g_end)
            if end - g_start < _MIN_BLOCK:
                queue.pop(0)
                continue
            slot = f"{g_start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M}"
            queue[0] = (end, g_end)
            break
        out.append({**item, "slot": slot})
    return out


def calendar_hash(events: list[CalendarEvent], tz: tzinfo) -> str:
    """A coarse hash of the day: busy blocks rounded down to 15 minutes, and
    how many all-day events. No titles and no dates, so the same shape of day
    hashes the same and an unchanged day still suppresses the brief."""

    def _q(dt: datetime) -> str:
        local = dt.astimezone(tz)
        return f"{local.hour:02d}:{local.minute // 15 * 15:02d}"

    blocks = sorted(
        f"{_q(e.start)}-{_q(e.end)}" for e in events if e.start is not None and e.end is not None
    )
    all_day = sum(1 for e in events if e.all_day)
    blob = json.dumps({"blocks": blocks, "all_day": all_day}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def render_events(events: list[CalendarEvent], tz: tzinfo) -> list[dict[str, str]]:
    """``[{time, title}]`` for the brief: all-day first, then by start."""
    rows = [{"time": "all day", "title": e.title} for e in events if e.all_day]
    timed = sorted(
        (e.start, e.end, e.title) for e in events if e.start is not None and e.end is not None
    )
    for start, end, title in timed:
        rows.append({
            "time": f"{start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M}",
            "title": title,
        })
    return rows


# --------------------------------------------------------------------------- #
# Entry point for the morning brief
# --------------------------------------------------------------------------- #


async def build_top_three(
    due_soon: list[dict[str, Any]], *, now: datetime | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """``(items, calendar)`` for the solo morning brief. Never raises.

    ``items``: up to three ``{key, kind, text, why, slot}`` — ``slot`` is set
    only when a calendar was read. ``calendar``: ``{"events": [...], "hash":
    str}`` when one was read, else None. The calendar is read only when there
    is something to place.
    """
    try:
        from openexecutive.memory.workspace_settings import get_user_timezone

        now = now or datetime.now(UTC)
        tz = get_user_timezone()
        goals, projects = gather_goals_and_projects()
        items = pick_top_three(
            focus_candidates(due_soon=due_soon, goals=goals, projects=projects, now=now)
        )
    except Exception:
        logger.exception("top_three: picking failed — the brief goes without it")
        return [], None
    if not items:
        return [], None
    events = await read_todays_calendar(now, tz)
    if events is None:
        return items, None
    try:
        items = assign_slots(items, free_gaps(events, now=now, tz=tz, work=working_hours()), tz)
        calendar = {"events": render_events(events, tz), "hash": calendar_hash(events, tz)}
    except Exception:
        logger.exception("top_three: slot planning failed — no slots in this brief")
        return items, None
    return items, calendar


__all__ = [
    "CALENDAR_TIMEOUT_SECONDS",
    "CALENDAR_TOOL",
    "TOP_THREE_MAX",
    "CalendarEvent",
    "assign_slots",
    "build_top_three",
    "calendar_hash",
    "focus_candidates",
    "free_gaps",
    "gather_goals_and_projects",
    "parse_events",
    "pick_top_three",
    "read_todays_calendar",
    "render_events",
    "working_hours",
]
