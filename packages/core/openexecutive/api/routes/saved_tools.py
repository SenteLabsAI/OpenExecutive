"""Saved tools (workflows.saved_tools): the scripts the Executive kept to run
again by name, on the Tools page. The principal's alone, scripts included.

  GET  /saved-tools                     — every saved tool and whether saving is on
  GET  /saved-tools/{name}              — one tool: its script, versions and recent runs
  PUT  /saved-tools/{name}              — {enabled}: turn it on or off
  POST /saved-tools/{name}/rollback     — {version}: make that version the one that runs

The Executive saves tools on its own (approved automatically: a saved tool
can do no more than the chat turn or workflow step that runs it); these
routes are how the owner looks at them and stops or reverts one.
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


class EnabledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=2**31)


def _require_principal(request: Request) -> None:
    from openexecutive.api.routes.people import caller_is_principal

    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can see saved tools.")


def _out(tool: Any) -> SavedToolOut:
    return SavedToolOut(
        name=tool.name, description=tool.description, enabled=tool.enabled, version=tool.version,
        uses_tools=tool.tools, origin=tool.origin, created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


def _detail(name: str) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    tool = saved_tools.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail="No saved tool by that name.")
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


@router.put("/saved-tools/{name}", response_model=SavedToolDetail)
def set_saved_tool_enabled(name: str, body: EnabledIn, request: Request) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    _require_principal(request)
    try:
        tool = saved_tools.set_enabled(name, body.enabled)
    except saved_tools.SavedToolError:
        raise HTTPException(status_code=404, detail="No saved tool by that name.") from None
    _audit(
        f"Saved tool {tool.name} turned {'on' if tool.enabled else 'off'}",
        {"name": tool.name, "enabled": tool.enabled},
    )
    return _detail(name)


@router.post("/saved-tools/{name}/rollback", response_model=SavedToolDetail)
def rollback_saved_tool(name: str, body: RollbackIn, request: Request) -> SavedToolDetail:
    from openexecutive.workflows import saved_tools

    _require_principal(request)
    try:
        tool = saved_tools.rollback(name, body.version)
    except saved_tools.SavedToolError:
        raise HTTPException(status_code=404, detail="No such version of that saved tool.") from None
    _audit(
        f"Saved tool {tool.name} switched to version {tool.version}",
        {"name": tool.name, "version": tool.version},
    )
    return _detail(name)
