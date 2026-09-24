"""Deterministic engagement-activity gathering for the value report.

Reads what the system actually did for a client over a period — decisions,
initiatives, advice, workflow deliverables, follow-through, monitoring —
straight from one SQLite snapshot. Works identically on the live DB (active
client) and a parked slot's ``state.db`` opened read-only, because both are
the same schema (the whole point of the slot design).

Deliberately SQL-direct with existence-guarded tables (same contract as
``clients/cockpit.py``): the episodic ``list_*`` helpers have no time
filtering and only target the live DB, and a slot saved before a
schema-adding deploy must degrade to empty sections, never error. Everything
here is facts — the LLM only ever narrates this output, it never sources it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DecisionItem(BaseModel):
    timestamp: str
    domain: str = ""
    summary: str
    outcome: str = ""


class InitiativeItem(BaseModel):
    title: str
    status: str = ""
    summary: str = ""


class DeliverableItem(BaseModel):
    workflow_name: str
    title: str
    created_at: str


class EngagementActivity(BaseModel):
    """Everything the value report can truthfully claim for one period."""

    period_start: str
    period_end: str
    decisions: list[DecisionItem] = Field(default_factory=list)
    decisions_total: int = 0
    initiatives: list[InitiativeItem] = Field(default_factory=list)
    initiatives_total: int = 0
    advice_count: int = 0
    advice_by_domain: dict[str, int] = Field(default_factory=dict)
    deliverables: list[DeliverableItem] = Field(default_factory=list)
    deliverables_total: int = 0
    followups_completed: int = 0
    followups_pending: int = 0
    alerts_handled: int = 0

    def is_empty(self) -> bool:
        return not (
            self.decisions
            or self.initiatives
            or self.advice_count
            or self.deliverables
            or self.followups_completed
            or self.alerts_handled
        )


# Caps keep the listed items readable and the narrative prompt bounded. The
# *_total fields carry the uncapped COUNT(*) so the report can say
# "showing N of M" instead of silently truncating.
_MAX_LISTED = 30


def gather_engagement_activity(
    db_path: Path, since_iso: str, until_iso: str, *, read_only: bool = False
) -> EngagementActivity:
    """Collect the period's activity from one DB snapshot.

    ``since_iso`` / ``until_iso`` are ISO timestamps (date-only strings work
    too — SQLite's lexicographic ISO comparison handles both). ``read_only``
    opens via a ``mode=ro`` URI for parked slot files.
    """
    activity = EngagementActivity(period_start=since_iso, period_end=until_iso)
    if not db_path.exists():
        return activity

    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        rng = (since_iso, until_iso)

        if "decisions" in tables:
            activity.decisions_total = _count(
                conn,
                "SELECT COUNT(*) FROM decisions "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "AND TRIM(COALESCE(summary, '')) != ''",
                rng,
            )
            rows = conn.execute(
                "SELECT timestamp, domain, summary, outcome FROM decisions "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (*rng, _MAX_LISTED),
            ).fetchall()
            activity.decisions = [
                DecisionItem(
                    timestamp=r["timestamp"] or "",
                    domain=r["domain"] or "",
                    summary=r["summary"] or "",
                    outcome=r["outcome"] or "",
                )
                for r in rows
                if (r["summary"] or "").strip()
            ]

        if "initiatives" in tables:
            activity.initiatives_total = _count(
                conn,
                "SELECT COUNT(*) FROM initiatives "
                "WHERE updated_at >= ? AND updated_at <= ? "
                "AND TRIM(COALESCE(title, '')) != ''",
                rng,
            )
            rows = conn.execute(
                "SELECT title, status, summary FROM initiatives "
                "WHERE updated_at >= ? AND updated_at <= ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (*rng, _MAX_LISTED),
            ).fetchall()
            activity.initiatives = [
                InitiativeItem(
                    title=r["title"] or "",
                    status=r["status"] or "",
                    summary=r["summary"] or "",
                )
                for r in rows
                if (r["title"] or "").strip()
            ]

        if "advice_given" in tables:
            rows = conn.execute(
                "SELECT domain, COUNT(*) AS n FROM advice_given "
                "WHERE timestamp >= ? AND timestamp <= ? GROUP BY domain",
                rng,
            ).fetchall()
            activity.advice_by_domain = {
                (r["domain"] or "general"): int(r["n"]) for r in rows
            }
            activity.advice_count = sum(activity.advice_by_domain.values())

        if "workflow_runs" in tables:
            activity.deliverables_total = _count(
                conn,
                "SELECT COUNT(*) FROM workflow_runs "
                "WHERE status = 'done' AND artifact IS NOT NULL AND artifact != '' "
                "AND created_at >= ? AND created_at <= ? "
                "AND workflow_name != 'engagement_value_report'",
                rng,
            )
            rows = conn.execute(
                "SELECT workflow_name, title, created_at FROM workflow_runs "
                "WHERE status = 'done' AND artifact IS NOT NULL AND artifact != '' "
                "AND created_at >= ? AND created_at <= ? "
                # The report must never list itself as a deliverable.
                "AND workflow_name != 'engagement_value_report' "
                "ORDER BY created_at DESC LIMIT ?",
                (*rng, _MAX_LISTED),
            ).fetchall()
            activity.deliverables = [
                DeliverableItem(
                    workflow_name=r["workflow_name"] or "",
                    title=r["title"] or r["workflow_name"] or "",
                    created_at=r["created_at"] or "",
                )
                for r in rows
            ]

        if "scheduled_actions" in tables:
            activity.followups_completed = _count(
                conn,
                "SELECT COUNT(*) FROM scheduled_actions "
                # An open loop is recorded 'done' but is a promise someone
                # made, not a follow-up the Executive completed.
                "WHERE status = 'done' AND kind != 'open_loop' "
                "AND run_at >= ? AND run_at <= ?",
                rng,
            )
            activity.followups_pending = _count(
                conn,
                "SELECT COUNT(*) FROM scheduled_actions WHERE status = 'pending'",
                (),
            )

        if "alerts" in tables:
            activity.alerts_handled = _count(
                conn,
                "SELECT COUNT(*) FROM alerts "
                "WHERE status != 'unread' AND created_at >= ? AND created_at <= ?",
                rng,
            )
    finally:
        conn.close()
    return activity


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0
