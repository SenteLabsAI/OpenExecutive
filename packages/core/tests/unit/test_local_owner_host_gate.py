"""One-person mode's loopback Host gate on the API.

`make dev` without Google sign-in runs the UI with no password, guarded by
where requests come from; the API gets the same guard, because a page that
DNS-rebinds its own hostname to 127.0.0.1 would otherwise reach it
same-origin, and a request with no x-caller-email runs as the principal.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from openexecutive.api.main import _is_local_owner_mode, create_app

_GATE_ERROR = "one-person mode only accepts requests addressed to this computer"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OE_LOCAL_OWNER_MODE", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)


def _get(host: str, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    # The Host header is set directly: TestClient cannot parse an IPv6 base_url.
    return TestClient(create_app()).get(path, headers={"host": host, **(headers or {})})


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST"])
def test_requests_addressed_to_this_computer_pass(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("OE_LOCAL_OWNER_MODE", "1")
    assert _get(host, "/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example:8000", "localhost.evil.example:8000", "192.168.1.20:8000"]
)
def test_a_rebinding_hostname_is_refused(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("OE_LOCAL_OWNER_MODE", "1")
    resp = _get(host, "/sessions")
    assert resp.status_code == 403
    assert resp.json() == {"error": _GATE_ERROR}


def test_webhooks_verify_themselves_and_are_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Telegram webhook may arrive through a tunnel under the tunnel's own name."""
    monkeypatch.setenv("OE_LOCAL_OWNER_MODE", "1")
    resp = TestClient(create_app()).post(
        "/webhook/telegram", json={}, headers={"host": "abc.tunnel.example"}
    )
    assert _GATE_ERROR not in resp.text


def test_the_gate_is_off_outside_one_person_mode() -> None:
    """Google-mode `make dev`, compose and tests keep reaching the API by any name."""
    assert _is_local_owner_mode() is False
    assert _get("api:8000", "/openapi.json").status_code == 200


def test_a_public_deployment_never_runs_in_one_person_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OE_LOCAL_OWNER_MODE", "1")
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s" * 32)
    assert _is_local_owner_mode() is False
    resp = _get("exec.example.com", "/openapi.json", headers={"x-api-key": "s" * 32})
    assert resp.status_code == 200
