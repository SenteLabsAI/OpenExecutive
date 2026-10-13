"""The fail-closed guard on OE_PUBLIC_DEPLOYMENT.

An internet-reachable instance with no BACKEND_SHARED_SECRET serves an
unauthenticated API. Deployments declare themselves public by setting
OE_PUBLIC_DEPLOYMENT, and create_app() then refuses to boot without the secret
rather than logging a warning and carrying on.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from openexecutive.api.main import _cors_origins, _is_public_deployment, create_app


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    monkeypatch.delenv("BACKEND_ALLOWED_ORIGINS", raising=False)


def test_public_deployment_without_secret_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")

    with pytest.raises(RuntimeError, match="BACKEND_SHARED_SECRET is required"):
        create_app()


def test_public_deployment_with_secret_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s" * 32)

    assert create_app() is not None


def test_local_dev_without_secret_still_boots() -> None:
    """The unset default stays permissive — `make dev` must keep working."""
    assert create_app() is not None


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off", "  "])
def test_falsey_values_do_not_arm_the_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", value)

    assert _is_public_deployment() is False
    assert create_app() is not None


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "production"])
def test_truthy_and_unrecognised_values_arm_the_guard(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An unrecognised value must arm the guard, not silently skip it — for a
    fail-closed check the safe default is to require the secret."""
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", value)

    assert _is_public_deployment() is True
    with pytest.raises(RuntimeError, match="BACKEND_SHARED_SECRET is required"):
        create_app()


def test_local_dev_allows_the_dev_ui_origin() -> None:
    assert _cors_origins() == ["http://localhost:3000", "http://127.0.0.1:3000"]


def test_public_deployment_drops_localhost_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentialed CORS for localhost on a public instance would let whatever
    a visitor runs on localhost:3000 read the API's answers."""
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s" * 32)
    monkeypatch.setenv("BACKEND_ALLOWED_ORIGINS", "https://exec.example.com, ,https://exec.other.com")

    assert _cors_origins() == ["https://exec.example.com", "https://exec.other.com"]

    client = TestClient(create_app())
    preflight = {"Access-Control-Request-Method": "GET"}
    refused = client.options("/health", headers={"Origin": "http://localhost:3000", **preflight})
    assert "access-control-allow-origin" not in refused.headers
    allowed = client.options("/health", headers={"Origin": "https://exec.example.com", **preflight})
    assert allowed.headers["access-control-allow-origin"] == "https://exec.example.com"
    assert allowed.headers["access-control-allow-credentials"] == "true"
