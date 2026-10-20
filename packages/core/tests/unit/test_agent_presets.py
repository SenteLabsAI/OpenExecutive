"""Quality presets: resolution against the allowlist, "Custom" detection and
the override writes behind POST /agents/presets/{id}."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.agents import overrides as ov_mod  # noqa: E402
from openexecutive.agents.presets import (  # noqa: E402
    AgentState,
    preset_available,
    preset_model,
    preset_status,
    target_for,
    writes_for,
)
from openexecutive.api.routes import agents as agents_route  # noqa: E402

FULL = ["claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


def _agents() -> list[AgentState]:
    return [
        AgentState("executive", "claude-sonnet-5", False, None, None, uses_deep_reasoning=False, core=True),
        AgentState("cfo", "claude-opus-5", True, None, None, core=True),
        AgentState("triage", "claude-sonnet-5", False, None, None),
        AgentState("utility_fast", "claude-haiku-4-5", False, None, None, uses_deep_reasoning=False),
    ]


# --- pure resolution -------------------------------------------------------


def test_preset_models_resolve_against_full_allowlist() -> None:
    assert preset_model("fast", FULL) == "claude-haiku-4-5"
    assert preset_model("thorough", FULL) == "claude-opus-5-5"
    assert preset_model("balanced", FULL) is None


def test_thorough_falls_back_to_next_allowed_model() -> None:
    reduced = ["claude-opus-5", "claude-sonnet-5"]
    assert preset_model("thorough", reduced) == "claude-opus-5"
    assert preset_available("thorough", reduced)
    # No small model allowed: Fast can't be offered.
    assert not preset_available("fast", reduced)


def test_local_only_install_offers_only_balanced() -> None:
    local = ["llama3:8b"]
    assert preset_available("balanced", local)
    assert not preset_available("fast", local)
    assert not preset_available("thorough", local)
    with pytest.raises(ValueError):
        target_for("fast", _agents()[1], allowed=local)


def test_thorough_moves_core_agents_only() -> None:
    agents = {a.agent_id: a for a in _agents()}
    cfo = target_for("thorough", agents["cfo"], allowed=FULL)
    assert (cfo.model, cfo.deep) == ("claude-opus-5-5", True)
    # The Executive never sends thinking fields, so its flag stays put.
    exe = target_for("thorough", agents["executive"], allowed=FULL)
    assert (exe.model, exe.deep) == ("claude-opus-5-5", False)
    tri = target_for("thorough", agents["triage"], allowed=FULL)
    assert (tri.model, tri.deep) == ("claude-sonnet-5", False)


def test_fast_turns_deep_reasoning_off() -> None:
    cfo = target_for("fast", _agents()[1], allowed=FULL)
    assert (cfo.model, cfo.deep) == ("claude-haiku-4-5", False)


def test_writes_store_only_what_differs_from_defaults() -> None:
    writes = {w.agent_id: w for w in writes_for("fast", _agents(), allowed=FULL)}
    # utility_fast already runs Haiku: nothing to write.
    assert "utility_fast" not in writes
    assert (writes["cfo"].model, writes["cfo"].deep) == ("claude-haiku-4-5", False)
    # Deep reasoning is already off by default for triage, so it isn't stored.
    assert (writes["triage"].model, writes["triage"].deep) == ("claude-haiku-4-5", None)


def test_balanced_on_fresh_install_writes_nothing() -> None:
    assert writes_for("balanced", _agents(), allowed=FULL) == []


def test_status_is_balanced_on_fresh_install() -> None:
    st = preset_status(_agents(), allowed=FULL)
    assert (st.active, st.base, st.custom_agents) == ("balanced", "balanced", [])


def test_status_reports_custom_agents() -> None:
    agents = _agents()
    agents[1] = AgentState("cfo", "claude-opus-5", True, "claude-sonnet-5", None, core=True)
    st = preset_status(agents, allowed=FULL)
    assert st.active is None
    assert st.base == "balanced"
    assert st.custom_agents == ["cfo"]


# --- HTTP ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "agents.db"
    monkeypatch.setattr(ov_mod, "DB_PATH", db)
    ov_mod.invalidate_cache()
    app = FastAPI()
    app.include_router(agents_route.router)
    yield TestClient(app)
    ov_mod.invalidate_cache()


def test_get_presets_on_fresh_install(client: TestClient) -> None:
    res = client.get("/agents/presets")
    assert res.status_code == 200
    body = res.json()
    assert [p["id"] for p in body["presets"]] == ["fast", "balanced", "thorough"]
    assert all(p["available"] for p in body["presets"])
    assert body["active"] == "balanced"
    assert body["custom_agents"] == []


def test_apply_thorough_then_balanced_round_trips(client: TestClient) -> None:
    res = client.post("/agents/presets/thorough")
    assert res.status_code == 200
    assert res.json()["active"] == "thorough"
    cfo = client.get("/agents/cfo").json()
    assert cfo["model"] == "claude-opus-5-5"
    assert cfo["deep_reasoning"] is True
    # Background helpers keep their defaults under Thorough.
    assert client.get("/agents/triage").json()["has_override"] is False

    res = client.post("/agents/presets/balanced")
    assert res.json()["active"] == "balanced"
    # Overrides that only held the preset's model are removed entirely.
    agents = client.get("/agents").json()
    assert all(a["has_override"] is False for a in agents)


def test_apply_preset_writes_one_history_row_per_agent(client: TestClient) -> None:
    client.patch("/agents/cfo", json={"instructions": "Prefer tables."})
    client.post("/agents/presets/fast")
    history = client.get("/agents/cfo/history").json()
    # The instructions-only row was snapshotted once by the preset.
    assert len(history) == 1
    assert history[0]["instructions"] == "Prefer tables."
    assert history[0]["model"] is None
    detail = client.get("/agents/cfo").json()
    assert detail["model"] == "claude-haiku-5-5"
    assert detail["instructions"] == "Prefer tables."  # untouched

    # Balanced keeps the row because it still holds instructions.
    client.post("/agents/presets/balanced")
    detail = client.get("/agents/cfo").json()
    assert detail["has_override"] is True
    assert detail["overridden_fields"] == ["instructions"]


def test_reapplying_active_preset_writes_nothing(client: TestClient) -> None:
    client.post("/agents/presets/fast")
    before = len(client.get("/agents/cfo/history").json())
    client.post("/agents/presets/fast")
    assert len(client.get("/agents/cfo/history").json()) == before


def test_one_agent_edit_marks_it_custom(client: TestClient) -> None:
    client.post("/agents/presets/fast")
    client.patch("/agents/cso", json={"model": "claude-sonnet-5"})
    body = client.get("/agents/presets").json()
    assert body["active"] is None
    assert body["base"] == "fast"
    assert body["custom_agents"] == ["cso"]


def test_unknown_preset_404(client: TestClient) -> None:
    assert client.post("/agents/presets/cheapest").status_code == 404


def test_unavailable_preset_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_route, "_allowed", lambda agent_id=None: ["claude-sonnet-5"])
    body = client.get("/agents/presets").json()
    avail = {p["id"]: p["available"] for p in body["presets"]}
    assert avail == {"fast": False, "balanced": True, "thorough": False}
    assert client.post("/agents/presets/fast").status_code == 409


# --- visibility ------------------------------------------------------------


def test_every_council_agent_has_a_visibility(client: TestClient) -> None:
    """Core = the Executive and the domain specialists; triage and the
    helpers are internal. A new specialist must declare visibility = "core"
    or this fails."""
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    agents = {a["name"]: a["visibility"] for a in client.get("/agents").json()}
    core = {n for n, v in agents.items() if v == "core"}
    assert core == {"executive"} | (set(SPECIALIST_REGISTRY) - {"triage"})
    assert agents["triage"] == "internal"
    assert {"quality_judge", "utility_fast", "research"} <= {
        n for n, v in agents.items() if v == "internal"
    }
