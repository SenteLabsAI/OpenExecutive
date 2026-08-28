from __future__ import annotations

import asyncio
import contextlib
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.providers.codex_auth import (
    CodexAuthConflict,
    CodexAuthManager,
    CodexAuthUnavailable,
    CodexNoActiveLogin,
    _official_codex_client,
)


class _CancelStatus(Enum):
    canceled = "canceled"


class _FakeLogin:
    login_id = "login-1"
    verification_url = "https://auth.openai.test/codex/device"
    user_code = "ABCD-1234"

    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.completed = asyncio.Event()
        self.success = True

    async def wait(self) -> Any:
        await self.completed.wait()
        if self.success:
            self.client.account_value = SimpleNamespace(
                root=SimpleNamespace(
                    type="chatgpt",
                    email="principal@example.com",
                    plan_type=SimpleNamespace(value="plus"),
                )
            )
        return SimpleNamespace(success=self.success, error=None)

    async def cancel(self) -> Any:
        self.success = False
        self.completed.set()
        return SimpleNamespace(status=_CancelStatus.canceled)


class _FakeClient:
    def __init__(self) -> None:
        self.account_value: Any = None
        self.login = _FakeLogin(self)
        self.entered = False
        self.closed = False

    async def __aenter__(self) -> _FakeClient:
        self.entered = True
        return self

    async def account(self) -> Any:
        account = (
            SimpleNamespace(root=self.account_value.root)
            if self.account_value is not None
            else None
        )
        return SimpleNamespace(account=account)

    async def login_chatgpt_device_code(self) -> _FakeLogin:
        return self.login

    async def close(self) -> None:
        self.closed = True


def test_official_client_uses_dedicated_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from openexecutive import config as config_module

    captured: dict[str, Any] = {}

    class _FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _FakeCodex:
        def __init__(self, config: Any) -> None:
            self.config = config

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(AsyncCodex=_FakeCodex, CodexConfig=_FakeConfig),
    )
    profile = tmp_path / "company" / "profile.yaml"
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(codex_home_path=None, company_profile_path=profile),
    )

    monkeypatch.setenv("BACKEND_SHARED_SECRET", "do-not-forward")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    _official_codex_client()

    expected = profile.parent / ".codex"
    assert captured["env"]["CODEX_HOME"] == str(expected)
    assert captured["env"]["BACKEND_SHARED_SECRET"] == ""
    assert captured["env"]["EXEC_EMAIL_ADDRESS"] == ""
    assert expected.is_dir()
    assert expected.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_device_login_moves_pending_to_connected() -> None:
    client = _FakeClient()
    manager = CodexAuthManager(lambda: client)

    started = await manager.start_device_login()
    assert started.login_id == "login-1"
    assert (await manager.status()).state == "pending"

    login_task = manager._login_task
    assert login_task is not None
    client.login.completed.set()
    await asyncio.wait_for(login_task, timeout=1)

    status = await manager.status()
    assert status.state == "connected"
    assert status.auth_mode == "chatgpt"
    assert status.email == "principal@example.com"
    assert status.plan_type == "plus"
    await manager.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_duplicate_login_is_rejected() -> None:
    manager = CodexAuthManager(lambda: _FakeClient())
    await manager.start_device_login()
    with pytest.raises(CodexAuthConflict, match="already in progress"):
        await manager.start_device_login()
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_requires_active_login_and_clears_pending_state() -> None:
    client = _FakeClient()
    manager = CodexAuthManager(lambda: client)
    with pytest.raises(CodexNoActiveLogin):
        await manager.cancel_device_login()

    await manager.start_device_login()
    login_task = manager._login_task
    assert login_task is not None
    assert await manager.cancel_device_login() == "canceled"
    with contextlib.suppress(asyncio.CancelledError):
        await login_task
    assert (await manager.status()).state == "disconnected"
    await manager.close()


@pytest.mark.asyncio
async def test_failed_cancel_discards_pending_client_and_allows_retry() -> None:
    class _BrokenCancelLogin(_FakeLogin):
        async def cancel(self) -> Any:
            raise RuntimeError("transport closed")

    class _BrokenCancelClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.login = _BrokenCancelLogin(self)

    failed_client = _BrokenCancelClient()
    replacement = _FakeClient()
    clients = iter([failed_client, replacement])
    manager = CodexAuthManager(lambda: next(clients))

    await manager.start_device_login()
    with pytest.raises(CodexAuthUnavailable, match="Could not cancel"):
        await manager.cancel_device_login()

    assert manager._active_login is None
    assert failed_client.closed is True
    assert (await manager.status()).state == "disconnected"
    assert replacement.entered is True
    await manager.close()


@pytest.mark.asyncio
async def test_dead_runtime_is_discarded_and_restarted_on_next_status() -> None:
    class _DeadClient(_FakeClient):
        async def account(self) -> Any:
            raise RuntimeError("transport closed")

    dead = _DeadClient()
    replacement = _FakeClient()
    clients = iter([dead, replacement])
    manager = CodexAuthManager(lambda: next(clients))

    assert (await manager.status()).state == "error"
    assert dead.closed is True
    assert (await manager.status()).state == "disconnected"
    assert replacement.entered is True
    await manager.close()


@pytest.mark.asyncio
async def test_close_clears_login_error_before_a_fresh_client_starts() -> None:
    failed = _FakeClient()
    replacement = _FakeClient()
    clients = iter([failed, replacement])
    manager = CodexAuthManager(lambda: next(clients))

    await manager.start_device_login()
    login_task = manager._login_task
    assert login_task is not None
    failed.login.success = False
    failed.login.completed.set()
    await login_task
    assert (await manager.status()).state == "error"

    await manager.close()
    assert (await manager.status()).state == "disconnected"
    assert replacement.entered is True
    await manager.close()


@pytest.mark.asyncio
async def test_unavailable_runtime_is_reported_without_raising_from_status() -> None:
    class _BrokenClient:
        async def __aenter__(self) -> None:
            raise FileNotFoundError("codex missing")

    manager = CodexAuthManager(lambda: _BrokenClient())
    status = await manager.status()
    assert status.state == "unavailable"
    assert status.error is not None

    with pytest.raises(CodexAuthUnavailable):
        await manager.start_device_login()
