"""Cross-department roll-up for the CEO review.

The CEO-advisor's weekly check-in (the Strategy department's cadence) needs the
whole company in view, not one department's goals: where every goal stands,
who owns each function, and what has been decided recently. This renders that
as a compact ``<company_rollup>`` block. Pure read; never raises.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_DECISIONS = 10
_MAX_SUMMARY_CHARS = 160


def render_company_rollup(
    *, exclude_slug: str | None = None, db_path: Path | None = None
) -> str:
    """Render every department's owner and goals, plus recent decisions."""
    from openexecutive.departments import store as dept_store
    from openexecutive.people import store as people_store

    try:
        states = dept_store.list_departments(db_path=db_path)
    except Exception:  # noqa: BLE001 - the roll-up is advisory context.
        logger.warning("rollup: could not load departments", exc_info=True)
        return ""

    heads: dict[int, str] = {}
    try:
        heads = {
            p.id: p.full_name
            for p in people_store.list_people(db_path=db_path)
            if p.id is not None
        }
    except Exception:  # noqa: BLE001
        logger.warning("rollup: could not load people", exc_info=True)

    sections: list[str] = []
    for state in states:
        cfg = state.config
        if cfg.slug == exclude_slug:
            continue
        head = heads.get(cfg.head_person_id) if cfg.head_person_id is not None else None
        lines = [
            f"### {cfg.title} (owner: {head or 'unassigned'}; "
            f"authority: {cfg.authority_level.value})"
        ]
        if not state.goals:
            lines.append("- No goals set.")
        for goal in state.goals:
            current = goal.current or "no current value recorded"
            reviewed = goal.last_reviewed_at[:10] if goal.last_reviewed_at else "never reviewed"
            lines.append(
                f"- [{goal.status}] {goal.key_result} — target: {goal.target}; "
                f"current: {current}; last reviewed: {reviewed}"
            )
        sections.append("\n".join(lines))

    if not sections:
        return ""

    decisions_block = ""
    try:
        from openexecutive.memory import episodic

        decisions = episodic.get_recent_decisions(limit=_MAX_DECISIONS, db_path=episodic.DB_PATH)
        if decisions:
            rows = [
                f"- {getattr(d, 'timestamp', '')[:10]} "
                f"[{getattr(d, 'department', '') or 'company'}]: "
                f"{str(getattr(d, 'summary', ''))[:_MAX_SUMMARY_CHARS]}"
                for d in decisions
            ]
            decisions_block = "\n\n### Recent decisions (all departments)\n" + "\n".join(rows)
    except Exception:  # noqa: BLE001
        logger.warning("rollup: could not load decisions", exc_info=True)

    return (
        "<company_rollup>\n"
        "Every department's owner and goals as last recorded. A goal with no current "
        "value or no review is itself a finding.\n\n"
        + "\n\n".join(sections)
        + decisions_block
        + "\n</company_rollup>"
    )
