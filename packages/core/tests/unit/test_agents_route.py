"""HTTP-level tests for the Agent Council API."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.api.routes import agents as agents_route  # noqa: E402


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "agents.db"
    monkeypatch.setattr(ov_mod, "DB_PATH", db)
    ov_mod.invalidate_cache()
    app = FastAPI()
    app.include_router(agents_route.router)
    yield TestClient(app)
    ov_mod.invalidate_cache()


def test_list_models_returns_allowed_models(client: TestClient) -> None:
    res = client.get("/agents/models")
    assert res.status_code == 200
    assert "claude-opus-4-7" in res.json()
    assert "claude-sonnet-5" in res.json()
    assert "claude-sonnet-4-6" in res.json()


def test_list_agents_returns_all_specialists(client: TestClient) -> None:
    res = client.get("/agents")
    assert res.status_code == 200
    body = res.json()
    names = {a["name"] for a in body}
    # At minimum these should exist; the registry currently has 9 specialists + executive.
    assert {"executive", "cso", "cfo", "chro", "cmo", "cpo", "coo", "gc", "board_comms", "triage"} <= names
    all_names = [a["name"] for a in body]
    assert len(all_names) == len(set(all_names)), f"duplicate agents in /agents: {all_names}"
    for a in body:
        assert a["has_override"] is False  # fresh DB


def test_executive_detail_has_expected_fields(client: TestClient) -> None:
    res = client.get("/agents/executive")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "executive"
    assert body["role"] == "Executive"
    assert body["has_override"] is False
    assert body["prompt_default"]  # non-empty default prompt


def test_get_agent_404_for_unknown(client: TestClient) -> None:
    assert client.get("/agents/not_a_real_agent").status_code == 404


def test_patch_then_get_reflects_override(client: TestClient) -> None:
    res = client.patch("/agents/cso", json={"role": "Test CSO"})
    assert res.status_code == 200
    detail = res.json()
    assert detail["role"] == "Test CSO"
    assert detail["role_default"] != "Test CSO"
    assert detail["has_override"] is True
    assert "role" in detail["overridden_fields"]


def test_patch_rejects_unknown_model(client: TestClient) -> None:
    res = client.patch("/agents/cso", json={"model": "claude-not-real"})
    assert res.status_code == 400


def test_patch_rejects_openrouter_model_when_openrouter_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``openai/gpt-5`` is a known OpenRouter slug, but with
    ``OPENROUTER_ENABLED=false`` it is NOT in the runtime's allowed list
    — the runtime has no backend that can serve it, so the API rejects
    the override at PATCH time rather than 500-ing on first chat call."""
    # The default config in conftest leaves OPENROUTER_ENABLED unset (False).
    res = client.patch("/agents/cso", json={"model": "openai/gpt-5"})
    assert res.status_code == 400
    assert "allowed" in res.json()["detail"].lower()


def test_patch_accepts_openrouter_model_when_openrouter_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``OPENROUTER_ENABLED=true`` the curated OpenRouter slugs are
    allowed values — operators can flip an agent to GPT-5 via the
    Council UI without a code change."""
    # Patch the registry's settings reader so allowed_models() folds the
    # OpenRouter set in. We can't toggle a real Settings() instance from
    # inside the test process because pydantic-settings caches behavior.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: SimpleNamespace(
            anthropic_api_key="sk-test",
            openrouter_enabled=True,
            openrouter_api_key="sk-or-test",
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_app_title="Open Executive",
            openrouter_referer=None,
            openrouter_timeout_s=180.0,
        ),
    )
    res = client.patch("/agents/cso", json={"model": "openai/gpt-5"})
    # 200 means the model passed the allowlist; the override was persisted.
    assert res.status_code == 200, res.text
    assert res.json()["model"] == "openai/gpt-5"


def test_reset_clears_override(client: TestClient) -> None:
    client.patch("/agents/cmo", json={"role": "Custom"})
    assert client.get("/agents/cmo").json()["has_override"] is True
    res = client.delete("/agents/cmo/override")
    assert res.status_code == 204
    assert client.get("/agents/cmo").json()["has_override"] is False


def test_reset_404_for_unknown_agent(client: TestClient) -> None:
    assert client.delete("/agents/no_such/override").status_code == 404


def test_history_then_rollback(client: TestClient) -> None:
    client.patch("/agents/cfo", json={"role": "v1"})
    client.patch("/agents/cfo", json={"role": "v2"})
    history = client.get("/agents/cfo/history").json()
    assert len(history) >= 1
    target = next(h for h in history if h["role"] == "v1")
    res = client.post(f"/agents/cfo/rollback/{target['id']}")
    assert res.status_code == 200
    assert client.get("/agents/cfo").json()["role"] == "v1"


def test_rollback_unknown_history_id(client: TestClient) -> None:
    assert client.post("/agents/cfo/rollback/99999").status_code == 404


def test_test_endpoint_passes_draft_to_analyze(client: TestClient) -> None:
    """The /test endpoint must forward draft overrides to analyze without
    writing to the DB."""
    create_mock = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="draft answer")])
    )
    fake_provider = SimpleNamespace(messages_create=create_mock)
    with patch("openexecutive.agents.base.get_provider", return_value=fake_provider):
        res = client.post(
            "/agents/cso/test",
            json={
                "query": "Hi",
                "prompt": "DRAFT_PROMPT",
                "model": "claude-sonnet-4-6",
                "use_deep_reasoning": False,
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["response"] == "draft answer"
    kw = create_mock.await_args.kwargs
    assert kw["model"] == "claude-sonnet-4-6"
    assert kw["system"][0]["text"] == "DRAFT_PROMPT"

    # And no DB row was written.
    assert client.get("/agents/cso").json()["has_override"] is False


def test_test_endpoint_rejects_empty_query(client: TestClient) -> None:
    res = client.post("/agents/cso/test", json={"query": "   "})
    assert res.status_code == 400


def test_test_endpoint_rejects_unknown_model(client: TestClient) -> None:
    res = client.post(
        "/agents/cso/test", json={"query": "hi", "model": "claude-fake"}
    )
    assert res.status_code == 400


def test_list_agents_returns_executive_first(client: TestClient) -> None:
    body = client.get("/agents").json()
    assert body[0]["name"] == "executive"
    assert body[0]["role"]  # non-empty default role
    assert body[0]["domains"] == []
    assert body[0]["has_override"] is False


def test_get_executive_detail_uses_persona_default(client: TestClient) -> None:
    from openexecutive.prompts.executive_persona import EXECUTIVE_PERSONA_PROMPT

    res = client.get("/agents/executive")
    assert res.status_code == 200
    detail = res.json()
    assert detail["name"] == "executive"
    assert detail["prompt"] == EXECUTIVE_PERSONA_PROMPT
    assert detail["prompt_default"] == EXECUTIVE_PERSONA_PROMPT
    assert detail["has_override"] is False
    assert detail["domains"] == []


def test_patch_executive_persists_override(client: TestClient) -> None:
    res = client.patch(
        "/agents/executive",
        json={"prompt": "You are a calm Executive.", "model": "claude-opus-4-7"},
    )
    assert res.status_code == 200
    detail = res.json()
    assert detail["prompt"] == "You are a calm Executive."
    assert detail["model"] == "claude-opus-4-7"
    assert detail["has_override"] is True
    assert set(detail["overridden_fields"]) >= {"prompt", "model"}

    # Re-fetch confirms persistence.
    again = client.get("/agents/executive").json()
    assert again["prompt"] == "You are a calm Executive."
    assert again["model"] == "claude-opus-4-7"


def test_reset_executive_override_clears_it(client: TestClient) -> None:
    client.patch("/agents/executive", json={"role": "Chief"})
    assert client.get("/agents/executive").json()["has_override"] is True
    assert client.delete("/agents/executive/override").status_code == 204
    assert client.get("/agents/executive").json()["has_override"] is False


def test_executive_history_and_rollback(client: TestClient) -> None:
    client.patch("/agents/executive", json={"role": "v1"})
    client.patch("/agents/executive", json={"role": "v2"})
    history = client.get("/agents/executive/history").json()
    target = next(h for h in history if h["role"] == "v1")
    res = client.post(f"/agents/executive/rollback/{target['id']}")
    assert res.status_code == 200
    assert client.get("/agents/executive").json()["role"] == "v1"


def test_executive_test_endpoint_uses_draft_prompt(client: TestClient) -> None:
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="exec preview")]
        )
    )
    fake_provider = SimpleNamespace(messages_create=create_mock)
    # _test_executive imports get_provider lazily from the providers package;
    # patch the symbol on the package attribute so the late binding picks it up.
    with patch(
        "openexecutive.providers.get_provider", return_value=fake_provider
    ):
        res = client.post(
            "/agents/executive/test",
            json={
                "query": "Hello",
                "prompt": "DRAFT_EXEC_PROMPT",
                "model": "claude-sonnet-4-6",
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["response"] == "exec preview"
    kw = create_mock.await_args.kwargs
    assert kw["model"] == "claude-sonnet-4-6"
    assert kw["system"][0]["text"] == "DRAFT_EXEC_PROMPT"
    # Test endpoint must not persist.
    assert client.get("/agents/executive").json()["has_override"] is False
