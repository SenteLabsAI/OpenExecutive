"""Local login's gate on the API.

`make dev` without Google sign-in runs the UI with no password, guarded by
where requests come from; the API gets the same guard. It usually has no
shared secret, and a request with no x-caller-email runs as the principal, so
without the gate a page in the owner's browser could drive it: by
DNS-rebinding its own hostname to 127.0.0.1, or by posting a plain form from
any other site or localhost port.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from openexecutive.api.main import _is_local_login, create_app

_HOST_ERROR = "local login only accepts requests addressed to this computer"
_CROSS_SITE_ERROR = "cross-site request refused"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    # Settings also reads the repo .env, so pin the value rather than delete it.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")


def _request(
    method: str, host: str, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    # The Host header is set directly: TestClient cannot parse an IPv6 base_url.
    return TestClient(create_app()).request(
        method, path, headers={"host": host, **(headers or {})}
    )


# ── addressed to this computer ───────────────────────────────────────────────


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST"])
def test_requests_addressed_to_this_computer_pass(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert _request("GET", host, "/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example:8000", "localhost.evil.example:8000", "192.168.1.20:8000"]
)
def test_a_rebinding_hostname_is_refused(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("GET", host, "/sessions")
    assert resp.status_code == 403
    assert resp.json() == {"error": _HOST_ERROR}


def test_google_chat_verifies_its_caller_and_may_come_through_a_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("POST", "abc.tunnel.example", "/webhook/google-chat")
    assert _HOST_ERROR not in resp.text


def test_telegram_passes_under_another_name_only_when_it_checks_its_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without TELEGRAM_WEBHOOK_SECRET the webhook accepts anyone's update, so
    a rebinding page could forge one; with it, the tunnel name is fine."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("POST", "abc.tunnel.example", "/webhook/telegram")
    assert resp.status_code == 403
    assert resp.json() == {"error": _HOST_ERROR}

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    resp = _request("POST", "abc.tunnel.example", "/webhook/telegram")
    assert _HOST_ERROR not in resp.text


# ── writes another page made the browser send ────────────────────────────────


@pytest.mark.parametrize("site", ["same-site", "cross-site", "Same-Site"])
def test_a_form_posted_from_another_page_is_refused(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("POST", "localhost:8000", "/sessions", {"sec-fetch-site": site})
    assert resp.status_code == 403
    assert resp.json() == {"error": _CROSS_SITE_ERROR}


@pytest.mark.parametrize("headers", [{}, {"sec-fetch-site": "same-origin"}, {"sec-fetch-site": "none"}])
def test_the_proxy_the_cli_and_same_origin_pages_may_write(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """The UI proxy and the CLI send no Sec-Fetch-Site; the API's own pages
    (e.g. /docs) are same-origin. They reach the route, which answers 405 here."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("POST", "localhost:8000", "/sessions", headers)
    assert resp.status_code == 405


def test_reads_are_not_refused_by_the_cross_site_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    resp = _request("GET", "localhost:8000", "/openapi.json", {"sec-fetch-site": "cross-site"})
    assert resp.status_code == 200


# ── when the gate is off ─────────────────────────────────────────────────────


def test_the_gate_is_off_outside_local_login() -> None:
    """Google-mode `make dev`, compose and tests keep reaching the API by any name."""
    assert _is_local_login() is False
    assert _request("GET", "api:8000", "/openapi.json").status_code == 200
    assert _request("POST", "api:8000", "/sessions", {"sec-fetch-site": "cross-site"}).status_code == 405


def test_a_public_deployment_never_runs_local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s" * 32)
    assert _is_local_login() is False
    resp = _request("GET", "exec.example.com", "/openapi.json", {"x-api-key": "s" * 32})
    assert resp.status_code == 200
