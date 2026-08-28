"""Principal-only ChatGPT subscription connection endpoints.

OAuth stays inside OpenAI's official Codex App Server. These routes expose only
the device-code ceremony and connection state; access/refresh tokens never pass
through FastAPI or the browser.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openexecutive.api.authorization import PrincipalOnly
from openexecutive.providers.codex_auth import (
    CodexAuthConflict,
    CodexAuthStatus,
    CodexAuthUnavailable,
    CodexDeviceLogin,
    CodexNoActiveLogin,
    get_codex_auth_manager,
)

router = APIRouter(prefix="/codex/auth")


class CodexCancelResponse(BaseModel):
    status: str


@router.get("/status", response_model=CodexAuthStatus)
async def codex_auth_status(_: PrincipalOnly) -> CodexAuthStatus:
    return await get_codex_auth_manager().status()


@router.post("/device/start", response_model=CodexDeviceLogin)
async def start_codex_device_login(_: PrincipalOnly) -> CodexDeviceLogin:
    try:
        return await get_codex_auth_manager().start_device_login()
    except CodexAuthConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CodexAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/device/cancel", response_model=CodexCancelResponse)
async def cancel_codex_device_login(_: PrincipalOnly) -> CodexCancelResponse:
    try:
        result = await get_codex_auth_manager().cancel_device_login()
    except CodexNoActiveLogin as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CodexAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return CodexCancelResponse(status=result)
