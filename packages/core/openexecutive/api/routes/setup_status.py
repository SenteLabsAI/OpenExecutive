"""``GET /setup/status`` — a green/amber/red light per part of the install.

The checks live in ``api/setup_checks.py``; this module runs them and shares
one run between requests that arrive close together.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from openexecutive.api.setup_checks import (
    PROBE_TIMEOUT_S,
    SetupCheck,
    gather_snapshot,
    run_checks,
)

router = APIRouter()

# A run calls Anthropic, Slack, Discord and Telegram. Requests inside this
# window get the last run's answer, and requests during a run wait for that
# run, so a held-down "Check again" (or a script) can't turn into a stream of
# calls to those services. Settings only change on a restart, so a few
# seconds' reuse hides nothing.
_REUSE_FOR_S = 5.0

_last_run: tuple[float, SetupStatusResponse] | None = None
_current_run: asyncio.Task[SetupStatusResponse] | None = None


class SetupStatusResponse(BaseModel):
    checked_at: str
    checks: list[SetupCheck]


async def _run_all(app_state: Any) -> SetupStatusResponse:
    # Imported here: api.main imports this module while it is being built.
    from openexecutive.api.main import _is_local_login
    from openexecutive.config import get_settings

    snap = await asyncio.to_thread(
        gather_snapshot, get_settings(), local_login=_is_local_login(), app_state=app_state
    )
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as http:
        checks = await run_checks(snap, http)
    return SetupStatusResponse(checked_at=snap.now.isoformat(), checks=checks)


@router.get("/setup/status", response_model=SetupStatusResponse)
async def setup_status(request: Request) -> SetupStatusResponse:
    global _last_run, _current_run
    if _last_run is not None and time.monotonic() - _last_run[0] < _REUSE_FOR_S:
        return _last_run[1]
    if _current_run is None or _current_run.done():
        _current_run = asyncio.create_task(_run_all(request.app.state))
    # Shielded: a caller that gives up must not cancel the run others await.
    result = await asyncio.shield(_current_run)
    _last_run = (time.monotonic(), result)
    return result
