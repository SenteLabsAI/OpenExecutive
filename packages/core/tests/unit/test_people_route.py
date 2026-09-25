"""HTTP-level tests for /people routes."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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


# --------------------------------------------------------------------------- #
# Who may change the roster
# --------------------------------------------------------------------------- #
# The UI proxy stamps the signed-in user's email as x-caller-email. A request
# without one (the CLI, local login) is read as the principal — which is why
# the tests above, which send none, act as the owner.

OWNER = {"x-caller-email": "olive@acme.io"}
TEAMMATE = {"x-caller-email": "tom@acme.io"}
STRANGER = {"x-caller-email": "sam@acme.io"}  # signed in, but on no People row


@pytest.fixture()
def team(client: TestClient) -> dict[str, int]:
    owner = client.post(
        "/people", json={"full_name": "Olive", "is_principal": True, "email": "olive@acme.io"}
    )
    teammate = client.post(
        "/people", json={"full_name": "Tom", "email": "tom@acme.io"}, headers=OWNER
    )
    assert (owner.status_code, teammate.status_code) == (201, 201)
    return {"owner": owner.json()["id"], "teammate": teammate.json()["id"]}


def test_the_owner_can_add_edit_and_archive(client: TestClient, team: dict[str, int]) -> None:
    added = client.post("/people", json={"full_name": "Ann"}, headers=OWNER)
    assert added.status_code == 201
    edited = client.patch(f"/people/{team['teammate']}", json={"role": "CFO"}, headers=OWNER)
    assert (edited.status_code, edited.json()["role"]) == (200, "CFO")
    assert client.post(f"/people/{added.json()['id']}/archive", headers=OWNER).status_code == 204


def test_no_signed_in_email_acts_as_the_owner(client: TestClient, team: dict[str, int]) -> None:
    # The CLI and local login, as for the workspace settings.
    assert client.patch(f"/people/{team['teammate']}", json={"role": "CFO"}).status_code == 200


@pytest.mark.parametrize("caller", [TEAMMATE, STRANGER], ids=["teammate", "unrostered"])
def test_nobody_else_can_add_edit_or_archive(
    client: TestClient, team: dict[str, int], caller: dict[str, str]
) -> None:
    before = client.get("/people", params={"include_archived": True}).json()
    responses = [
        client.post("/people", json={"full_name": "Mallory"}, headers=caller),
        client.patch(f"/people/{team['owner']}", json={"role": "Intern"}, headers=caller),
        client.post(f"/people/{team['owner']}/archive", headers=caller),
        client.post(f"/people/{team['teammate']}/archive", headers=caller),
    ]
    assert [r.status_code for r in responses] == [403] * 4
    assert responses[0].json()["detail"] == "Only the principal can change the People list"
    assert client.get("/people", params={"include_archived": True}).json() == before


def test_a_teammate_cannot_edit_their_own_entry_either(
    client: TestClient, team: dict[str, int]
) -> None:
    # Deliberate: every field is sign-in, messaging, approvals or chasing.
    resp = client.patch(
        f"/people/{team['teammate']}", json={"on_leave_until": "2026-12-01"}, headers=TEAMMATE
    )
    assert resp.status_code == 403
    tom = people_store.get_person(team["teammate"])
    assert tom is not None and tom.on_leave_until is None


def test_a_teammate_cannot_take_over_the_owners_email(
    client: TestClient, team: dict[str, int]
) -> None:
    # The attack: put an address you sign in with on the principal's row, and
    # every later sign-in resolves to the principal.
    for email in ("tom@acme.io", "tom.other@acme.io"):
        resp = client.patch(f"/people/{team['owner']}", json={"email": email}, headers=TEAMMATE)
        assert resp.status_code == 403
    owner = people_store.find_person_by_email("olive@acme.io")
    assert owner is not None and owner.id == team["owner"]
    assert people_store.find_person_by_email("tom.other@acme.io") is None


def test_a_teammate_cannot_add_a_principal(client: TestClient, team: dict[str, int]) -> None:
    resp = client.post(
        "/people",
        json={"full_name": "Mallory", "is_principal": True, "email": "m@evil.example"},
        headers=TEAMMATE,
    )
    assert resp.status_code == 403
    assert people_store.find_person_by_email("m@evil.example") is None
    assert [p.full_name for p in people_store.list_people() if p.is_principal] == ["Olive"]


def test_a_refused_caller_cannot_probe_ids(client: TestClient, team: dict[str, int]) -> None:
    assert client.patch("/people/9999", json={"role": "X"}, headers=TEAMMATE).status_code == 403
    assert client.post("/people/9999/archive", headers=TEAMMATE).status_code == 403
    assert client.patch("/people/9999", json={"role": "X"}, headers=OWNER).status_code == 404


def test_before_there_is_a_principal_anyone_signed_in_can_set_up(client: TestClient) -> None:
    # A first setup from the People page: no owner yet, so any signed-in user
    # may add people, including themselves as principal.
    tom = client.post("/people", json={"full_name": "Tom", "email": "tom@acme.io"}, headers=STRANGER)
    assert tom.status_code == 201
    tom_id = tom.json()["id"]
    assert client.patch(f"/people/{tom_id}", json={"role": "COO"}, headers=TEAMMATE).status_code == 200
    sam = client.post(
        "/people",
        json={"full_name": "Sam", "is_principal": True, "email": "sam@acme.io"},
        headers=STRANGER,
    )
    assert sam.status_code == 201
    # Now there is one, and only Sam may.
    assert client.patch(f"/people/{tom_id}", json={"role": "CFO"}, headers=TEAMMATE).status_code == 403
    assert client.patch(f"/people/{tom_id}", json={"role": "CFO"}, headers=STRANGER).status_code == 200


def test_an_unreadable_roster_refuses(
    client: TestClient, team: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(people_store, "find_principal_person", locked)
    assert client.post("/people", json={"full_name": "Ann"}, headers=OWNER).status_code == 403
