"""Engagement-intake generation: the grounded sibling of the fixture generator.

Covers prompt/tool selection (the part that differs from the demo path — the
shared loop is exercised by ``test_fixtures_generator.py``), Council
registration, and the ``/clients/generate`` HTTP contract.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.agents.engagement_intake import (
    ENGAGEMENT_INTAKE_SYSTEM,
    EngagementIntakeAgent,
)
from openexecutive.fixtures import generator as gen


def _intake_bundle_dict() -> dict[str, Any]:
    return {
        "profile": {
            "name": "Meridian Solar",
            "industry": "Commercial solar",
            "stage": "Private",
            "mission": "Margin-positive installs.",
        },
        "people": [
            {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
        ],
        "departments": [],
        "memory": {"decisions": [], "initiatives": [], "advice_given": [], "alerts": []},
        "docs": [{"filename": "intake_brief.md", "content": "# Brief"}],
    }


@pytest.mark.asyncio
async def test_generate_engagement_bundle_uses_grounded_agent_and_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    tool_block = SimpleNamespace(
        type="tool_use", name="emit_fixture", input=_intake_bundle_dict()
    )
    response = SimpleNamespace(content=[tool_block])

    class FakeProvider:
        async def messages_create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return response

    monkeypatch.setattr(
        "openexecutive.providers.registry.get_provider", lambda model: FakeProvider()
    )
    bundle = await gen.generate_engagement_bundle("Kickoff notes: Meridian Solar…")
    assert bundle.profile.name == "Meridian Solar"

    # Grounded agent's prompt, not the fictional fixture author's.
    assert captured["system"][0]["text"] == ENGAGEMENT_INTAKE_SYSTEM
    # Grounded tool description, same forced tool name.
    assert "REAL client" in captured["tools"][0]["description"]
    assert captured["tool_choice"]["name"] == "emit_fixture"
    # Intake framing in the user turn.
    assert captured["messages"][0]["content"].startswith("Client intake material:")


def test_engagement_intake_agent_registered_in_council() -> None:
    from openexecutive.api.routes.agents import _agent_registry

    registry = _agent_registry()
    assert "engagement_intake" in registry
    assert isinstance(registry["engagement_intake"], EngagementIntakeAgent)
    # Stays out of the Executive's consult routing.
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    assert "engagement_intake" not in SPECIALIST_REGISTRY


@pytest.fixture()
def client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from openexecutive import config
    from openexecutive.api.routes import clients as route

    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_generate_route_returns_reviewable_draft(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each request gets its own bundle — the route appends attachment docs to it,
    # so a shared instance would leak docs across tests.
    async def _fake_generate(description: str, settings: Any) -> Any:
        assert "kickoff" in description
        return gen.FixtureBundle.model_validate(_intake_bundle_dict())

    monkeypatch.setattr(gen, "generate_engagement_bundle", _fake_generate)

    resp = client.post("/clients/generate", data={"description": "kickoff notes"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_name"] == "meridian_solar"
    assert body["display_name"] == "Meridian Solar"
    assert body["bundle"]["people"][0]["full_name"] == "Dana Reyes"


def test_generate_route_injects_attachment_docs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_generate(description: str, settings: Any) -> Any:
        captured["description"] = description
        return gen.FixtureBundle.model_validate(_intake_bundle_dict())

    monkeypatch.setattr(gen, "generate_engagement_bundle", _fake_generate)

    resp = client.post(
        "/clients/generate",
        data={"description": "kickoff notes"},
        files=[("files", ("roster.csv", b"name,role\nDana Reyes,CEO\n", "text/csv"))],
    )
    assert resp.status_code == 200, resp.text

    # The attachment text reached the generator alongside the pasted notes.
    assert "kickoff notes" in captured["description"]
    assert "=== Attached: roster.csv ===" in captured["description"]
    assert "Dana Reyes,CEO" in captured["description"]

    # …and was appended to the bundle as a company doc for RAG indexing.
    docs = resp.json()["bundle"]["docs"]
    source_docs = [d for d in docs if d["filename"] == "source_roster.md"]
    assert len(source_docs) == 1
    assert "Dana Reyes,CEO" in source_docs[0]["content"]


def test_generate_route_accepts_files_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_generate(description: str, settings: Any) -> Any:
        captured["description"] = description
        return gen.FixtureBundle.model_validate(_intake_bundle_dict())

    monkeypatch.setattr(gen, "generate_engagement_bundle", _fake_generate)

    resp = client.post(
        "/clients/generate",
        files=[("files", ("brief.txt", b"Meridian Solar kickoff brief.", "text/plain"))],
    )
    assert resp.status_code == 200, resp.text
    assert "Meridian Solar kickoff brief." in captured["description"]


def test_generate_route_rejects_unsupported_file(client: TestClient) -> None:
    resp = client.post(
        "/clients/generate",
        files=[("files", ("logo.png", b"\x89PNG\r\n", "image/png"))],
    )
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["detail"]


def test_generate_route_rejects_too_many_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api import intake_uploads

    # The caps live in api.intake_uploads (shared with onboarding intake), so
    # that is the module whose attribute the helpers actually read.
    monkeypatch.setattr(intake_uploads, "_INTAKE_MAX_FILES", 2)
    files = [("files", (f"n{i}.txt", b"hello world", "text/plain")) for i in range(3)]
    resp = client.post("/clients/generate", files=files)
    assert resp.status_code == 400
    assert "Too many files" in resp.json()["detail"]


def test_generate_route_rejects_oversized_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api import intake_uploads

    monkeypatch.setattr(intake_uploads, "_INTAKE_MAX_BYTES_PER_FILE", 8)
    resp = client.post(
        "/clients/generate",
        files=[("files", ("big.txt", b"way more than eight bytes", "text/plain"))],
    )
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


def test_generate_route_truncates_stored_doc(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import clients as route

    monkeypatch.setattr(route, "_INTAKE_MAX_DOC_CHARS", 5)

    async def _fake_generate(description: str, settings: Any) -> Any:
        return gen.FixtureBundle.model_validate(_intake_bundle_dict())

    monkeypatch.setattr(gen, "generate_engagement_bundle", _fake_generate)

    resp = client.post(
        "/clients/generate",
        files=[("files", ("note.txt", b"abcdefghij", "text/plain"))],
    )
    assert resp.status_code == 200, resp.text
    docs = resp.json()["bundle"]["docs"]
    src = [d for d in docs if d["filename"] == "source_note.md"][0]
    assert src["content"] == "abcde"


def test_generate_route_dedups_same_stem_attachments(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_generate(description: str, settings: Any) -> Any:
        return gen.FixtureBundle.model_validate(_intake_bundle_dict())

    monkeypatch.setattr(gen, "generate_engagement_bundle", _fake_generate)

    # Two files share the stem "budget" — the appended docs must not collide.
    resp = client.post(
        "/clients/generate",
        files=[
            ("files", ("budget.csv", b"a,b\n1,2\n", "text/csv")),
            ("files", ("budget.txt", b"narrative budget notes", "text/plain")),
        ],
    )
    assert resp.status_code == 200, resp.text
    names = [d["filename"] for d in resp.json()["bundle"]["docs"]]
    assert "source_budget.md" in names
    assert "source_budget_2.md" in names
    # No duplicate filenames in the returned bundle.
    assert len(names) == len(set(names))


def test_generate_route_error_mapping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(description: str, settings: Any) -> Any:
        raise gen.GenerationError("model emitted garbage")

    monkeypatch.setattr(gen, "generate_engagement_bundle", _boom)

    # No notes and no files → 400 before the generator is reached.
    assert (
        client.post("/clients/generate", data={"description": "   "}).status_code == 400
    )
    resp = client.post("/clients/generate", data={"description": "notes"})
    assert resp.status_code == 422
    assert "garbage" in resp.json()["detail"]


def test_create_route_accepts_generated_bundle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from openexecutive.clients import slots
    from openexecutive.memory import episodic

    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    episodic.initialize_db(db_path)

    resp = client.post(
        "/clients",
        json={
            "display_name": "Meridian Solar",
            "source": "generated",
            "bundle": _intake_bundle_dict(),
            "intake_description": "kickoff notes",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"] == "generated"
    assert body["active"] is False

    listed = client.get("/clients").json()
    assert listed["clients"][0]["origin"] == "generated"
    assert listed["active"] is None

    # Missing bundle → 400.
    assert (
        client.post(
            "/clients", json={"display_name": "X", "source": "generated"}
        ).status_code
        == 400
    )
    _ = slots  # imported to assert module availability under route lazy imports
