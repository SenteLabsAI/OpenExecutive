"""The weekly review of goals no department check-in covers.

A department with a specialist grades its own goals in its check-in
(``workflows.department_check_in``). Every other goal — one with no
department, or in a department with no specialist — would otherwise never be
graded, so it could never show as at risk. This review grades those goals
once a week (the ``goal_review`` scheduled kind, ``scheduler.runner``), in
both workspace modes, with ``goals.FALLBACK_REVIEWER``, and writes the
verdicts back through the check-in's own ``persist_goal_verdicts``.

It costs nothing when there is nothing to do: no such goals, or none that
changed or went unreviewed for a week, means no model call.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from openexecutive.departments.goals import FALLBACK_REVIEWER, unreviewed_goals
from openexecutive.departments.models import (
    DepartmentCharter,
    DepartmentConfig,
    DepartmentState,
    Goal,
)

logger = logging.getLogger(__name__)

REVIEW_ACTOR = "goal_review"
# A goal graded within this long, and not edited since, is skipped.
_FRESH_FOR = timedelta(days=6)


def _needs_review(goal: Goal, now: datetime) -> bool:
    from openexecutive.alerts.lifecycle import parse_aware

    reviewed = parse_aware(goal.last_reviewed_at) if goal.last_reviewed_at else None
    if reviewed is None:
        return True
    edited = parse_aware(goal.updated_at) if goal.updated_at else None
    if edited is not None and edited > reviewed:
        return True
    return now - reviewed >= _FRESH_FOR


def review_group(goals: list[Goal]) -> DepartmentState:
    """The goals as one review group, in the shape the check-in helpers take.
    Its empty slug marks it as no department (the audit row carries none)."""
    return DepartmentState(
        config=DepartmentConfig(
            slug="",
            title="Company goals",
            specialist_key=FALLBACK_REVIEWER,
            charter=DepartmentCharter(mission="The goals no department's specialist reviews."),
        ),
        goals=goals,
    )


async def run_review(now: datetime | None = None) -> int:
    """Grade the goals no check-in covers. Returns how many were graded.
    Never raises."""
    from openexecutive.departments import registry
    from openexecutive.orchestrator.router import route_to_specialist
    from openexecutive.workflows.department_check_in import (
        _parse_verdicts,
        _render_goals,
        persist_goal_verdicts,
    )

    now = now or datetime.now(UTC)
    try:
        goals = [g for g in unreviewed_goals(registry.list_states()) if _needs_review(g, now)]
    except Exception:
        logger.exception("goal_review: goals unreadable")
        return 0
    if not goals:
        logger.info("goal_review: nothing to review")
        return 0

    group = review_group(goals)
    period = now.strftime("%Y-%m-%d")
    context = _render_goals(group)
    try:
        raw = await route_to_specialist(
            specialist_name=FALLBACK_REVIEWER,
            query=(
                f"Weekly review of the company's goals for {period}. For each goal, "
                "assess whether it is on_track, at_risk or off_track from its current "
                "value against its target and how much of its period is left.\n\n"
                "Return ONLY a JSON object on its own — no prose before or after, "
                "no markdown fences — with this exact shape:\n"
                '{"verdicts": [{"goal_id": <int from the [id=N] prefix>, '
                '"status": "on_track" | "at_risk" | "off_track", '
                '"rationale": "<one short sentence>"}, ...], '
                '"narrative": "<one sentence>"}\n\n'
                "Use the integer id from each goal's `[id=N]` prefix below. Skip "
                "goals you cannot grade — never guess a number that is not given.\n\n"
                f"{context}"
            ),
            context=context,
        )
        verdicts, _narrative = _parse_verdicts(raw)
        written = persist_goal_verdicts(group, verdicts, now=now, period=period, actor=REVIEW_ACTOR)
    except Exception:
        logger.exception("goal_review: grading failed")
        return 0
    logger.info("goal_review: graded %d of %d goal(s)", len(written), len(goals))
    return len(written)
