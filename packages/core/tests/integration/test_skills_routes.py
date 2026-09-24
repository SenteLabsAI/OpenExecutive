"""Integration tests for the /skills FastAPI route group."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import skills as skills_route
from openexecutive.knowledge import skills_index, skills_repo
from openexecutive.knowledge.skills_index import seed_builtin_skills
from openexecutive.knowledge.store import ChromaDBStore


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    builtin_root = tmp_path / "builtin_skills"
    company_root = tmp_path / "company_skills"
    builtin_root.mkdir()
    company_root.mkdir()

    # Seed one built-in skill to exercise the readonly path.
    builtin_skill = builtin_root / "strategy" / "competitive-teardown.md"
    builtin_skill.parent.mkdir(parents=True)
    builtin_skill.write_text(
        "---\nname: competitive-teardown\ndescription: A teardown template\n"
        "when_to_use: Competitor analysis\ncategory: strategy\n---\n\n# Teardown\n\nSteps.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(skills_index, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_repo, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_index, "_company_skills_path", lambda: company_root)
    monkeypatch.setattr(skills_repo, "_company_skills_path", lambda: company_root)

    store = ChromaDBStore(persist_directory=str(tmp_path / "chroma"))
    asyncio.run(seed_builtin_skills(store=store, force=True))

    app = FastAPI()
    app.state.store = store
    app.include_router(skills_route.router)
    yield TestClient(app)


def test_list_returns_seeded_builtin(client: TestClient) -> None:
    resp = client.get("/skills")
    assert resp.status_code == 200
    skills = resp.json()["skills"]
    assert any(s["name"] == "competitive-teardown" and s["source"] == "builtin" for s in skills)


def test_create_company_skill(client: TestClient) -> None:
    payload = {
        "name": "test-skill",
        "category": "finance",
        "description": "A test skill",
        "when_to_use": "during tests",
        "body": "# body",
    }
    resp = client.post("/skills", json=payload)
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    assert detail["name"] == "test-skill"
    assert detail["source"] == "company"
    assert "body" in detail


def test_create_conflict_returns_409(client: TestClient) -> None:
    payload = {
        "name": "dup",
        "category": "general",
        "description": "d",
        "when_to_use": "w",
        "body": "b",
    }
    assert client.post("/skills", json=payload).status_code == 201
    resp = client.post("/skills", json=payload)
    assert resp.status_code == 409


def test_get_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/skills/does-not-exist")
    assert resp.status_code == 404


def test_update_builtin_saves_customization_and_delete_reverts(client: TestClient) -> None:
    payload = {
        "name": "competitive-teardown",
        "category": "strategy",
        "description": "our teardown",
        "when_to_use": "changed",
        "body": "ours",
    }
    resp = client.put("/skills/competitive-teardown", json=payload)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["source"], resp.json()["customized"]) == ("company", True)

    listed = [s for s in client.get("/skills").json()["skills"] if s["name"] == payload["name"]]
    assert len(listed) == 1 and listed[0]["customized"] is True

    resp = client.delete("/skills/competitive-teardown")
    assert resp.status_code == 200
    assert resp.json() == {"name": "competitive-teardown", "outcome": "reverted"}
    assert client.get("/skills/competitive-teardown").json()["source"] == "builtin"


def test_delete_builtin_hides_and_restore_unhides(client: TestClient) -> None:
    resp = client.delete("/skills/competitive-teardown")
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "hidden"

    names = {s["name"] for s in client.get("/skills").json()["skills"]}
    assert "competitive-teardown" not in names
    with_hidden = client.get("/skills", params={"include_hidden": True}).json()["skills"]
    assert any(s["name"] == "competitive-teardown" and s["hidden"] for s in with_hidden)
    detail = client.get("/skills/competitive-teardown")
    assert detail.status_code == 200 and detail.json()["hidden"] is True

    resp = client.post("/skills/competitive-teardown/restore")
    assert resp.status_code == 200
    assert resp.json()["hidden"] is False
    assert client.post("/skills/competitive-teardown/restore").status_code == 404


def test_update_company_skill(client: TestClient) -> None:
    create_payload = {
        "name": "editable",
        "category": "general",
        "description": "original",
        "when_to_use": "w",
        "body": "b",
    }
    assert client.post("/skills", json=create_payload).status_code == 201

    update_payload = {**create_payload, "description": "updated"}
    resp = client.put("/skills/editable", json=update_payload)
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated"


def test_delete_company_skill(client: TestClient) -> None:
    payload = {
        "name": "throwaway",
        "category": "general",
        "description": "d",
        "when_to_use": "w",
        "body": "b",
    }
    assert client.post("/skills", json=payload).status_code == 201
    resp = client.delete("/skills/throwaway")
    assert resp.status_code == 200
    assert client.get("/skills/throwaway").status_code == 404


def test_search_endpoint(client: TestClient) -> None:
    resp = client.get("/skills/search", params={"q": "competitor analysis teardown"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert any(r["name"] == "competitive-teardown" for r in results)
    # No body in search results.
    assert all("body" not in r for r in results)


def test_put_body_name_mismatch_400(client: TestClient) -> None:
    create_payload = {
        "name": "matched",
        "category": "general",
        "description": "d",
        "when_to_use": "w",
        "body": "b",
    }
    assert client.post("/skills", json=create_payload).status_code == 201
    resp = client.put(
        "/skills/matched",
        json={**create_payload, "name": "different"},
    )
    assert resp.status_code == 400


def test_list_and_detail_report_workflows_that_follow_a_playbook(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows.playbooks import PlaybookUser

    monkeypatch.setattr(
        skills_route,
        "playbook_users",
        lambda: {
            "competitive-teardown": [
                PlaybookUser(name="competitive_teardown", title="Competitive teardown")
            ]
        },
    )
    expected = [
        {"name": "competitive_teardown", "title": "Competitive teardown", "is_custom": False}
    ]
    listed = {s["name"]: s for s in client.get("/skills").json()["skills"]}
    assert listed["competitive-teardown"]["used_by"] == expected
    detail = client.get("/skills/competitive-teardown").json()
    assert detail["used_by"] == expected
