"""What the Executive did for the owner over a month or a week, in numbers.

The engagement value report (``workflows/engagement_value_report.py``) is
written for a fractional executive's client and runs only on demand; the
weekly review runs in solo mode only; and Pulse's Done recently is a feed of
the last few rows. This module is the owner's own tally, for ``GET /today/value``, the
"Your month" card on Pulse and the monthly message
(``scheduler.runner._run_monthly_value``).

Every number is a ``COUNT(*)`` of rows that prove the thing happened, read
straight from SQLite; no model writes or estimates any of it, and there is no
"hours saved" figure because nothing recorded supports one:

  • messages sent — the DM handlers' own ``tool_invocation`` rows
    (``kind=outbound``, sent, not held or suppressed) to anyone but the owner,
    plus mail sent through the Gmail or Outlook tools. Mail sent from a chat
    turn counts here, whatever Done recently shows.
  • inbox replies drafted and sent — the owner's own Act as me rows.
  • meetings booked — successful calendar create calls.
  • briefs delivered — the scheduler's ``delivered`` row per brief.
  • reports — finished workflow runs, minus the daily rhythm runs.
  • follow-ups — scheduled actions the runner delivered (follow-ups, nudges).
  • tasks tracked and closed — open loops opened, and closed as done.
  • decisions — gated proposals that reached a verdict.
  • alerts flagged, and requests answered (one ``chat_turn`` out row each).

Cost is left out on purpose: next to what the owner got it reads as a bill,
and it already has its own page (``GET /audit/usage``).

Only the owner reads it, so rows private to the owner count; a row private to
another team member never does. Tables are existence-guarded: a missing table
reads as zero, never an error.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Period = Literal["month", "week"]

# The Executive's own daily rhythm runs (as ``api.routes.today.RHYTHM_WORKFLOWS``):
# counted as briefs, not as reports.
_RHYTHM_WORKFLOWS = ("morning_brief", "end_of_day_digest", "executive_reflection", "weekly_review")

# DM handlers that write their own sent row (``orchestrator.schedule_tools``).
_DM_TOOLS = ("send_slack_dm", "send_discord_dm", "send_telegram_message")
# Mail sends through the workspace tools, in the gateway's normalized form
# (lowercase, ``-`` as ``_``). Drafts are not sends.
_MAIL_SEND_TOOLS = (
    "google_workspace__send_gmail_message",
    "microsoft_365__send_mail",
    "microsoft_365__reply_mail_message",
    "microsoft_365__reply_all_mail_message",
    "microsoft_365__forward_mail_message",
    "microsoft_365__send_draft_message",
)
_MEETING_TOOLS = ("create_calendar_event", "create_instant_meeting")
_DECIDED = (
    "executed", "approved_unchanged", "approved_with_edit", "rejected", "reversed",
)
_MAX_DELIVERABLES = 10


class ValueLine(BaseModel):
    """One counted thing, this period and the one before."""

    key: str
    label: str
    count: int
    previous: int


class Deliverable(BaseModel):
    run_id: str
    title: str
    workflow_name: str
    finished_at: str


class ValueSummary(BaseModel):
    period: Period
    label: str  # "October 2026", "Week of 12 October"
    start: str  # UTC ISO, inclusive
    end: str  # UTC ISO, exclusive
    lines: list[ValueLine] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    deliverables_total: int = 0

    def is_empty(self) -> bool:
        return not any(line.count for line in self.lines)

    def line(self, key: str) -> ValueLine | None:
        return next((ln for ln in self.lines if ln.key == key), None)


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime
    label: str

    @property
    def bounds(self) -> tuple[str, str]:
        return (_iso(self.start), _iso(self.end))


def _iso(dt: datetime) -> str:
    # Second precision with an explicit offset: compares correctly as a
    # string against both "...+00:00" and the audit log's "....ffffffZ".
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _month_start(day: date, months_back: int) -> date:
    index = day.year * 12 + (day.month - 1) - months_back
    return date(index // 12, index % 12 + 1, 1)


def period_window(period: Period, offset: int, *, now: datetime, zone: tzinfo) -> _Window:
    """The calendar month or week (Monday first) ``offset`` periods before the
    current one, in ``zone``. ``offset`` 0 is the period ``now`` is in."""
    today = now.astimezone(zone).date()
    if period == "month":
        first = _month_start(today, offset)
        nxt = _month_start(first, -1)
        label = f"{first:%B} {first.year}"
    else:
        first = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
        nxt = first + timedelta(days=7)
        label = f"Week of {first.day} {first:%B}"
    start = datetime(first.year, first.month, first.day, tzinfo=zone)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return _Window(start, end, label)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@contextmanager
def _conn(path: Path) -> Iterator[sqlite3.Connection | None]:
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(conn: sqlite3.Connection | None, table: str, where: str, params: tuple[Any, ...]) -> int:
    if conn is None or table not in _tables(conn):
        return 0
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()  # noqa: S608
    except sqlite3.OperationalError:
        return 0  # an older schema without the column
    return int(row[0] or 0)


# Audit rows the owner may count: theirs and the team's, never a row private
# to another team member.
_AUDIT_SCOPE = "ts >= ? AND ts < ? AND private_to_person IS NULL"


def _audit_count(conn: sqlite3.Connection | None, bounds: tuple[str, str],
                 extra: str, params: tuple[Any, ...] = ()) -> int:
    return _count(conn, "audit_log", f"{_AUDIT_SCOPE} AND {extra}", (*bounds, *params))


def _in(values: tuple[str, ...]) -> str:
    return "(" + ",".join("?" * len(values)) + ")"


def _owner_refs(principal: Any) -> set[str]:
    """The owner's own chat ids: a DM to them is a brief or a reply, not a
    message sent on their behalf."""
    if principal is None:
        return set()
    return {
        str(v) for v in (
            principal.slack_user_id, principal.discord_user_id, principal.telegram_chat_id,
        ) if v
    }


def _messages_sent(conn: sqlite3.Connection | None, bounds: tuple[str, str],
                   owner_refs: set[str]) -> int:
    if conn is None or "audit_log" not in _tables(conn):
        return 0
    try:
        dms = conn.execute(
        "SELECT COALESCE(json_extract(details_json,'$.user_id'),"
        " json_extract(details_json,'$.chat_id')) AS ref, COUNT(*) AS n "
        f"FROM audit_log WHERE {_AUDIT_SCOPE} AND event_type = 'tool_invocation' "
        "AND json_extract(details_json,'$.kind') = 'outbound' "
        f"AND json_extract(details_json,'$.tool') IN {_in(_DM_TOOLS)} "
        "AND json_extract(details_json,'$.ok') = 1 "
        "AND COALESCE(json_extract(details_json,'$.held'),0) = 0 "
        "AND COALESCE(json_extract(details_json,'$.suppressed'),0) = 0 "
        "GROUP BY ref",
        (*bounds, *_DM_TOOLS),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0  # an older schema without the column
    dm_total = sum(int(r["n"]) for r in dms if str(r["ref"]) not in owner_refs)
    # A workspace send that came back with an error is not a send.
    mail = _audit_count(
        conn, bounds,
        "event_type = 'tool_invocation' AND json_extract(details_json,'$.kind') = 'mcp' "
        "AND replace(lower(json_extract(details_json,'$.tool')),'-','_') IN "
        f"{_in(_MAIL_SEND_TOOLS)} "
        "AND COALESCE(json_extract(details_json,'$.ok'),1) != 0 "
        "AND lower(COALESCE(json_extract(details_json,'$.result_preview'),'')) NOT LIKE '%error%'",
        _MAIL_SEND_TOOLS,
    )
    return dm_total + mail


def _counts(audit: sqlite3.Connection | None, db: sqlite3.Connection | None,
            bounds: tuple[str, str], *, principal: Any) -> dict[str, int]:
    owner_id = getattr(principal, "id", None)
    own = "json_extract(details_json,'$.person_id') = ?"
    return {
        "messages_sent": _messages_sent(audit, bounds, _owner_refs(principal)),
        "inbox_drafted": _audit_count(
            audit, bounds,
            f"event_type IN ('delegation_reply_drafted','delegation_drafted') AND {own}",
            (owner_id,),
        ) if owner_id is not None else 0,
        "inbox_sent": _audit_count(
            audit, bounds, f"event_type = 'delegation_reply_sent' AND {own}", (owner_id,),
        ) if owner_id is not None else 0,
        "meetings_booked": _audit_count(
            audit, bounds,
            "event_type = 'tool_invocation' AND json_extract(details_json,'$.kind') = 'skill' "
            f"AND json_extract(details_json,'$.tool') IN {_in(_MEETING_TOOLS)} "
            "AND COALESCE(json_extract(details_json,'$.ok'),1) != 0",
            _MEETING_TOOLS,
        ),
        "briefs_delivered": _audit_count(
            audit, bounds,
            "event_type = 'scheduled_action' AND json_extract(details_json,'$.phase') = 'delivered' "
            "AND json_extract(details_json,'$.kind') LIKE 'principal_%'",
        ),
        "reports": _count(
            db, "workflow_runs",
            f"status = 'done' AND updated_at >= ? AND updated_at < ? "
            f"AND workflow_name NOT IN {_in(_RHYTHM_WORKFLOWS)}",
            (*bounds, *_RHYTHM_WORKFLOWS),
        ),
        "follow_ups": _audit_count(
            audit, bounds,
            "event_type = 'scheduled_action' AND json_extract(details_json,'$.action_id') IS NOT NULL "
            "AND json_extract(details_json,'$.phase') IN ('delivered','held')",
        ),
        "tasks_tracked": _count(
            db, "scheduled_actions", "kind = 'open_loop' AND created_at >= ? AND created_at < ?",
            bounds,
        ),
        "tasks_closed": _audit_count(
            audit, bounds,
            "json_extract(details_json,'$.op') = 'loop_closed' "
            "AND json_extract(details_json,'$.reason') = 'done'",
        ),
        "decisions": _count(
            db, "decision_instances",
            f"resolved_at >= ? AND resolved_at < ? AND status IN {_in(_DECIDED)}",
            (*bounds, *_DECIDED),
        ),
        "alerts": _count(db, "alerts", "created_at >= ? AND created_at < ?", bounds),
        "answered": _audit_count(
            audit, bounds,
            "event_type = 'chat_turn' AND json_extract(details_json,'$.direction') = 'out'",
        ),
    }


_LABELS: dict[str, str] = {
    "messages_sent": "Messages sent for you",
    "inbox_drafted": "Replies drafted in your inbox",
    "inbox_sent": "Replies sent from your inbox",
    "meetings_booked": "Meetings booked",
    "briefs_delivered": "Briefs delivered",
    "reports": "Reports and documents",
    "follow_ups": "Follow-ups and nudges sent",
    "tasks_tracked": "Tasks tracked",
    "tasks_closed": "Tasks closed as done",
    "decisions": "Proposals decided",
    "alerts": "Things it flagged",
    "answered": "Requests answered",
}


def _deliverables(db: sqlite3.Connection | None, bounds: tuple[str, str]) -> list[Deliverable]:
    if db is None or "workflow_runs" not in _tables(db):
        return []
    rows = db.execute(
        "SELECT run_id, title, workflow_name, updated_at FROM workflow_runs "
        "WHERE status = 'done' AND updated_at >= ? AND updated_at < ? "
        f"AND workflow_name NOT IN {_in(_RHYTHM_WORKFLOWS)} "
        "ORDER BY updated_at DESC LIMIT ?",
        (*bounds, *_RHYTHM_WORKFLOWS, _MAX_DELIVERABLES),
    ).fetchall()
    return [
        Deliverable(run_id=r["run_id"], title=r["title"] or r["workflow_name"],
                    workflow_name=r["workflow_name"], finished_at=r["updated_at"])
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def summarize(
    period: Period = "month",
    offset: int = 0,
    *,
    now: datetime | None = None,
    zone: tzinfo | None = None,
    db_path: Path | None = None,
    audit_db_path: Path | None = None,
) -> ValueSummary:
    """The tally for one period, with the period before it beside each line."""
    from openexecutive.audit import get_audit_logger
    from openexecutive.memory.episodic import _resolve_db_path
    from openexecutive.people.store import find_principal_person

    if zone is None:
        from openexecutive.memory.workspace_settings import get_user_timezone

        zone = get_user_timezone()
    now = now or datetime.now(UTC)
    window = period_window(period, offset, now=now, zone=zone)
    before = period_window(period, offset + 1, now=now, zone=zone)
    principal = find_principal_person()
    audit_path = audit_db_path or get_audit_logger()._db_path
    with _conn(audit_path) as audit, _conn(_resolve_db_path(db_path)) as db:
        current = _counts(audit, db, window.bounds, principal=principal)
        previous = _counts(audit, db, before.bounds, principal=principal)
        deliverables = _deliverables(db, window.bounds)
    start, end = window.bounds
    return ValueSummary(
        period=period,
        label=window.label,
        start=start,
        end=end,
        lines=[
            ValueLine(key=k, label=_LABELS[k], count=current[k], previous=previous[k])
            for k in _LABELS
        ],
        deliverables=deliverables,
        deliverables_total=current["reports"],
    )


# Lines the monthly message leads with, in order, when they are non-zero.
_HEADLINE_KEYS = (
    "messages_sent", "inbox_sent", "meetings_booked", "reports", "follow_ups",
    "tasks_closed", "decisions", "answered",
)


def render_message(summary: ValueSummary, *, link: str = "") -> str:
    """The short monthly message: the counted lines that aren't zero and
    where to see the rest. Deterministic text."""
    lines = [f"**What your Executive did in {summary.label}**", ""]
    shown = [summary.line(k) for k in _HEADLINE_KEYS]
    for line in shown:
        if line is not None and line.count:
            lines.append(f"- {line.label}: {line.count}")
    if len(lines) == 2:
        lines.append("- Nothing to report this time.")
    if link:
        lines += ["", f"The full breakdown: {link}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The monthly message
# --------------------------------------------------------------------------- #

# The workspace's local hours it may go out in, on the first weekday of the
# month (the briefs' daytime, as the note reminders use).
FIRST_HOUR = 9
LAST_HOUR = 18
CHECK_EVERY = timedelta(minutes=15)
# Claimed once per month under this scope in ``briefing_narrative``.
_CLAIM_PREFIX = "value_summary:"

_last_check_at: datetime | None = None
_task: asyncio.Task[bool] | None = None


def _first_weekday(day: date) -> date:
    first = day.replace(day=1)
    while first.weekday() >= 5:
        first += timedelta(days=1)
    return first


def _claim_month(month: str, now: datetime) -> bool:
    """Record that last month's message is going out; False when it already
    did (or another worker got there first). Never released: one try."""
    from openexecutive.briefing import narrative_cache

    try:
        narrative_cache.initialize_db()
        with narrative_cache._get_conn() as conn:
            return bool(conn.execute(
                "INSERT OR IGNORE INTO briefing_narrative "
                "(scope, input_hash, narrative_text, generated_at) VALUES (?, '', '', ?)",
                (_CLAIM_PREFIX + month, now.astimezone(UTC).isoformat()),
            ).rowcount)
    except Exception:
        logger.warning("value_summary: couldn't record the monthly claim", exc_info=True)
        return False


def value_link() -> str:
    from openexecutive.config import get_settings

    return get_settings().ui_base_url.rstrip("/") + "/memories?tab=value"


async def send_monthly(now: datetime) -> bool:
    """On the first weekday of a month, in daytime, send the owner last
    month's tally once. True when it went. Never raises."""
    from openexecutive.audit import log_event
    from openexecutive.audit.context import private_rows
    from openexecutive.config import get_settings
    from openexecutive.memory.workspace_settings import get_user_timezone
    from openexecutive.scheduler.runner import _deliver_to_principal

    try:
        if not get_settings().value_summary_monthly_enabled:
            return False
        zone = get_user_timezone()
        local = now.astimezone(zone)
        if local.date() != _first_weekday(local.date()) or not FIRST_HOUR <= local.hour < LAST_HOUR:
            return False
        summary = summarize("month", 1, now=now, zone=zone)
        month = summary.start[:7]
        if summary.is_empty() or not _claim_month(month, now):
            return False
        text = render_message(summary, link=value_link())
        with private_rows():
            delivery = await _deliver_to_principal(text, label="Your month")
    except Exception:
        logger.warning("value_summary: monthly message failed", exc_info=True)
        return False
    log_event(
        "value_summary_sent" if delivery.ok else "value_summary_not_sent",
        f"Monthly summary for {summary.label}: {delivery.reason}",
        actor="scheduler",
        details={"month": month, "channel": delivery.channel, "reason": delivery.reason},
        private=True,
    )
    return delivery.ok


def maybe_send_monthly(now: datetime) -> bool:
    """Start a monthly check when the interval has passed and none is running.
    True when one started. Never raises."""
    global _last_check_at, _task
    try:
        if _task is not None and not _task.done():
            return False
        if _last_check_at is not None and now - _last_check_at < CHECK_EVERY:
            return False
        _last_check_at = now
        _task = asyncio.get_running_loop().create_task(send_monthly(now))
        return True
    except Exception:
        logger.warning("value_summary: couldn't start the monthly check", exc_info=True)
        return False


__all__ = [
    "maybe_send_monthly",
    "send_monthly",
    "Deliverable",
    "Period",
    "ValueLine",
    "ValueSummary",
    "period_window",
    "render_message",
    "summarize",
]
