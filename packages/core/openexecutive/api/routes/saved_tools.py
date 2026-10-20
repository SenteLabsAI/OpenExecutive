"""Saved tools (workflows.saved_tools): the scripts the Executive kept to run
again by name, shown to people as "Custom tools" (Settings → Advanced).
The principal's alone, scripts included.

  GET  /saved-tools                     — every saved tool and whether saving is on
  GET  /saved-tools/{name}              — one tool: its script, versions and recent runs
  PUT  /saved-tools/{name}              — {enabled?, workflows?}: turn it on or off,
                                          and on or off for workflows
  POST /saved-tools/{name}/rollback     — {version}: make that version the one that runs

A tool is a script (run_script) or a Python job (run_python_job, ``kind``
"python"); Python tools run only in chat, so they never turn on for workflows.

The Executive saves tools on its own (approved automatically: a saved tool
can do no more than the chat turn or workflow step that runs it); these
routes are how the owner looks at them and stops or reverts one. Workflows
run a kept tool only once the owner turns it on for them (``workflows``),
at the version they turned on; that is the one change here that lets more
happen unattended, so it needs a request tied to the owner (signed sign-ins
or local login), as Take the lead's switches do.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class SavedToolOut(BaseModel):
    name: str
    description: str
    enabled: bool
    version: int
    uses_tools: list[str]
    origin: str
    created_at: str
    updated_at: str
    # The version workflows may run, or None: not on for workflows.
    workflow_version: int | None = None
    # "script" (run_script) or "python" (run_python_job, chat only).
    kind: str = "script"


class SavedToolsOut(BaseModel):
    # SAVED_TOOLS_ENABLED: off, nothing is saved and no saved tool runs.
    enabled: bool
    tools: list[SavedToolOut]


class VersionOut(BaseModel):
    version: int
    description: str
    script: str
    uses_tools: list[str]
    origin: str
    created_at: str


class RunOut(BaseModel):
    version: int
    ok: bool
    calls: int
    duration_ms: int
    origin: str
    at: str


class SavedToolDetail(SavedToolOut):
    script: str
    versions: list[VersionOut]
    runs: list[RunOut]


class ToolUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    # True turns `version` (the one the owner looked at) on for workflows;
    # False turns workflows off.
    workflows: bool | None = None
    version: int | None = Field(default=None, ge=1, le=2**31)


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=2**31)


def _require_principal(request: Request) -> None:
    from openexecutive.api.routes.people import caller_is_principal

    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can see custom tools.")


def _out(tool: Any) -> SavedToolOut:
    return SavedToolOut(
        name=tool.name, description=tool.description, enabled=tool.enabled, version=tool.version,
        uses_tools=tool.tools, origin=tool.origin, created_at=tool.created_at,
        updated_at=tool.updated_at, workflow_version=tool.workflow_version, kind=tool.kind,
    )


def _detail(name: str) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    tool = saved_tools.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail="No custom tool by that name.")
    return SavedToolDetail(
        **_out(tool).model_dump(),
        script=tool.script,
        versions=[VersionOut(**v) for v in saved_tools.versions(name)],
        runs=[RunOut(**r) for r in saved_tools.runs(name, limit=20)],
    )


def _audit(summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    try:
        log_event("saved_tool_changed", summary, actor="user", details=details)
    except Exception:
        logger.warning("saved tools: couldn't audit a change", exc_info=True)


@router.get("/saved-tools", response_model=SavedToolsOut)
def list_saved_tools(request: Request) -> SavedToolsOut:
    from openexecutive.config import get_settings
    from openexecutive.workflows import saved_tools

    _require_principal(request)
    return SavedToolsOut(
        enabled=get_settings().saved_tools_enabled,
        tools=[_out(t) for t in saved_tools.list_tools()],
    )


@router.get("/saved-tools/{name}", response_model=SavedToolDetail)
def get_saved_tool(name: str, request: Request) -> SavedToolDetail:
    _require_principal(request)
    return _detail(name)


def _tied_to_the_owner(request: Request) -> bool:
    """Whether this request is provably the owner's (signed sign-ins or local
    login), as Take the lead requires before letting more run on its own."""
    from openexecutive.api.routes.take_the_lead import _principal, _provably_theirs

    return _provably_theirs(request, _principal(request))


@router.put("/saved-tools/{name}", response_model=SavedToolDetail)
def update_saved_tool(name: str, body: ToolUpdate, request: Request) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    _require_principal(request)
    if body.enabled is None and body.workflows is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    if body.workflows and body.version is None:
        raise HTTPException(status_code=422, detail="Say which version to turn on for workflows.")
    if body.workflows and (current := saved_tools.get(name)) is not None and current.kind != "script":
        raise HTTPException(status_code=409, detail="Python tools run only in chat, never in workflows.")
    if body.workflows and not _tied_to_the_owner(request):
        raise HTTPException(
            status_code=409,
            detail="This needs signed sign-ins on this server, so that nobody else can turn it on for you.",
        )
    try:
        tool = None
        if body.enabled is not None:
            tool = saved_tools.set_enabled(name, body.enabled)
            _audit(
                f"Custom tool {tool.name} turned {'on' if tool.enabled else 'off'}",
                {"name": tool.name, "enabled": tool.enabled},
            )
        if body.workflows is not None:
            tool = saved_tools.set_workflows(name, body.version if body.workflows else None)
            _audit(
                f"Custom tool {tool.name} "
                + (f"version {tool.workflow_version} on for workflows" if body.workflows else "off for workflows"),
                {"name": tool.name, "workflow_version": tool.workflow_version},
            )
    except saved_tools.SavedToolError as exc:
        raise HTTPException(status_code=404, detail=f"{str(exc).capitalize()}.") from None
    return _detail(name)


@router.post("/saved-tools/{name}/rollback", response_model=SavedToolDetail)
def rollback_saved_tool(name: str, body: RollbackIn, request: Request) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    _require_principal(request)
    try:
        tool = saved_tools.rollback(name, body.version)
    except saved_tools.SavedToolError:
        raise HTTPException(status_code=404, detail="No such version of that tool.") from None
    _audit(
        f"Custom tool {tool.name} switched to version {tool.version}",
        {"name": tool.name, "version": tool.version},
    )
    return _detail(name)
