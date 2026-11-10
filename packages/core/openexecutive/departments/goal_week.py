"""What the Executive did for each goal this week, for the Goals page.

Every goal card on the Goals page says "What I did for this goal this week":
one short line per thing, "Nothing this week." when there was none, and
"Waiting on you: …" for a step held for the owner's yes. "See everything"
lists the same for a longer window.

Read from what is recorded at the time, never guessed afterwards, and with
no model call:

- **Goal updates** — ``update_department_goal`` (a chat turn told the
  Executive how a goal is going) writes an audit row naming the goal, its
  status before and after and its new current value.
- **Weekly checks** — the goal review and the department check-ins write one
  ``goal_status_review`` audit row per run, with a verdict per goal.
- **Take the lead** — a look that works toward a goal says so first
  (``take_the_lead.for_goal``), and every step it records after carries the
  goal: done and approved steps are lines, a step waiting for a yes is a
  "Waiting on you" line linking to its card on Home.

Who sees what: audit rows private to the owner stay the owner's, and a
teammate sees their own rows besides the shared ones (``AuditLogger.query``).
Take the lead acts as the owner, so its steps are shown to the owner only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_STATUS_WORDS = {
    "on_track": "on track",
    "at_risk": "at risk",
    "off_track": "off track",
    "done": "done",
    "not_started": "not started",
}

_AUDIT_LIMIT = 1000
LINE_MAX = 200
# A goal's current value as a line shows it ("Set it to 3,150/mo.").
CURRENT_MAX = 60
# Who writes goal updates and weekly verdicts. ``POST /audit/log`` lets any
# caller add a row, so a line is shown only for these writers, and only
# with a known status and a clipped value.
_UPDATE_ACTOR = "executive"
_REVIEW_ACTORS = frozenset({"goal_review", "weekly_review", "department_check_in"})


@dataclass(frozen=True)
class WeekLine:
    text: str
    at: str


@dataclass(frozen=True)
class WaitingLine:
    text: str
    at: str
    # The card on Home to open (``/?card=<id>``), when it can be found.
    card_id: int | None = None


@dataclass
class GoalWeek:
    done: list[WeekLine] = field(default_factory=list)
    waiting: list[WaitingLine] = field(default_factory=list)


def _status(value: Any) -> str | None:
    """A known status in words; None for anything else."""
    return _STATUS_WORDS.get(str(value or ""))


def _clip(text: str) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= LINE_MAX else line[: LINE_MAX - 1].rstrip() + "…"


def update_line(details: dict[str, Any]) -> str | None:
    """"Marked it at risk and set it to 3,150/mo." for one goal update;
    None when its status isn't one a goal can have."""
    before, after = details.get("from_status"), details.get("to_status")
    status = _status(after)
    if status is None:
        return None
    raw = details.get("new_current")
    new_current = " ".join(str(raw).split())[:CURRENT_MAX] if raw is not None else None
    changed_status = after != before
    prior = " ".join(str(details.get("prior_current") or "").split())[:CURRENT_MAX]
    changed_current = bool(new_current) and new_current != prior
    if changed_status and changed_current:
        return f"Marked it {status} and set it to {new_current}."
    if changed_status:
        return f"Marked it {status}."
    if changed_current:
        return f"Set it to {new_current}."
    return f"Checked it: still {status}."


def review_line(transition: dict[str, Any]) -> str | None:
    before, after = transition.get("from"), transition.get("to")
    status = _status(after)
    if status is None:
        return None
    if after != before:
        return f"Checked it in the weekly review: now {status}."
    return f"Checked it in the weekly review: still {status}."


def _goal_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _audit_lines(
    wanted: set[int], since: str, *, principal: bool, viewer: int | None,
) -> list[tuple[int, WeekLine]]:
    from openexecutive.audit.logger import get_audit_logger

    audit = get_audit_logger()
    scope: dict[str, Any] = (
        {"include_private": True, "all_owners": True} if principal
        else {"include_private": False, "owned_by": viewer}
    )
    out: list[tuple[int, WeekLine]] = []
    try:
        updates = audit.query(
            event_type="tool_invocation", q="update_department_goal ", since=since,
            limit=_AUDIT_LIMIT, **scope,
        )
        reviews = audit.query(event_type="goal_status_review", since=since, limit=_AUDIT_LIMIT, **scope)
    except Exception:
        logger.warning("goal_week: couldn't read the audit log", exc_info=True)
        return out
    for event in updates:
        details = event.details or {}
        if (
            event.actor != _UPDATE_ACTOR
            or details.get("tool") != "update_department_goal"
            or details.get("ok") is not True
        ):
            continue
        goal_id = _goal_id(details.get("goal_id"))
        text = update_line(details)
        if goal_id is not None and goal_id in wanted and text:
            out.append((goal_id, WeekLine(text=text, at=event.ts)))
    for event in reviews:
        if event.actor not in _REVIEW_ACTORS:
            continue
        for transition in (event.details or {}).get("transitions") or []:
            if not isinstance(transition, dict):
                continue
            goal_id = _goal_id(transition.get("goal_id"))
            text = review_line(transition)
            if goal_id is not None and goal_id in wanted and text:
                out.append((goal_id, WeekLine(text=text, at=event.ts)))
    return out


def _lead_text(row: Any) -> str:
    """A Take the lead step in the words Home's Done for you uses."""
    from openexecutive.api.routes.today import _mail_names

    who = _mail_names(row.mail_to) if row.mail_to else ""
    text = f"Emailed {who}" if who else row.summary
    if who and row.mail_subject:
        text = f"{text}: {row.mail_subject}"
    text = text.rstrip(".") + "."
    if row.outcome:
        text = f"{text} {row.outcome.rstrip('.')}."
    return _clip(text)


def _card_id(decision_id: int | None) -> int | None:
    if decision_id is None:
        return None
    try:
        from openexecutive.alerts.store import get_alert_by_external
        from openexecutive.memory.decision_ledger import (
            DECISION_ALERT_SOURCE,
            decision_alert_external_id,
        )

        alert = get_alert_by_external(DECISION_ALERT_SOURCE, decision_alert_external_id(decision_id))
    except Exception:
        logger.warning("goal_week: couldn't find the card for decision %s", decision_id, exc_info=True)
        return None
    return alert.id if alert is not None else None


def goal_week(
    goal_ids: list[int],
    *,
    days: int = 7,
    principal: bool,
    viewer: int | None = None,
    now: datetime | None = None,
) -> dict[int, GoalWeek]:
    """``{goal id: GoalWeek}`` for every id asked for (empty when nothing
    was done), newest line first. ``principal``: the viewer is the owner,
    who also sees what Take the lead did as them. Never raises."""
    weeks: dict[int, GoalWeek] = {gid: GoalWeek() for gid in goal_ids}
    if not goal_ids:
        return weeks
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    for goal_id, line in _audit_lines(set(goal_ids), since, principal=principal, viewer=viewer):
        weeks[goal_id].done.append(line)
    if principal:
        from openexecutive.orchestrator import take_the_lead

        for row in take_the_lead.goal_rows(goal_ids, days=days):
            week = weeks.get(row.goal_id) if row.goal_id is not None else None
            if week is None:
                continue
            if "__" in row.tool and not take_the_lead.mcp_tool_acts(row.tool):
                continue  # a connected-app read did nothing for anyone
            if row.status in ("done", "approved"):
                week.done.append(WeekLine(text=_lead_text(row), at=row.at))
            elif row.status == "waiting":
                week.waiting.append(WaitingLine(
                    text=_clip(row.summary), at=row.at, card_id=_card_id(row.decision_id),
                ))
    for week in weeks.values():
        week.done.sort(key=lambda line: line.at, reverse=True)
        week.waiting.sort(key=lambda line: line.at, reverse=True)
    return weeks
