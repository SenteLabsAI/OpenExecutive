"""First read of the free-text description (/onboard/interview/understand)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import onboarding as route
from openexecutive.onboarding import interview as iv
from openexecutive.onboarding import understand as un

SECRET = "ARR-4242-SECRET"


def _tool_response(data: dict[str, Any], name: str = un.TOOL_NAME) -> Any:
    block = SimpleNamespace(type="tool_use", name=name, input=data)
    return SimpleNamespace(content=[block], usage=None)


class _Provider:
    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response, self.exc = response, exc
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    prov = _Provider()
    monkeypatch.setattr("openexecutive.providers.registry.get_provider", lambda m: prov)
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    return prov


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


async def test_understand_reads_the_fields_and_trims(provider: _Provider) -> None:
    provider.response = _tool_response(
        {
            "mode": "solo",
            "role_kind": "in_house",
            "role_title": "  Head of Customer Success ",
            "reports_to": "COO",
            "company": "Northwind Software, about 300 people",
            "focus": "",
            "ignored": "x",
        }
    )
    got = await un.understand("I'm Head of Customer Success at Northwind.")
    assert got.mode == "solo" and got.role_kind == "in_house"
    assert got.role_title == "Head of Customer Success"
    assert got.focus is None
    call = provider.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": un.TOOL_NAME}
    # One-off call: nothing dynamic rides in a cached system block.
    assert "cache_control" not in call["system"][0]


async def test_understand_leaves_unknowns_null(provider: _Provider) -> None:
    provider.response = _tool_response({})
    got = await un.understand("I run a bakery.")
    assert got == un.Understanding()


async def test_understand_rejects_a_bad_enum(provider: _Provider) -> None:
    provider.response = _tool_response({"mode": "everyone"})
    with pytest.raises(iv.InterviewError):
        await un.understand("hello")


async def test_understand_provider_error_is_input_free(provider: _Provider) -> None:
    provider.exc = RuntimeError(SECRET)
    with pytest.raises(iv.InterviewError) as err:
        await un.understand("hello")
    assert SECRET not in str(err.value)


def test_route_returns_the_understanding(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def _fake(text: str) -> un.Understanding:
        seen.append(text)
        return un.Understanding(mode="team", company="Northwind Tools")

    monkeypatch.setattr(un, "understand", _fake)
    resp = client.post("/onboard/interview/understand", data={"description": "We sell tools."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "team" and body["company"] == "Northwind Tools"
    assert body["role_kind"] is None
    assert seen == ["We sell tools."]


def test_route_includes_attached_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def _fake(text: str) -> un.Understanding:
        seen.append(text)
        return un.Understanding()

    monkeypatch.setattr(un, "understand", _fake)
    resp = client.post(
        "/onboard/interview/understand",
        data={"description": "About me"},
        files=[("files", ("one-pager.txt", b"We sell tools", "text/plain"))],
    )
    assert resp.status_code == 200, resp.text
    assert "=== Attached: one-pager.txt ===" in seen[0] and "We sell tools" in seen[0]


def test_route_empty_is_422(client: TestClient) -> None:
    assert client.post("/onboard/interview/understand", data={"description": " "}).status_code == 422


@pytest.mark.parametrize(
    ("exc", "status"),
    [(iv.InterviewError("nope"), 502), (iv.InterviewTimeout("slow"), 504)],
)
def test_route_maps_failures_without_echoing_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, exc: Exception, status: int
) -> None:
    async def _boom(text: str) -> un.Understanding:
        raise exc

    monkeypatch.setattr(un, "understand", _boom)
    resp = client.post("/onboard/interview/understand", data={"description": SECRET})
    assert resp.status_code == status
    assert SECRET not in resp.text
