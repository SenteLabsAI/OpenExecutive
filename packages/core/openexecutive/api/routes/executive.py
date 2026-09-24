"""Pause / resume the Executive's autonomous work (scheduler/pause.py).

Behind the global shared-secret middleware like every other route, and
deliberately NOT behind ``SCHEDULED_ADMIN_TOKEN``: the web UI's proxy cannot
send that header, and the switch has to work from any page.

The two directions are authorized differently. **Pause** is the safe
direction — it only holds work — so any signed-in caller may pull the brake.
**Resume** releases every held outbound action at once, so it is the
principal's alone (``people.store.is_principal_or_self``, the same rule the
people routes use). Until a principal is on the roster nobody could pass that
check, so resume is then open rather than leaving a pause no one can lift.

``paused_by`` comes from the ``x-caller-email`` the UI proxy stamps from the
signed-in session; direct API callers fall back to ``"api"``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openexecutive.scheduler import pause as pause_store

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_ACTOR_LEN = 200


class ExecutiveStatus(BaseModel):
    paused: bool
    paused_at: str | None = None
    paused_by: str | None = None
    reason: str | None = None
    # Pending scheduled actions already due — what fires on resume.
    held_actions: int = 0
    # Whether THIS caller may resume (principal-only once one exists).
    can_resume: bool = False


class PauseBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _caller(request: Request) -> str:
    email = (request.headers.get("x-caller-email") or "").strip()
    return email[:_MAX_ACTOR_LEN] or "api"


def _may_resume(request: Request) -> bool:
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.people import store as people_store

    try:
        if people_store.find_principal_person() is None:
            return True
        return people_store.is_principal_or_self(
            _resolve_caller_person_id(request), None
        )
    except Exception:
        # Can't tell who is asking — keep the brake on.
        logger.exception("executive: principal lookup failed — denying resume")
        return False


def _status(request: Request) -> ExecutiveStatus:
    state = pause_store.get_pause_state()
    return ExecutiveStatus(
        **state.model_dump(),
        held_actions=pause_store.count_held_actions(),
        can_resume=_may_resume(request),
    )


@router.get("/executive/status", response_model=ExecutiveStatus)
def get_executive_status(request: Request) -> ExecutiveStatus:
    return _status(request)


@router.post("/executive/pause", response_model=ExecutiveStatus)
def pause_executive(request: Request, body: PauseBody | None = None) -> ExecutiveStatus:
    """Hold all autonomous work. Idempotent — re-pausing keeps the original
    start time and reason."""
    reason = ((body.reason if body else None) or "").strip() or None
    was_paused = pause_store.get_pause_state().paused
    actor = _caller(request)
    pause_store.pause(actor, reason)
    if not was_paused:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "executive_paused",
            "Executive paused — autonomous work on hold"
            + (f": {reason}" if reason else ""),
            actor=actor,
            details={"reason": reason},
        )
    return _status(request)


@router.post("/executive/resume", response_model=ExecutiveStatus)
def resume_executive(request: Request) -> ExecutiveStatus:
    """Release held work. Due actions fire on the next scheduler tick.
    Principal only (see module docstring)."""
    if not _may_resume(request):
        raise HTTPException(status_code=403, detail="Only the principal can resume the Executive")
    prior = pause_store.get_pause_state()
    actor = _caller(request)
    held = pause_store.count_held_actions()
    pause_store.resume(actor)
    if prior.paused:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "executive_resumed",
            f"Executive resumed — {held} held action(s) released",
            actor=actor,
            details={
                "paused_at": prior.paused_at,
                "paused_by": prior.paused_by,
                "held_actions": held,
            },
        )
    return _status(request)
