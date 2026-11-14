"""A custom workflow is deleted by the person who created it or the principal.

Workflows stay shared for editing and running. Deleting one is its creator's
or the principal's: a teammate gets a 403 for someone else's, and the list tells
the page who may (``can_delete``) so it only offers Delete where it works.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.workflows import router as workflows_router
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import approved_targets, dynamic_store

OWNER = "jordan@example.com"
MATE = "sam@example.com"
OTHER = "riley@example.com"
_SECRET = "shared-s3cret"


@pytest.fixture()
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "workflow_delete.db"
    for module in (dynamic_store, people_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    from openexecutive.audit import logger as audit_logger

    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db))
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    people_registry.invalidate()
    people_store.initialize_db(db)
    dynamic_store.initialize_dynamic_workflows_db(db)
    approved_targets.initialize_approved_targets_db(db)
    people_store.upsert_person(
        full_name="Jordan Avery", is_principal=True, email=OWNER, kind="team", db_path=db,
    )
    people_store.upsert_person(full_name="Sam Lee", email=MATE, kind="team", db_path=db)
    people_store.upsert_person(full_name="Riley Cho", email=OTHER, kind="team", db_path=db)
    people_registry.invalidate()
    yield db
    people_registry.invalidate()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(workflows_router)
    return TestClient(app, client=("203.0.113.5", 50000))


def _as(email: str) -> dict[str, str]:
    return {"x-api-key": _SECRET, "x-caller-email": email}


def _definition(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "title": name.replace("_", " ").title(),
        "input_fields": [{"name": "topic", "label": "Topic", "required": True}],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }


def _make(client: TestClient, name: str, email: str) -> None:
    r = client.post("/workflows/custom", json=_definition(name), headers=_as(email))
    assert r.status_code == 201, r.text


def _can_delete(client: TestClient, email: str) -> dict[str, bool]:
    r = client.get("/workflows/custom", headers=_as(email))
    assert r.status_code == 200, r.text
    return {d["name"]: d["can_delete"] for d in r.json()["definitions"]}


def test_creator_deletes_their_own(team: Path) -> None:
    client = _client()
    _make(client, "sams_digest", MATE)
    assert client.delete("/workflows/custom/sams_digest", headers=_as(MATE)).status_code == 200
    assert client.get("/workflows/custom/sams_digest", headers=_as(MATE)).status_code == 404


def test_teammate_cannot_delete_someone_elses(team: Path) -> None:
    client = _client()
    _make(client, "sams_digest", MATE)
    _make(client, "owners_review", OWNER)
    for name in ("sams_digest", "owners_review"):
        r = client.delete(f"/workflows/custom/{name}", headers=_as(OTHER))
        assert r.status_code == 403, r.text
        assert "created this workflow" in r.json()["detail"]
        assert client.get(f"/workflows/custom/{name}", headers=_as(OTHER)).status_code == 200


def test_principal_deletes_any(team: Path) -> None:
    client = _client()
    _make(client, "sams_digest", MATE)
    assert client.delete("/workflows/custom/sams_digest", headers=_as(OWNER)).status_code == 200


def test_no_recorded_creator_is_the_principals(team: Path) -> None:
    client = _client()
    _make(client, "old_one", MATE)
    with dynamic_store._get_conn(team) as conn:
        conn.execute("UPDATE dynamic_workflows SET owner_person_id = NULL WHERE name = 'old_one'")
    assert client.delete("/workflows/custom/old_one", headers=_as(MATE)).status_code == 403
    assert client.delete("/workflows/custom/old_one", headers=_as(OWNER)).status_code == 200


def test_unknown_workflow_is_a_404_for_everyone(team: Path) -> None:
    assert _client().delete("/workflows/custom/nope", headers=_as(OTHER)).status_code == 404


def test_list_says_who_can_delete(team: Path) -> None:
    client = _client()
    _make(client, "sams_digest", MATE)
    _make(client, "owners_review", OWNER)
    assert _can_delete(client, MATE) == {"sams_digest": True, "owners_review": False}
    assert _can_delete(client, OTHER) == {"sams_digest": False, "owners_review": False}
    assert _can_delete(client, OWNER) == {"sams_digest": True, "owners_review": True}
