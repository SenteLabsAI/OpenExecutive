"""Pydantic models for the Departments feature.

A `Department` is a persistent wrapper around an existing stateless
`BaseAgent` specialist. The wrapper owns charter, Goals, cadences, and an
authority level; the agent itself stays stateless. People-related fields
(`head_person_id`, member ids) are reserved here but not exercised until
Phase 3 lands the People model.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AuthorityLevel(StrEnum):
    """How autonomously a department may take proactive action."""

    AUTO_EXECUTE = "auto_execute"
    PROPOSE_ONLY = "propose_only"
    ESCALATE = "escalate"


GoalStatus = Literal["on_track", "at_risk", "off_track"]
PeriodType = Literal["week", "month", "quarter", "year", "ongoing"]


class Goal(BaseModel):
    """A single Goal row for one department, scoped to a period.

    `period_type` says how to interpret `period_value`:
      • week → e.g. "Week of May 18"
      • month → "May 2026"
      • quarter → "Q2 2026"
      • year → "2026"
      • ongoing → display-only, period_value typically "Ongoing"
    """

    id: int | None = None
    department_slug: str
    period_type: PeriodType = "quarter"
    period_value: str
    key_result: str
    target: str
    current: str = ""
    status: GoalStatus = "on_track"
    created_at: str = ""
    updated_at: str = ""
    # When OE last graded this Goal (via the department_check_in workflow
    # or, in Phase B, a chat-driven `update_department_goal` tool call).
    # Distinct from `updated_at`, which bumps on any content edit — a
    # principal editing the key_result on Tuesday and OE reviewing it on
    # Wednesday must be distinguishable so the UI can render "Last
    # reviewed Nd ago" honestly. Empty string = never reviewed.
    last_reviewed_at: str = ""


class DepartmentCharter(BaseModel):
    """Static charter text — mission + scope boundaries."""

    mission: str
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class DepartmentConfig(BaseModel):
    """Department metadata: identity, charter, authority, cadences.

    `head_person_id` and `head_persona_slug` are reserved but unused in
    Phase 1; they wire up in Phases 3 (People) and 6 (per-head persona).
    """

    slug: str
    title: str
    # Key into orchestrator.router.SPECIALIST_REGISTRY. Nullable: some org
    # structures (nonprofits, SMBs) have departments that do not map to one
    # of the specialist agents — e.g. "Volunteer Coordination", "Family
    # Services". Those departments render as informational rows in the UI
    # and are skipped by specialist-routing workflows.
    specialist_key: str | None = None
    charter: DepartmentCharter
    authority_level: AuthorityLevel = AuthorityLevel.PROPOSE_ONLY
    head_person_id: int | None = None
    head_persona_slug: str | None = None
    # cadence_name -> spec, e.g. {"check_in": "weekly@mon@09:00"}; an empty spec
    # means the check-in is off. Parsed by departments/cadence.py.
    cadences: dict[str, str] = Field(default_factory=dict)
    # Department-scoped broadcast channels. When set, OE can post to the
    # department's team room via `send_department_message` instead of (or
    # in addition to) DMing the department head. Nullable — a department
    # with no channel configured simply has no broadcast surface, and OE
    # falls back to DMing the head (or the company channel) per the
    # "Choosing Who to Tell" judgment in the persona.
    slack_channel_id: str | None = None
    discord_channel_id: str | None = None
    telegram_chat_id: str | None = None
    # Named external entities this department wants monitored (vendors it
    # is evaluating, competitors in its lane, tickers it tracks). The
    # research watch policy treats these as strong grounding — like the
    # company profile's `vendors` / `tickers` — so a proposal about one of
    # them can be added to the watchlist directly and routed to this
    # department (and its head) instead of waiting on the principal.
    watched_entities: list[str] = Field(default_factory=list)


class DepartmentState(BaseModel):
    """Department config + its current Goals + financial sketch.

    Returned by the read API and rendered into the Executive prompt block
    (Phase 2). `member_person_ids` is populated in Phase 3.
    """

    config: DepartmentConfig
    goals: list[Goal] = Field(default_factory=list)
    headcount: int | None = None
    budget_usd: float | None = None
    member_person_ids: list[int] = Field(default_factory=list)
    updated_at: str = ""
