"""Departments package — persistent state layer for the specialist agents.

See `plans/lets-plan-this-out-twinkling-crystal.md` for the full feature shape.
Phase 1 ships only the model + store + registry + API; downstream phases
(prompt injection, authority gate, cadences, People) layer on top.
"""
from __future__ import annotations

from openexecutive.departments.models import (
    AuthorityLevel,
    DepartmentCharter,
    DepartmentConfig,
    DepartmentState,
    Goal,
    GoalStatus,
    PeriodType,
)
from openexecutive.departments.registry import (
    get_state,
    invalidate,
    list_configs,
    list_states,
    slug_for_specialist,
    specialist_for_slug,
)

__all__ = [
    "AuthorityLevel",
    "DepartmentCharter",
    "DepartmentConfig",
    "DepartmentState",
    "Goal",
    "GoalStatus",
    "PeriodType",
    "get_state",
    "invalidate",
    "list_configs",
    "list_states",
    "slug_for_specialist",
    "specialist_for_slug",
]
