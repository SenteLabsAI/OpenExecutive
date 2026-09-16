"""Unit tests for the Open Executive MCP server (openexecutive.mcp_server).

All LLM/DB-touching service functions are mocked — these tests exercise the
wiring (registration, handler serialization, the specialist-enum contract, the
mount, and the shared-secret gate), not the underlying subsystems.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.mcp_server import server as mcp_server


class _Dumpable:
    """Stand-in for a Pydantic model: only ``model_dump`` is exercised."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self._payload


# ---------------------------------------------------------------------------
# Contracts: specialist enum + registration.
# ---------------------------------------------------------------------------
def test_specialist_enum_matches_registry() -> None:
    """The Literal advertised to clients must equal the live roster, or callers
    get an enum that drifts from what route_to_specialist accepts."""
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert set(mcp_server.specialist_keys()) == set(SPECIALIST_REGISTRY)


@pytest.mark.asyncio
async def test_registered_tools() -> None:
    tools = await mcp_server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "consult_specialist",
        "search_knowledge",
        "list_workflows",
        "ask_executive",
    }


@pytest.mark.asyncio
async def test_registered_resources() -> None:
    resources = await mcp_server.mcp.list_resources()
    assert {str(r.uri) for r in resources} == {
        "oe://company/profile",
        "oe://today/briefing",
        "oe://today/activity",
        "oe://people/roster",
        "oe://departments/state",
        "oe://memory/decisions",
        "oe://memory/initiatives",
        "oe://memory/advice",
    }


# ---------------------------------------------------------------------------
# Resource handlers.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_company_profile_resource_empty() -> None:
    fake = _Dumpable({})
    fake.is_empty = lambda: True  # type: ignore[attr-defined]
    with patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=fake,
    ):
        out = await mcp_server.company_profile()
    assert "No company profile" in out


@pytest.mark.asyncio
async def test_company_profile_resource_populated() -> None:
    fake = _Dumpable({})
    fake.is_empty = lambda: False  # type: ignore[attr-defined]
    fake.to_prompt_block = lambda: "ACME Corp — SaaS"  # type: ignore[attr-defined]
    with patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=fake,
    ):
        out = await mcp_server.company_profile()
    assert out == "ACME Corp — SaaS"


@pytest.mark.asyncio
async def test_people_roster_resource_serializes_models() -> None:
    people = [_Dumpable({"id": 1, "full_name": "Alice"})]
    with patch("openexecutive.people.store.list_people", return_value=people):
        out = await mcp_server.people_roster()
    assert json.loads(out) == [{"id": 1, "full_name": "Alice"}]


@pytest.mark.asyncio
async def test_today_briefing_resource_serializes_model() -> None:
    with patch(
        "openexecutive.api.routes.today._build_today",
        return_value=_Dumpable({"departments": [], "people": []}),
    ):
        out = await mcp_server.today_briefing()
    assert json.loads(out) == {"departments": [], "people": []}


@pytest.mark.asyncio
async def test_today_activity_resource_serializes_model() -> None:
    with patch(
        "openexecutive.api.routes.today._build_activity",
        return_value=_Dumpable({"items": []}),
    ) as build:
        out = await mcp_server.today_activity()
    assert json.loads(out) == {"items": []}
    build.assert_called_once_with(20)


@pytest.mark.asyncio
async def test_memory_decisions_resource_serializes_list() -> None:
    with patch(
        "openexecutive.memory.episodic.list_decisions",
        return_value=[_Dumpable({"id": 7, "title": "Pricing"})],
    ):
        out = await mcp_server.memory_decisions()
    assert json.loads(out) == [{"id": 7, "title": "Pricing"}]


# ---------------------------------------------------------------------------
# Tool handlers.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consult_specialist_routes_to_specialist() -> None:
    mock = AsyncMock(return_value="CFO analysis")
    with patch("openexecutive.orchestrator.router.route_to_specialist", new=mock):
        out = await mcp_server.consult_specialist("cfo", "runway?", "context here")
    assert out == "CFO analysis"
    mock.assert_awaited_once_with(
        "cfo", "runway?", context="context here", actor="specialist_mcp",
    )


@pytest.mark.asyncio
async def test_consult_specialist_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown specialist"):
        await mcp_server.consult_specialist("bogus", "q")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_knowledge_uses_retriever_and_store() -> None:
    mcp_server.set_store("STORE_SENTINEL")
    try:
        with patch(
            "openexecutive.knowledge.retriever.retrieve",
            return_value="[doc] chunk",
        ) as ret:
            out = await mcp_server.search_knowledge("unit economics")
        assert out == "[doc] chunk"
        ret.assert_called_once_with(query="unit economics", store="STORE_SENTINEL")
    finally:
        mcp_server.set_store(None)


@pytest.mark.asyncio
async def test_list_workflows_returns_catalog() -> None:
    out = await mcp_server.list_workflows()
    data = json.loads(out)
    assert isinstance(data, list) and data
    assert all("name" in w for w in data)


@pytest.mark.asyncio
async def test_ask_executive_resolves_principal_and_calls_chat() -> None:
    profile = _Dumpable({})
    profile.is_empty = lambda: True  # type: ignore[attr-defined]

    class _Person:
        id = 42

    chat = AsyncMock(return_value="Executive answer")
    with (
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            return_value=profile,
        ),
        patch(
            "openexecutive.people.store.find_principal_person",
            return_value=_Person(),
        ),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
        patch("openexecutive.orchestrator.executive.Executive.chat", new=chat),
    ):
        out = await mcp_server.ask_executive("What's our top risk?")
    assert out == "Executive answer"
    assert chat.await_args.kwargs["person_id"] == 42


# ---------------------------------------------------------------------------
# Mount + auth.
# ---------------------------------------------------------------------------
def test_mount_adds_mcp_route() -> None:
    app = FastAPI()
    mcp_server.mount(app)
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)


def test_mcp_path_is_not_unauthenticated() -> None:
    """The /mcp endpoint must stay behind the shared-secret gate."""
    from openexecutive.api import main

    assert "/mcp" not in main._UNAUTHENTICATED_PATHS


def test_mcp_endpoint_is_gated_by_shared_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With BACKEND_SHARED_SECRET set, /mcp returns 401 without the header and
    passes the gate (no 401) with it. Routing only — lifespan is not run."""
    from openexecutive.api.main import create_app

    monkeypatch.setenv("BACKEND_SHARED_SECRET", "testsecret")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    app = create_app()
    client = TestClient(app)  # not a context manager → lifespan does not run

    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream"}

    no_key = client.post("/mcp", json=body, headers=headers, follow_redirects=False)
    assert no_key.status_code == 401

    with_key = client.post(
        "/mcp",
        json=body,
        headers={**headers, "x-api-key": "testsecret"},
        follow_redirects=False,
    )
    assert with_key.status_code != 401
