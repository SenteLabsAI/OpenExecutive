"""Workspace settings: solo / team mode and the user's time zone.

``GET /workspace`` is open to any caller. ``PUT /workspace`` changes how the
whole install behaves — solo mode cancels every department check-in, and a
new zone re-times the principal's briefs — so it is the principal's alone,
by the same rule as resuming the Executive
(``chat._caller_is_principal_or_unclaimed``): a caller that resolves to the
principal (a request with no ``x-caller-email`` — the CLI, direct curl, local
login — does), or anyone while no principal is on the roster yet, so
first-run setup can choose a mode before onboarding has created one.

Both routes work before onboarding: nothing here needs a company profile.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from openexecutive.api.models import WorkspaceResponse, WorkspaceUpdateRequest
from openexecutive.memory import workspace_settings

router = APIRouter()


def _response() -> WorkspaceResponse:
    ws = workspace_settings.get_workspace()
    return WorkspaceResponse(
        mode=ws.mode,
        timezone=ws.timezone,
        effective_timezone=workspace_settings.get_user_timezone().key,
    )


@router.get("/workspace", response_model=WorkspaceResponse)
def get_workspace() -> WorkspaceResponse:
    return _response()


@router.put("/workspace", response_model=WorkspaceResponse)
def update_workspace(request: Request, body: WorkspaceUpdateRequest) -> WorkspaceResponse:
    """Change the fields present in the body; returns the new settings."""
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    if not _caller_is_principal_or_unclaimed(request):
        raise HTTPException(
            status_code=403, detail="Only the principal can change workspace settings"
        )
    before = workspace_settings.get_workspace()
    sent = body.model_fields_set
    if "timezone" in sent and body.timezone != before.timezone:
        workspace_settings.set_timezone(body.timezone)
    if "mode" in sent and body.mode is not None and body.mode != before.mode:
        workspace_settings.set_workspace_mode(body.mode)

    after = _response()
    if (after.mode, after.timezone) != (before.mode, before.timezone):
        from openexecutive.audit import log_event as audit_log

        caller = (request.headers.get("x-caller-email") or "").strip()[:200] or "api"
        audit_log(
            "workspace_settings_changed",
            f"Workspace settings changed: mode {before.mode} → {after.mode}, "
            f"time zone {before.timezone or 'default'} → {after.timezone or 'default'}",
            actor=caller,
            details={
                "mode": {"from": before.mode, "to": after.mode},
                "timezone": {"from": before.timezone, "to": after.timezone},
            },
        )
    return after
