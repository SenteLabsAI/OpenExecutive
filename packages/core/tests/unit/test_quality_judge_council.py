"""The Committee's Quality Judge is exposed through the Agent Council.

Council overrides on ``quality_judge`` must flow back into the Committee
on the next ``build_quality_reviewer`` call so operators can tune the
judge without a process restart.
"""
from __future__ import annotations

import os
from pathlib import Path

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


def test_list_agents_includes_quality_judge(client: TestClient) -> None:
    body = client.get("/agents").json()
    names = {a["name"] for a in body}
    assert "quality_judge" in names
    # No duplicates introduced.
    all_names = [a["name"] for a in body]
    assert len(all_names) == len(set(all_names))


def test_quality_judge_detail_has_committee_role_and_review_prompt(
    client: TestClient,
) -> None:
    res = client.get("/agents/quality_judge")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "quality_judge"
    assert body["role"] == "Committee · Quality Judge"
    assert body["role_default"] == "Committee · Quality Judge"
    assert body["has_override"] is False
    # Prompt default is the committee reviewer prompt, not a specialist persona.
    assert "adversarial quality reviewer" in body["prompt_default"].lower()
    # Quality judge isn't a domain expert.
    assert body["domains"] == []


def test_quality_judge_override_propagates_to_committee(
    client: TestClient,
) -> None:
    """Patch the judge's prompt via the Council; the next
    ``build_quality_reviewer`` must pick up the override."""
    new_prompt = "You are a custom quality reviewer. Return JSON only."
    res = client.patch(
        "/agents/quality_judge",
        json={"prompt": new_prompt, "model": "claude-haiku-4-5"},
    )
    assert res.status_code == 200
    detail = res.json()
    assert detail["has_override"] is True
    assert detail["prompt"] == new_prompt
    assert detail["model"] == "claude-haiku-4-5"

    # Now the Committee should pick up both overrides.
    from openexecutive.orchestrator.committee_reviewers import build_quality_reviewer

    reviewer = build_quality_reviewer(model="claude-opus-4-7")
    assert reviewer.name == "quality_judge"
    assert reviewer.system_prompt == new_prompt
    assert reviewer.model == "claude-haiku-4-5"


def test_build_quality_reviewer_uses_agent_model_not_passed_arg(
    client: TestClient,
) -> None:
    """The Council is authoritative — even without an override, the
    agent's ``effective_model()`` (which reads ``settings.default_model``
    at access time) wins over whatever the caller passes. The ``model``
    arg is retained only for call-site stability."""
    from openexecutive.config import get_settings
    from openexecutive.orchestrator.committee_reviewers import build_quality_reviewer

    reviewer = build_quality_reviewer(model="claude-opus-4-7")
    # The passed model is ignored; the agent supplies settings.default_model.
    assert reviewer.model == get_settings().default_model
    assert reviewer.model != "claude-opus-4-7"


def test_quality_judge_reset_clears_override(client: TestClient) -> None:
    client.patch("/agents/quality_judge", json={"role": "Custom Judge"})
    assert client.get("/agents/quality_judge").json()["role"] == "Custom Judge"
    res = client.delete("/agents/quality_judge/override")
    assert res.status_code == 204
    detail = client.get("/agents/quality_judge").json()
    assert detail["has_override"] is False
    assert detail["role"] == "Committee · Quality Judge"


def test_quality_judge_history_endpoint_works(client: TestClient) -> None:
    """Proves the quality_judge is fully wired into the agent endpoints —
    history (and by extension rollback) treat it like any other agent.
    History rows are only written on UPDATE, so two PATCHes are needed
    to produce one entry."""
    client.patch("/agents/quality_judge", json={"role": "v1"})
    client.patch("/agents/quality_judge", json={"role": "v2"})
    res = client.get("/agents/quality_judge/history")
    assert res.status_code == 200
    history = res.json()
    assert len(history) >= 1
    assert all(entry["agent_id"] == "quality_judge" for entry in history)
