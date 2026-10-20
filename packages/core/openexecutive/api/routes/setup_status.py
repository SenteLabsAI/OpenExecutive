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


class SetupStatusResponse(BaseModel):
    checked_at: str
    checks: list[SetupCheck]


class _Runs:
    """Kept on ``app.state``: one app's last answer and its run in flight."""

    def __init__(self) -> None:
        self.last: tuple[float, SetupStatusResponse] | None = None
        self.current: asyncio.Task[SetupStatusResponse] | None = None


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
    state = request.app.state
    runs: _Runs | None = getattr(state, "setup_status_runs", None)
    if runs is None:
        runs = state.setup_status_runs = _Runs()
    if runs.last is not None and time.monotonic() - runs.last[0] < _REUSE_FOR_S:
        return runs.last[1]
    current = runs.current
    if current is None or current.done() or current.get_loop() is not asyncio.get_running_loop():
        current = runs.current = asyncio.create_task(_run_all(state))
    # Shielded: a caller that gives up must not cancel the run others await.
    result = await asyncio.shield(current)
    runs.last = (time.monotonic(), result)
    return result
