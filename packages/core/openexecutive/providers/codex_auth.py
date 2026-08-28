"""Managed ChatGPT subscription login through OpenAI's official Codex SDK.

This module intentionally does not implement OAuth or handle tokens itself.
``openai-codex`` starts the official Codex App Server over stdio; App Server
owns device authorization, credential persistence, and refresh.  The manager
below only keeps the live login handle needed to report/cancel an in-progress
device-code flow.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

CodexAuthState = Literal[
    "unavailable", "disconnected", "pending", "connected", "error"
]


class CodexAuthStatus(BaseModel):
    state: CodexAuthState
    auth_mode: str | None = None
    email: str | None = None
    plan_type: str | None = None
    login_id: str | None = None
    verification_url: str | None = None
    user_code: str | None = None
    error: str | None = None


class CodexDeviceLogin(BaseModel):
    login_id: str
    verification_url: str
    user_code: str


class CodexAuthError(RuntimeError):
    """Base class for safe, expected connection-flow failures."""


class CodexAuthConflict(CodexAuthError):
    """A login is already active or Codex is already authenticated."""


class CodexAuthUnavailable(CodexAuthError):
    """The official Codex runtime could not be initialized."""


class CodexNoActiveLogin(CodexAuthError):
    """Cancellation was requested without an active device-code flow."""


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if isinstance(value, Enum) or isinstance(enum_value, str):
        return str(enum_value)
    return str(value)


def _codex_child_environment(codex_home: str) -> dict[str, str]:
    """Override inherited secret variables before SDK launches App Server."""
    environment = {"CODEX_HOME": codex_home}
    sensitive_suffixes = (
        "_API_KEY",
        "_SECRET",
        "_TOKEN",
        "_PASSWORD",
        "_EMAIL",
        "_EMAIL_ADDRESS",
    )
    sensitive_names = {"DATABASE_URL", "EMAIL_ADDRESS", "PRINCIPAL_EMAIL"}
    for name in os.environ:
        if name.upper().endswith(sensitive_suffixes) or name.upper() in sensitive_names:
            environment[name] = ""
    return environment


def _official_codex_client() -> Any:
    # Imported lazily so merely importing the API does not start Codex or make
    # test collection depend on a platform runtime binary.
    from openai_codex import AsyncCodex, CodexConfig

    from openexecutive.config import get_settings

    settings = get_settings()
    codex_home = settings.codex_home_path or (
        settings.company_profile_path.parent / ".codex"
    )
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Do not inherit a developer's ~/.codex login. This dedicated directory is
    # also the persistence seam hardened/configured by the credential step.
    codex_home.chmod(0o700)

    return AsyncCodex(
        CodexConfig(
            client_name="open_executive",
            client_title="Open Executive",
            client_version="0.1.0",
            env=_codex_child_environment(str(codex_home)),
            # Device login/account APIs are stable. Keep experimental RPCs off.
            experimental_api=False,
        )
    )


class CodexAuthManager:
    """One-process owner of the official Codex App Server auth session."""

    def __init__(self, client_factory: Callable[[], Any] = _official_codex_client) -> None:
        self._client_factory = client_factory
        self._client: Any = None
        self._active_login: Any = None
        self._login_task: asyncio.Task[None] | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            client = self._client_factory()
            await client.__aenter__()
        except Exception as exc:
            logger.exception("Codex App Server initialization failed")
            raise CodexAuthUnavailable(
                "Codex is unavailable. Check the server logs and runtime installation."
            ) from exc
        self._client = client
        # A fresh App Server is a fresh attempt; do not surface an old device
        # login failure after transport recovery succeeds.
        self._last_error = None
        return client

    async def _discard_client(self) -> None:
        """Drop a failed App Server so the next request can restart it."""
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    @staticmethod
    def _connected_status(account_response: Any) -> CodexAuthStatus | None:
        account = getattr(account_response, "account", None)
        if account is None:
            return None
        root = getattr(account, "root", account)
        auth_mode = _enum_value(getattr(root, "type", None))
        return CodexAuthStatus(
            state="connected",
            auth_mode=auth_mode,
            email=getattr(root, "email", None),
            plan_type=_enum_value(getattr(root, "plan_type", None)),
        )

    async def status(self) -> CodexAuthStatus:
        async with self._lock:
            if self._active_login is not None:
                return CodexAuthStatus(
                    state="pending",
                    login_id=self._active_login.login_id,
                    verification_url=self._active_login.verification_url,
                    user_code=self._active_login.user_code,
                )
            try:
                client = await self._get_client()
                account_response = await client.account()
            except CodexAuthUnavailable as exc:
                return CodexAuthStatus(state="unavailable", error=str(exc))
            except Exception:
                logger.exception("Codex account status check failed")
                await self._discard_client()
                return CodexAuthStatus(
                    state="error",
                    error="Could not read Codex account status. Check the server logs.",
                )

            connected = self._connected_status(account_response)
            if connected is not None:
                self._last_error = None
                return connected
            if self._last_error:
                return CodexAuthStatus(state="error", error=self._last_error)
            return CodexAuthStatus(state="disconnected")

    async def start_device_login(self) -> CodexDeviceLogin:
        async with self._lock:
            if self._active_login is not None:
                raise CodexAuthConflict("A Codex login is already in progress.")

            client = await self._get_client()
            try:
                connected = self._connected_status(await client.account())
                if connected is not None:
                    raise CodexAuthConflict("Codex is already authenticated.")
                handle = await client.login_chatgpt_device_code()
            except CodexAuthConflict:
                raise
            except Exception as exc:
                logger.exception("Could not start Codex device-code login")
                await self._discard_client()
                raise CodexAuthUnavailable(
                    "Could not start ChatGPT sign-in. Check the server logs."
                ) from exc

            self._active_login = handle
            self._last_error = None
            self._login_task = asyncio.create_task(
                self._watch_login(handle), name="codex-device-login"
            )
            logger.info("Codex device-code login started")
            return CodexDeviceLogin(
                login_id=handle.login_id,
                verification_url=handle.verification_url,
                user_code=handle.user_code,
            )

    async def _watch_login(self, handle: Any) -> None:
        watcher_failed = False
        try:
            completed = await handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex device-code login watcher failed")
            success = False
            watcher_failed = True
        else:
            success = bool(getattr(completed, "success", False))
            if not success:
                logger.warning(
                    "Codex device-code login failed: %s",
                    getattr(completed, "error", None) or "unknown error",
                )

        async with self._lock:
            # A cancelled flow may already have been replaced by a newer login.
            if self._active_login is not handle:
                return
            self._active_login = None
            self._login_task = None
            self._last_error = (
                None if success else "ChatGPT sign-in did not complete. Please try again."
            )
            if watcher_failed:
                await self._discard_client()
        if success:
            logger.info("Codex device-code login completed")

    async def cancel_device_login(self) -> str:
        async with self._lock:
            handle = self._active_login
            if handle is None:
                raise CodexNoActiveLogin("No Codex login is in progress.")
            try:
                response = await handle.cancel()
            except Exception as exc:
                # A transport failure cannot leave a pending login blocking a
                # retry. Drop both the App Server and its watcher; a later
                # status/start creates a fresh official client.
                task = self._login_task
                self._active_login = None
                self._login_task = None
                self._last_error = None
                await self._discard_client()
                if task is not None and not task.done():
                    task.cancel()
                logger.exception("Could not cancel Codex device-code login")
                raise CodexAuthUnavailable(
                    "Could not cancel ChatGPT sign-in. Check the server logs."
                ) from exc
            task = self._login_task
            self._active_login = None
            self._login_task = None
            self._last_error = None
            if task is not None and not task.done():
                task.cancel()
            status = _enum_value(getattr(response, "status", None)) or "canceled"
            logger.info("Codex device-code login cancellation result: %s", status)
            return status

    async def close(self) -> None:
        async with self._lock:
            client = self._client
            task = self._login_task
            self._client = None
            self._active_login = None
            self._login_task = None
            self._last_error = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.exception("Codex App Server shutdown failed")
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


_manager = CodexAuthManager()


def get_codex_auth_manager() -> CodexAuthManager:
    return _manager


async def close_codex_auth_manager() -> None:
    await _manager.close()
