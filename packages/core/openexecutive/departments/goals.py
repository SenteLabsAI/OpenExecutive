"""Goals as their own thing: who owns each one, and which ones nobody reviews.

A goal has an owner and, optionally, a department (``models.Goal``). This
module answers the two questions that cut across departments:

* **Who is it on?** ``effective_owner_id``: the goal's own owner, else its
  department's lead, else the principal. The stored owner stays NULL for a
  goal that follows its department, so a new lead takes its goals with them.
* **Who reviews it?** A department with a specialist grades its own goals in
  its check-in. Every other goal (no department, or a department with no
  specialist) is ``unreviewed`` and graded by ``goal_review`` instead, so no
  goal goes unchecked.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openexecutive.departments import store
from openexecutive.departments.models import DepartmentState, Goal

# The specialist that grades goals no department's specialist covers. The
# strategy officer's brief includes OKRs and company-level priorities.
FALLBACK_REVIEWER = "cso"


@dataclass(frozen=True)
class GoalView:
    """A goal with its owner and department resolved for display."""

    goal: Goal
    owner_id: int | None
    owner_name: str
    department_title: str | None


def _principal_id(db_path: Path | None) -> int | None:
    from openexecutive.people import store as people_store

    try:
        person = people_store.find_principal_person(db_path=db_path)
    except Exception:  # noqa: BLE001 - no roster yet reads as "no principal".
        return None
    return person.id if person is not None else None


def effective_owner_id(
    goal: Goal,
    heads: dict[str, int | None],
    principal_id: int | None,
) -> int | None:
    """The goal's own owner, else its department's lead, else the principal."""
    if goal.owner_person_id is not None:
        return goal.owner_person_id
    if goal.department_slug and heads.get(goal.department_slug) is not None:
        return heads[goal.department_slug]
    return principal_id


def list_goal_views(db_path: Path | None = None) -> list[GoalView]:
    """Every goal, oldest first, with its owner's name and department title."""
    from openexecutive.people import store as people_store

    states = store.list_departments(db_path=db_path)
    heads = {s.config.slug: s.config.head_person_id for s in states}
    titles = {s.config.slug: s.config.title for s in states}
    principal_id = _principal_id(db_path)
    names: dict[int, str] = {}
    views: list[GoalView] = []
    for goal in store.list_all_goals(db_path=db_path):
        owner_id = effective_owner_id(goal, heads, principal_id)
        if owner_id is not None and owner_id not in names:
            person = people_store.get_person(owner_id, db_path=db_path)
            # Only a current teammate is named: a contact is private to the
            # principal, and these views reach teammates too.
            on_team = person is not None and not person.archived and person.kind == "team"
            names[owner_id] = person.full_name if on_team and person is not None else ""
        views.append(
            GoalView(
                goal=goal,
                owner_id=owner_id,
                owner_name=names.get(owner_id, "") if owner_id is not None else "",
                department_title=titles.get(goal.department_slug or ""),
            )
        )
    return views


def unreviewed_goals(states: list[DepartmentState], db_path: Path | None = None) -> list[Goal]:
    """Goals no department check-in grades: those with no department, or in a
    department with no specialist."""
    without_specialist = {s.config.slug for s in states if not s.config.specialist_key}
    return [
        g
        for g in store.list_all_goals(db_path=db_path)
        if not g.department_slug
        or g.department_slug in without_specialist
        or g.department_slug not in {s.config.slug for s in states}
    ]
