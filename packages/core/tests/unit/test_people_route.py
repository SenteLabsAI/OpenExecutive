"""HTTP-level tests for /people routes."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import people as people_route
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_registry.invalidate()
    people_store.initialize_db()

    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# List + Get
# --------------------------------------------------------------------------- #

def test_list_people_empty(client: TestClient) -> None:
    resp = client.get("/people")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_and_get(client: TestClient) -> None:
    resp = client.post(
        "/people",
        json={
            "full_name": "Alex Rivera",
            "role": "CEO",
            "is_principal": True,
            "email": "alex@example.com",
            "preferred_channel": "email",
            "authority_scope": ["wildcard"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["full_name"] == "Alex Rivera"
    assert data["is_principal"] is True
    assert AuthorityScope.WILDCARD.value in [s for s in data["authority_scope"]]

    pid = data["id"]
    get_resp = client.get(f"/people/{pid}")
    assert get_resp.status_code == 200
    assert get_resp.json()["role"] == "CEO"


def test_get_unknown_person_404(client: TestClient) -> None:
    assert client.get("/people/9999").status_code == 404


def test_list_excludes_archived_by_default(client: TestClient) -> None:
    client.post("/people", json={"full_name": "Active"})
    create = client.post("/people", json={"full_name": "ToArchive"})
    pid = create.json()["id"]
    client.post(f"/people/{pid}/archive")

    resp = client.get("/people")
    names = [p["full_name"] for p in resp.json()]
    assert "Active" in names
    assert "ToArchive" not in names


def test_list_include_archived(client: TestClient) -> None:
    client.post("/people", json={"full_name": "Active"})
    create = client.post("/people", json={"full_name": "Archived"})
    pid = create.json()["id"]
    client.post(f"/people/{pid}/archive")

    resp = client.get("/people", params={"include_archived": "true"})
    names = [p["full_name"] for p in resp.json()]
    assert "Active" in names
    assert "Archived" in names


# --------------------------------------------------------------------------- #
# Patch
# --------------------------------------------------------------------------- #

def test_patch_updates_fields(client: TestClient) -> None:
    create = client.post("/people", json={"full_name": "Old Name", "role": "CFO"})
    pid = create.json()["id"]

    resp = client.patch(f"/people/{pid}", json={"full_name": "Sarah Chen", "email": "s@co.com"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Sarah Chen"
    assert resp.json()["email"] == "s@co.com"
    assert resp.json()["role"] == "CFO"  # unchanged


def test_patch_authority_scope(client: TestClient) -> None:
    create = client.post("/people", json={"full_name": "Sarah"})
    pid = create.json()["id"]

    resp = client.patch(
        f"/people/{pid}",
        json={"authority_scope": ["spend_gt_10k", "board_comms"]},
    )
    assert resp.status_code == 200
    scopes = set(resp.json()["authority_scope"])
    assert "spend_gt_10k" in scopes
    assert "board_comms" in scopes


def test_patch_unknown_person_404(client: TestClient) -> None:
    assert client.patch("/people/9999", json={"role": "X"}).status_code == 404


def test_patch_empty_body_noop(client: TestClient) -> None:
    create = client.post("/people", json={"full_name": "Alex"})
    pid = create.json()["id"]
    resp = client.patch(f"/people/{pid}", json={})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Alex"


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #

def test_archive_person(client: TestClient) -> None:
    create = client.post("/people", json={"full_name": "Jamie"})
    pid = create.json()["id"]
    resp = client.post(f"/people/{pid}/archive")
    assert resp.status_code == 204

    get_resp = client.get(f"/people/{pid}")
    assert get_resp.json()["archived"] is True


def test_archive_unknown_404(client: TestClient) -> None:
    assert client.post("/people/9999/archive").status_code == 404


# --------------------------------------------------------------------------- #
# By-scope lookup
# --------------------------------------------------------------------------- #

def test_by_scope_returns_matching(client: TestClient) -> None:
    create = client.post(
        "/people",
        json={"full_name": "Sarah", "authority_scope": ["spend_gt_10k"]},
    )
    assert create.status_code == 201

    resp = client.get("/people/by-scope/spend_gt_10k")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["full_name"] == "Sarah"


def test_by_scope_wildcard_included(client: TestClient) -> None:
    client.post(
        "/people",
        json={"full_name": "Founder", "is_principal": True, "authority_scope": ["wildcard"]},
    )
    resp = client.get("/people/by-scope/legal_sign")
    names = [p["full_name"] for p in resp.json()]
    assert "Founder" in names


def test_by_scope_unknown_token_400(client: TestClient) -> None:
    resp = client.get("/people/by-scope/do_whatever")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Registry invalidation
# --------------------------------------------------------------------------- #

def test_create_invalidates_registry(client: TestClient) -> None:
    # Warm the cache.
    before = people_registry.list_people()
    assert before == []
    assert people_registry._cache is not None

    client.post("/people", json={"full_name": "New Person"})
    assert people_registry._cache is None  # invalidated

    after = people_registry.list_people()
    assert len(after) == 1


def test_patch_clears_on_leave_with_flag(client: TestClient) -> None:
    # Mirrors the person page: an emptied date sends null plus clear_on_leave.
    pid = client.post("/people", json={"full_name": "Sam Lee", "role": "COO"}).json()["id"]
    resp = client.patch(f"/people/{pid}", json={"on_leave_until": "2026-12-01", "clear_on_leave": False})
    assert resp.json()["on_leave_until"] == "2026-12-01"
    # null alone leaves the date in place.
    assert client.patch(f"/people/{pid}", json={"on_leave_until": None}).json()["on_leave_until"] == "2026-12-01"
    resp = client.patch(f"/people/{pid}", json={"on_leave_until": None, "clear_on_leave": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["on_leave_until"] is None
