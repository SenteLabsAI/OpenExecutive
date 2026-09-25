"""Workspace settings: solo / team mode, the user's time zone, and the
principal's role (what they do — solo mode reads it; see
``memory.workspace_settings.PrincipalRole``).

``GET /workspace`` is open to any caller. ``PUT /workspace`` changes how the
whole install behaves — solo mode cancels every department check-in, and a
new zone re-times the principal's briefs — so it is the principal's alone,
by the same rule as resuming the Executive
(``chat._caller_is_principal_or_unclaimed``): a caller that resolves to the
principal (a request with no ``x-caller-email`` — the CLI, direct curl, local
login — does), or anyone while no principal is on the roster yet, so
first-run setup can choose a mode before onboarding has created one.

The principal's role (who they report to, what they are measured on) is
theirs: ``GET`` returns it only to a caller that ``PUT`` would let through,
and reads it back as nulls for anyone else (a teammate on a team install).

Both routes work before onboarding: nothing here needs a company profile.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from openexecutive.api.models import WorkspaceResponse, WorkspaceUpdateRequest
from openexecutive.memory import workspace_settings

router = APIRouter()


def _response(*, with_role: bool = True) -> WorkspaceResponse:
    ws = workspace_settings.get_workspace()
    role = ws.principal_role() if with_role else workspace_settings.PrincipalRole()
    return WorkspaceResponse(
        mode=ws.mode,
        timezone=ws.timezone,
        effective_timezone=workspace_settings.get_user_timezone().key,
        **role.model_dump(),
    )


@router.get("/workspace", response_model=WorkspaceResponse)
def get_workspace(request: Request) -> WorkspaceResponse:
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    return _response(with_role=_caller_is_principal_or_unclaimed(request))


@router.put("/workspace", response_model=WorkspaceResponse)
def update_workspace(request: Request, body: WorkspaceUpdateRequest) -> WorkspaceResponse:
    """Change the fields present in the body; returns the new settings."""
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    if not _caller_is_principal_or_unclaimed(request):
        raise HTTPException(
            status_code=403, detail="Only the principal can change workspace settings"
        )
    before = _response()
    sent = body.model_fields_set
    if "timezone" in sent and body.timezone != before.timezone:
        workspace_settings.set_timezone(body.timezone)
    if "mode" in sent and body.mode is not None and body.mode != before.mode:
        workspace_settings.set_workspace_mode(body.mode)
    role_update = {
        f: getattr(body, f)
        for f in workspace_settings.ROLE_FIELDS
        if f in sent and getattr(body, f) != getattr(before, f)
    }
    if role_update:
        workspace_settings.set_principal_role(**role_update)

    after = _response()
    mode_or_zone_changed = (after.mode, after.timezone) != (before.mode, before.timezone)
    # Names only: the role is the principal's own free text (and may name
    # their manager), so the audit trail records which fields changed, not
    # what they say.
    role_changed = sorted(
        f for f in workspace_settings.ROLE_FIELDS if getattr(after, f) != getattr(before, f)
    )
    if mode_or_zone_changed or role_changed:
        from openexecutive.audit import log_event as audit_log

        caller = (request.headers.get("x-caller-email") or "").strip()[:200] or "api"
        details: dict[str, object] = {
            "mode": {"from": before.mode, "to": after.mode},
            "timezone": {"from": before.timezone, "to": after.timezone},
        }
        parts: list[str] = []
        if mode_or_zone_changed:
            parts.append(
                f"mode {before.mode} → {after.mode}, "
                f"time zone {before.timezone or 'default'} → {after.timezone or 'default'}"
            )
        if role_changed:
            details["role_fields_changed"] = role_changed
            parts.append("principal's role updated")
        audit_log(
            "workspace_settings_changed",
            "Workspace settings changed: " + ", ".join(parts),
            actor=caller,
            details=details,
        )
    return after
