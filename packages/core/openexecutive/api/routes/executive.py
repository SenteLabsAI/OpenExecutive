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

Also here: the owner's safety level, "When should I check with you?"
(``orchestrator.outside_mode``), with any channel set apart from it, which
only the principal may change.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openexecutive.api import caller as api_caller
from openexecutive.config import get_settings
from openexecutive.scheduler import pause as pause_store

logger = logging.getLogger(__name__)

router = APIRouter()


class ExecutiveStatus(BaseModel):
    paused: bool
    paused_at: str | None = None
    paused_by: str | None = None
    reason: str | None = None
    # Pending scheduled actions already due — what fires on resume.
    held_actions: int = 0
    # Whether THIS caller may resume (principal-only once one exists).
    can_resume: bool = False
    # What the Executive calls itself (EXEC_DISPLAY_NAME): the label over its
    # replies in the web chat.
    name: str = ""


class PauseBody(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _may_resume(request: Request) -> bool:
    # Can't tell who is asking → False, so the brake stays on.
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    return _caller_is_principal_or_unclaimed(request)


def _status(request: Request) -> ExecutiveStatus:
    state = pause_store.get_pause_state()
    return ExecutiveStatus(
        **state.model_dump(),
        held_actions=pause_store.count_held_actions(),
        can_resume=_may_resume(request),
        name=get_settings().exec_display_name.strip(),
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
    actor = api_caller.actor(request)
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
    actor = api_caller.actor(request)
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


class OutsideContentOut(BaseModel):
    # The company's level: "ask" (Careful), "outsiders" (Balanced) or "trust"
    # (Hands-off): orchestrator.outside_mode.
    mode: str
    # Channels set apart from it: channel -> mode. Any other follows ``mode``.
    channels: dict[str, str] = Field(default_factory=dict)
    # The channels a level can be set on (outside_mode.CHANNELS), in order.
    channel_ids: list[str] = Field(default_factory=list)
    # Whether THIS caller may change it: the principal only.
    can_change: bool = False


class OutsideContentIn(BaseModel):
    mode: Literal["ask", "outsiders", "trust"]
    # Channel changes: a mode sets the channel apart, null makes it follow
    # ``mode`` again. Channels left out keep what they have.
    channels: dict[str, Literal["ask", "outsiders", "trust"] | None] | None = None


def _outside_out(*, can_change: bool) -> OutsideContentOut:
    from openexecutive.orchestrator import outside_mode

    return OutsideContentOut(
        mode=outside_mode.get_mode(),
        channels=outside_mode.stored_channels(),
        channel_ids=list(outside_mode.CHANNELS),
        can_change=can_change,
    )


def _is_principal(request: Request) -> bool:
    from openexecutive.api.routes.people import caller_is_principal

    return caller_is_principal(request)


@router.get("/executive/outside-content", response_model=OutsideContentOut)
def get_outside_content(request: Request) -> OutsideContentOut:
    """The safety level: when a read of someone else's text holds what the
    Executive does next (orchestrator.outside_mode), and the channels set
    apart from it."""
    return _outside_out(can_change=_is_principal(request))


@router.put("/executive/outside-content", response_model=OutsideContentOut)
def set_outside_content(request: Request, body: OutsideContentIn) -> OutsideContentOut:
    """Change it. The principal only: it decides how far text from outside
    the company can steer their Executive."""
    from openexecutive.orchestrator import outside_mode

    if not _is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can change this.")
    unknown = sorted(set(body.channels or {}) - set(outside_mode.CHANNELS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown channel: {', '.join(unknown)}")
    before = outside_mode.get_mode()
    before_channels = outside_mode.stored_channels()
    actor = api_caller.actor(request)
    outside_mode.set_mode(body.mode, updated_by=actor, channels=dict(body.channels or {}))
    after_channels = outside_mode.stored_channels()
    if before != body.mode or before_channels != after_channels:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "outside_content_mode_changed",
            f"Safety level: {before} -> {body.mode}",
            actor=actor,
            details={
                "before": before,
                "mode": body.mode,
                "channels_before": before_channels,
                "channels": after_channels,
            },
        )
    return _outside_out(can_change=True)
