"""Authority gate for department-scoped proactive actions.

The gate is consulted whenever a proactive action (scheduled or cadence)
has a department slug — it reads the department's authority_level and
decides whether the action should execute immediately, be proposed to an
approver, or be escalated to an approver as urgent. Only `execute` runs the
action; `propose` and `escalate` both wait for a person to approve it.

All callers are expected to pass `now` explicitly so the gate is
deterministic and easy to test without time mocking.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from openexecutive.departments import registry as dept_registry
from openexecutive.departments.models import AuthorityLevel
from openexecutive.people.models import AuthorityScope

logger = logging.getLogger(__name__)


class GateDecision(BaseModel):
    """Outcome of the authority gate check."""

    allowed: bool
    action: Literal["execute", "propose", "escalate"]
    assignee_person_id: int | None = None
    deliver_at: datetime | None = None
    reason: str = ""


def gate_action(
    department_slug: str,
    action_kind: str,
    *,
    required_scope: AuthorityScope | None = None,
    has_user_consent: bool = False,
    now: datetime,
) -> GateDecision:
    """Decide how a proposed action should proceed.

    Returns a GateDecision describing:
    - execute  — run the action immediately (auto_execute level or has consent)
    - propose  — surface to an approver; do not execute now
    - escalate — surface to an approver as urgent, now; do not execute until
                 they approve (the level promises "the specialist will not act")

    `deliver_at` is set when the best matching approver is outside their
    availability window — a `propose` caller should reschedule for that time
    instead of surfacing it immediately; an escalation does not wait.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    # Unconditional fast-path: explicit user consent bypasses authority checks.
    if has_user_consent:
        return GateDecision(
            allowed=True,
            action="execute",
            reason="user_consent",
        )

    state = dept_registry.get_state(department_slug)
    if state is None:
        logger.warning(
            "authority_gate: unknown department %r — defaulting to propose", department_slug
        )
        return _route_proposal(
            department_slug=department_slug,
            action=cast_action("propose"),
            required_scope=required_scope,
            now=now,
            reason=f"unknown department {department_slug!r}",
        )

    level = state.config.authority_level

    if level == AuthorityLevel.AUTO_EXECUTE:
        return GateDecision(
            allowed=True,
            action="execute",
            reason=f"department {department_slug!r} has authority_level=auto_execute",
        )

    if level == AuthorityLevel.PROPOSE_ONLY:
        return _route_proposal(
            department_slug=department_slug,
            action="propose",
            required_scope=required_scope,
            now=now,
            reason=f"department {department_slug!r} has authority_level=propose_only",
        )

    # ESCALATE: route to an approver as urgent; nothing executes until they approve.
    return _route_proposal(
        department_slug=department_slug,
        action="escalate",
        required_scope=required_scope,
        now=now,
        reason=f"department {department_slug!r} has authority_level=escalate",
    )


def _route_proposal(
    *,
    department_slug: str,
    action: Literal["propose", "escalate"],
    required_scope: AuthorityScope | None,
    now: datetime,
    reason: str,
) -> GateDecision:
    """Find the best approver and compute their next availability window."""
    from openexecutive.people import registry as people_registry
    from openexecutive.people.channel import next_available_window
    from openexecutive.people.store import find_approvers

    scope = required_scope or AuthorityScope.WILDCARD
    approvers = find_approvers(scope)

    if not approvers:
        # Fall back to principal (wildcard).
        principal = people_registry.get_principal()
        if principal is None:
            logger.warning(
                "authority_gate: no approvers and no principal for dept=%r scope=%r",
                department_slug, scope,
            )
            return GateDecision(
                allowed=False,
                action=action,
                assignee_person_id=None,
                deliver_at=None,
                reason=f"{reason}; no approvers found, no principal configured",
            )
        approvers = [principal]

    assignee = approvers[0]
    assert assignee.id is not None  # find_approvers only returns rows with id

    # Check if the assignee is reachable RIGHT NOW. `next_available_window`
    # scans forward in 15-min steps — it always returns a future time even
    # when we're already inside a window. Guard with an in-window check so
    # we only defer when the person is genuinely unavailable.
    from openexecutive.people.channel import _in_window
    in_window_now = (
        not assignee.availability  # no windows = always reachable
        or any(_in_window(win, now) for win in assignee.availability)
    )
    deliver_at = None if in_window_now else next_available_window(assignee.id, after=now)

    return GateDecision(
        allowed=False,
        action=action,
        assignee_person_id=assignee.id,
        deliver_at=deliver_at,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Alert helper
# ---------------------------------------------------------------------------

def propose_via_alert(
    department_slug: str,
    person_id: int,
    summary: str,
    body: str,
    suggested_action: str = "",
    extra_tags: list[str] | None = None,
    *,
    external_id_suffix: str = "",
) -> int | None:
    """Persist a proposal as an alert routed to a specific Person.

    Returns the alert id, or None if a duplicate was suppressed.

    topic_tags carries both department and person identifiers so the UI
    and future resolvers can filter/match without parsing the body;
    ``extra_tags`` lets a caller carry the originating alert's tags (e.g.
    a ``comp`` / ``legal`` marker) onto the proposal.

    ``external_id_suffix`` turns the card into a recurring one: an open
    (unread) card with the same dedup key is refreshed in place with the new
    body — whatever its suffix, so one situation never has two open cards —
    and once the person has acted on it a fresh card is minted only when
    the suffix changes (callers pass e.g. the ISO week). Without a suffix
    the proposal is one-shot and a repeat with the same summary is
    suppressed for good. Coalescing keeps the open card's routing: a head
    change re-routes it only once the current card has been handled.
    """
    from openexecutive.alerts.store import coalesce_alert, insert_alert

    topic_tags = [f"department:{department_slug}", f"person:{person_id}"]
    for tag in extra_tags or []:
        if tag not in topic_tags:
            topic_tags.append(tag)
    dedup_key = f"proposal:{department_slug}:{person_id}:{summary[:60]}"
    external_id = f"{dedup_key}:{external_id_suffix}" if external_id_suffix else dedup_key

    try:
        if external_id_suffix and coalesce_alert(
            source="authority_gate", dedup_key=dedup_key, severity="medium", body=body,
        ):
            return None
        return insert_alert(
            source="authority_gate",
            external_id=external_id,
            severity="medium",
            headline=summary,
            body=body,
            suggested_action=suggested_action,
            topic_tags=topic_tags,
            dedup_key=dedup_key,
            routed_to_person_id=person_id,
        )
    except Exception:
        logger.exception(
            "authority_gate: propose_via_alert failed for dept=%r person=%d",
            department_slug, person_id,
        )
        return None


def cast_action(a: str) -> Literal["propose", "escalate"]:
    """Narrow an untyped string to the propose/escalate literal."""
    if a == "escalate":
        return "escalate"
    return "propose"
