"""Endpoints for inspecting and cancelling proactive scheduled actions."""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from openexecutive.config import get_settings
from openexecutive.memory.episodic import (
    ScheduledAction,
    cancel_scheduled_action,
    get_scheduled_action,
    list_scheduled_actions,
)

router = APIRouter()
logger = logging.getLogger(__name__)


_VALID_STATUS_FILTERS = {"pending", "running", "done", "failed", "cancelled"}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_ACTOR_LEN = 200


def _signed_in_caller(request: Request) -> str | None:
    """The signed-in user's email when the request came through the UI proxy.

    The proxy (packages/ui/src/app/api/backend/[...path]/route.ts) strips any
    client-sent ``x-caller-*`` header and stamps ``x-caller-email`` from the
    verified sign-in, next to the ``x-api-key`` shared secret. The email is only
    trusted when that secret is configured AND this request carries it: with
    ``BACKEND_SHARED_SECRET`` unset, anyone who can reach the API could write
    the header themselves.
    """
    secret = os.environ.get("BACKEND_SHARED_SECRET", "").strip()
    if not secret:
        return None
    provided = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(provided.encode(), secret.encode()):
        return None
    email = (request.headers.get("x-caller-email") or "").strip().lower()
    return email[:_MAX_ACTOR_LEN] or None


def require_cancel_permission(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> str:
    """Gate cancelling a scheduled action; returns who is cancelling, for the audit row.

    Cancelling only stops something from happening — the same "safe
    direction" that lets any signed-in user pause the Executive
    (routes/executive.py) — so it is allowed for:
    - a matching ``X-Admin-Token`` when SCHEDULED_ADMIN_TOKEN is set (scripts);
    - a signed-in user coming through the UI proxy (``_signed_in_caller``), so
      the Pulse page's Cancel button works on a deployed server, not only
      under `make dev`;
    - a loopback caller when no admin token is configured (`make dev`, curl).

    Anything else fails closed: 401 when an admin token is configured, 503
    otherwise. GETs stay unauthenticated to match the /memories/* routes the
    Pulse page already reads.
    """
    expected = get_settings().scheduled_admin_token
    if (
        expected
        and x_admin_token is not None
        and hmac.compare_digest(x_admin_token.encode(), expected.encode())
    ):
        return "admin-token"
    caller = _signed_in_caller(request)
    if caller:
        return caller
    client_host = request.client.host if request.client else ""
    if not expected and client_host in _LOOPBACK_HOSTS:
        return "local"
    if expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Token",
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Cancelling scheduled actions is disabled for this caller. Set the "
            "same BACKEND_SHARED_SECRET on the API and the UI so signed-in users "
            "can cancel, or SCHEDULED_ADMIN_TOKEN for scripts."
        ),
    )


@router.get(
    "/scheduled",
    response_model=list[ScheduledAction],
)
def list_scheduled(
    status: str = "pending",
    limit: int = 100,
    order: str = "asc",
) -> list[ScheduledAction]:
    if status != "all" and status not in _VALID_STATUS_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"status must be 'all' or one of {sorted(_VALID_STATUS_FILTERS)}",
        )
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    if order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
    return list_scheduled_actions(
        status=None if status == "all" else status,
        limit=limit,
        order=order,
    )


@router.get(
    "/scheduled/{action_id}",
    response_model=ScheduledAction,
)
def get_scheduled(action_id: int) -> ScheduledAction:
    row = get_scheduled_action(action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scheduled action not found")
    return row


@router.delete(
    "/scheduled/{action_id}",
    response_model=ScheduledAction,
)
def cancel_scheduled(
    action_id: int, actor: str = Depends(require_cancel_permission)
) -> ScheduledAction:
    result = cancel_scheduled_action(action_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="scheduled action not found")
    if result == "not_cancellable":
        raise HTTPException(
            status_code=409,
            detail="action is already running, done, failed, or cancelled — cannot cancel",
        )
    row = get_scheduled_action(action_id)
    if row is None:  # pragma: no cover — defensive
        raise HTTPException(status_code=404, detail="scheduled action not found")
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "scheduled_action_cancelled",
        f"Cancelled scheduled {row.kind} #{action_id}: {row.intent_text[:120]}",
        actor=actor,
        details={
            "action_id": action_id,
            "kind": row.kind,
            "channel": row.channel,
            "department": row.department or None,
        },
    )
    return row
