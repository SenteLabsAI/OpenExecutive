from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api import authorization
from openexecutive.api.routes import codex_auth as route
from openexecutive.providers.codex_auth import (
    CodexAuthConflict,
    CodexAuthStatus,
    CodexAuthUnavailable,
    CodexDeviceLogin,
    CodexNoActiveLogin,
)


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _principal(monkeypatch: pytest.MonkeyPatch, *, is_principal: bool = True) -> None:
    monkeypatch.setattr(
        authorization,
        "find_person_by_email",
        lambda _email: SimpleNamespace(id=1, is_principal=is_principal),
    )
    monkeypatch.setattr(
        authorization,
        "find_principal_person",
        lambda: SimpleNamespace(id=1) if is_principal else None,
    )



def test_status_requires_verified_principal_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SimpleNamespace(status=AsyncMock(return_value=CodexAuthStatus(state="disconnected")))
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)

    assert client.get("/codex/auth/status").status_code == 403

    _principal(monkeypatch, is_principal=False)
    response = client.get(
        "/codex/auth/status", headers={"X-Caller-Email": "member@example.com"}
    )
    assert response.status_code == 403
    assert client.post(
        "/codex/auth/device/start",
        headers={"X-Caller-Email": "member@example.com"},
    ).status_code == 403
    assert client.post(
        "/codex/auth/device/cancel",
        headers={"X-Caller-Email": "member@example.com"},
    ).status_code == 403
    manager.status.assert_not_awaited()


def test_configured_principal_recovers_legacy_unbound_record(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authorization, "find_person_by_email", lambda _email: None)
    monkeypatch.setattr(authorization, "find_principal_person", lambda: SimpleNamespace(id=1))
    monkeypatch.setenv("PRINCIPAL_EMAIL", "owner@example.com")
    manager = SimpleNamespace(status=AsyncMock(return_value=CodexAuthStatus(state="disconnected")))
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)

    assert client.get(
        "/codex/auth/status", headers={"X-Caller-Email": "owner@example.com"}
    ).status_code == 200


def test_principal_can_read_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _principal(monkeypatch)
    manager = SimpleNamespace(
        status=AsyncMock(
            return_value=CodexAuthStatus(
                state="connected",
                auth_mode="chatgpt",
                email="principal@example.com",
                plan_type="plus",
            )
        )
    )
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)

    response = client.get(
        "/codex/auth/status", headers={"X-Caller-Email": "principal@example.com"}
    )
    assert response.status_code == 200
    assert response.json()["state"] == "connected"
    assert response.json()["plan_type"] == "plus"


def test_principal_can_start_and_cancel_device_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _principal(monkeypatch)
    manager = SimpleNamespace(
        start_device_login=AsyncMock(
            return_value=CodexDeviceLogin(
                login_id="login-1",
                verification_url="https://auth.openai.test/codex/device",
                user_code="ABCD-1234",
            )
        ),
        cancel_device_login=AsyncMock(return_value="canceled"),
    )
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)
    headers = {"X-Caller-Email": "principal@example.com"}

    started = client.post("/codex/auth/device/start", headers=headers)
    assert started.status_code == 200
    assert started.json()["user_code"] == "ABCD-1234"

    cancelled = client.post("/codex/auth/device/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "canceled"}


def test_expected_flow_conflicts_return_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _principal(monkeypatch)
    manager = SimpleNamespace(
        start_device_login=AsyncMock(
            side_effect=CodexAuthConflict("A Codex login is already in progress.")
        ),
        cancel_device_login=AsyncMock(
            side_effect=CodexNoActiveLogin("No Codex login is in progress.")
        ),
    )
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)
    headers = {"X-Caller-Email": "principal@example.com"}

    assert client.post("/codex/auth/device/start", headers=headers).status_code == 409
    assert client.post("/codex/auth/device/cancel", headers=headers).status_code == 409


def test_runtime_failures_return_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _principal(monkeypatch)
    manager = SimpleNamespace(
        start_device_login=AsyncMock(
            side_effect=CodexAuthUnavailable("Codex is unavailable.")
        ),
        cancel_device_login=AsyncMock(
            side_effect=CodexAuthUnavailable("Codex is unavailable.")
        ),
    )
    monkeypatch.setattr(route, "get_codex_auth_manager", lambda: manager)
    headers = {"X-Caller-Email": "principal@example.com"}

    assert client.post("/codex/auth/device/start", headers=headers).status_code == 503
    assert client.post("/codex/auth/device/cancel", headers=headers).status_code == 503
