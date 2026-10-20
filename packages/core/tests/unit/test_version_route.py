"""GET /version reports the running version and whether a newer release is out."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import version as version_route

# Patching swaps the attribute on the httpx module itself, so keep the real one.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.delenv("UPDATE_CHECK_ENABLED", raising=False)
    version_route._reset_cache()
    yield
    version_route._reset_cache()


def _client(current: str = "0.4.4") -> TestClient:
    app = FastAPI(version=current)
    app.include_router(version_route.router)
    return TestClient(app)


def _github(
    monkeypatch: pytest.MonkeyPatch, tag: object, status: int = 200, body: object = None
) -> list[str]:
    """Answer GitHub's latest-release call with `tag`; returns the URLs asked."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            status,
            json=body
            if body is not None
            else {"tag_name": tag, "html_url": "https://evil.example/x"},
        )

    def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(version_route.httpx, "AsyncClient", fake_client)
    return calls


def test_newer_release_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _github(monkeypatch, "v0.5.0")
    body = _client("0.4.4").get("/version").json()
    assert body == {
        "current": "0.4.4",
        "latest": "0.5.0",
        "update_available": True,
        "release_url": "https://github.com/SenteLabsAI/OpenExecutive/releases/tag/v0.5.0",
        "check_enabled": True,
    }
    assert calls == ["https://api.github.com/repos/SenteLabsAI/OpenExecutive/releases/latest"]


@pytest.mark.parametrize("tag", ["v0.4.4", "v0.4.3", "0.4.4"])
def test_same_or_older_release_is_no_update(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    _github(monkeypatch, tag)
    body = _client("0.4.4").get("/version").json()
    assert body["update_available"] is False


def test_versions_compare_numerically(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, "v0.10.0")
    assert _client("0.9.9").get("/version").json()["update_available"] is True


@pytest.mark.parametrize("tag", ["nightly", "v1.0.0-rc.1", None, 7, "v1.0.0/../../x"])
def test_unparseable_tag_gives_no_latest(monkeypatch: pytest.MonkeyPatch, tag: object) -> None:
    _github(monkeypatch, tag)
    body = _client().get("/version").json()
    assert body["latest"] is None
    assert body["release_url"] is None
    assert body["update_available"] is False


def test_github_failure_degrades_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    _github(monkeypatch, "v0.5.0", status=503)
    resp = _client().get("/version")
    assert resp.status_code == 200
    assert resp.json()["latest"] is None
    assert resp.json()["update_available"] is False


def test_answer_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _github(monkeypatch, "v0.5.0")
    client = _client()
    client.get("/version")
    client.get("/version")
    assert len(calls) == 1


def test_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _github(monkeypatch, "v0.5.0")
    clock = [1000.0]
    monkeypatch.setattr(version_route.time, "monotonic", lambda: clock[0])
    client = _client()
    client.get("/version")
    clock[0] += version_route._SUCCESS_TTL_S + 1
    client.get("/version")
    assert len(calls) == 2


def test_last_good_answer_survives_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(version_route.time, "monotonic", lambda: clock[0])
    _github(monkeypatch, "v0.5.0")
    client = _client()
    client.get("/version")
    clock[0] += version_route._SUCCESS_TTL_S + 1
    calls = _github(monkeypatch, "v0.5.0", status=500)
    assert client.get("/version").json()["latest"] == "0.5.0"
    # A failure is retried after the shorter failure TTL, not on every load.
    client.get("/version")
    assert len(calls) == 1


def test_disabled_check_skips_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPDATE_CHECK_ENABLED", "false")
    calls = _github(monkeypatch, "v0.5.0")
    body = _client("0.4.4").get("/version").json()
    assert body == {
        "current": "0.4.4",
        "latest": None,
        "update_available": False,
        "release_url": None,
        "check_enabled": False,
    }
    assert calls == []


@pytest.mark.parametrize("body", [[], "v0.5.0", 5])
def test_non_object_answer_is_a_cached_miss(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    calls = _github(monkeypatch, None, body=body)
    client = _client()
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json()["latest"] is None
    client.get("/version")
    assert len(calls) == 1
