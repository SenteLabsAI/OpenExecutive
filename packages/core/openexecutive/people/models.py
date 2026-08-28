"""Pydantic models for the People feature.

A `Person` is a real human (fractional executive, ops manager, bookkeeper,
board chair, or the founding principal) that the Executive can address,
assign work to, wait on, and resume from in Phase 6.

Phase 3 establishes the model, store, registry, and channel helper.
The WaitForHuman workflow primitive and inbound resolver land in Phase 6.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AuthorityScope(StrEnum):
    """Tokens describing what decisions a Person is authorised to approve.

    Checked by the authority gate (Phase 4) when a department proposes an
    action. A Person with WILDCARD approves anything — typically the principal.
    """

    SPEND_LT_2K = "spend_lt_2k"
    SPEND_LT_10K = "spend_lt_10k"
    SPEND_GT_10K = "spend_gt_10k"
    HIRING_SIGNOFF = "hiring_signoff"
    VENDOR_ONBOARDING = "vendor_onboarding"
    CUSTOMER_CREDIT = "customer_credit"
    LEGAL_SIGN = "legal_sign"
    BOARD_COMMS = "board_comms"
    MEETING_SCHEDULING = "meeting_scheduling"
    WILDCARD = "wildcard"


class AvailabilityWindow(BaseModel):
    """A recurring weekly window when a Person can be reached.

    Multiple windows can be configured (e.g. "Tue 9-1 PT" + "Thu 2-4 PT").
    The channel helper uses these to gate outbound delivery: a message to
    a fractional CFO is queued until the next window rather than interrupting.
    """

    weekdays: list[int] = Field(
        default_factory=list,
        description="ISO weekday numbers: 0=Mon … 6=Sun",
    )
    start_local: str = Field(
        description="Window start in local time, HH:MM (24h)",
        pattern=r"^\d{2}:\d{2}$",
    )
    end_local: str = Field(
        description="Window end in local time, HH:MM (24h)",
        pattern=r"^\d{2}:\d{2}$",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA timezone name, e.g. 'America/Los_Angeles'",
    )


PreferredChannel = Literal["email", "slack", "telegram", "discord", "any"]


class Person(BaseModel):
    """A real human who can receive work, approve actions, and respond.

    `is_principal=True` marks the founding user — the fallback approver for
    any action when no delegated Person is matched. Exactly one Person should
    have `is_principal=True`. The API permits only the canonical principal
    (identified by the UI proxy's verified email) to mutate the roster, and
    refuses a second principal or principal archival. The canonical principal
    email is bound at onboarding and immutable through the public API.

    `department_slugs` is advisory — it tells the Executive which department
    contexts this person participates in. FK enforcement deferred to Phase 4.
    """

    id: int | None = None
    full_name: str
    role: str = ""
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: PreferredChannel = "any"
    availability: list[AvailabilityWindow] = Field(default_factory=list)
    authority_scope: list[AuthorityScope] = Field(default_factory=list)
    response_sla_hours: int = 24
    on_leave_until: date | None = None
    reports_to_person_id: int | None = None
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
