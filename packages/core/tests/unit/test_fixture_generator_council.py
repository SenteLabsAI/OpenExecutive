"""The fixture generator is exposed through the Agent Council.

Council overrides on ``fixture_generator`` must flow into
``generate_fixture_bundle`` on the next call so operators can switch the
generation model (incl. OpenRouter slugs) without a process restart.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.agents.fixture_generator import FixtureGeneratorAgent  # noqa: E402
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


def test_list_agents_includes_fixture_generator(client: TestClient) -> None:
    body = client.get("/agents").json()
    names = [a["name"] for a in body]
    assert "fixture_generator" in names
    # Not callable as a specialist — but present in the Council.
    assert len(names) == len(set(names))  # no duplicates


def test_fixture_generator_detail(client: TestClient) -> None:
    res = client.get("/agents/fixture_generator")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "fixture_generator"
    assert body["role"] == "Utility · Company fixture generator"
    assert body["has_override"] is False
    assert body["domains"] == []
    assert "fixture author" in body["prompt_default"].lower()


def test_override_propagates_to_effective_model(client: TestClient) -> None:
    res = client.patch(
        "/agents/fixture_generator",
        json={"model": "claude-haiku-4-5"},
    )
    assert res.status_code == 200
    assert res.json()["model"] == "claude-haiku-4-5"
    # The generator reads effective_model() at call time → picks up the override.
    assert FixtureGeneratorAgent().effective_model() == "claude-haiku-4-5"


def test_effective_model_defaults_to_settings(client: TestClient) -> None:
    from openexecutive.config import get_settings

    # No override set → falls back to the class default (settings.default_model).
    assert FixtureGeneratorAgent().effective_model() == get_settings().default_model


def test_reset_clears_override(client: TestClient) -> None:
    client.patch("/agents/fixture_generator", json={"model": "claude-haiku-4-5"})
    res = client.delete("/agents/fixture_generator/override")
    assert res.status_code == 204
    assert client.get("/agents/fixture_generator").json()["has_override"] is False
