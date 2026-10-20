"""Who may see and change a person's other contact details through the
people routes (api/routes/people.py): the details on GET /people,
/people/{id} and /people/by-scope go only to the principal and to that
person, and fail closed; POST and PATCH check every detail before writing."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import people as people_route
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope, ContactDetail

OWNER = "olivia@co.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.setattr("openexecutive.integrations.roster_intake.after_roster_write", lambda: None)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def owner() -> int:
    pid = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    return pid


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(people_route.router)
    return TestClient(app)


def test_the_people_route_takes_details(owner: int, api: TestClient) -> None:
    pid = people_store.upsert_person(full_name="Leo Park", email="leo@contoso.example", kind="contact")
    mine = {"x-caller-email": OWNER}
    body = api.patch(f"/people/{pid}", headers=mine, json={"details": [
        {"kind": "phone", "value": " +1 555 010 0100 "}, {"kind": "address", "value": ""},
    ]}).json()
    assert body["details"] == [{"kind": "phone", "value": "+1 555 010 0100"}]
    bad = api.patch(f"/people/{pid}", headers=mine, json={
        "role": "Changed", "details": [{"kind": "website", "value": "not a site"}],
    })
    assert bad.status_code == 422
    # A bad detail changes nothing: not the person, and no one is created.
    assert people_store.get_person(pid).role == ""
    before = len(people_store.list_people(include_contacts=True))
    made = api.post("/people", headers=mine, json={
        "full_name": "Ann Example", "email": "ann@x.example", "kind": "contact",
        "details": [{"kind": "phone", "value": "abc"}],
    })
    assert made.status_code == 422
    assert len(people_store.list_people(include_contacts=True)) == before


def test_a_teammates_details_go_only_to_the_principal_and_them(owner: int, api: TestClient) -> None:
    ben = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_store.upsert_person(full_name="Cara Teammate", email="cara@co.example")

    people_store.set_person_details(ben, [ContactDetail(kind="mobile", value="555 010 0142")], source="signature")
    people_registry.invalidate()

    def details(who: str, path: str) -> list[Any]:
        body = api.get(path, headers={"x-caller-email": who}).json()
        rows = body if isinstance(body, list) else [body]
        return [r["details"] for r in rows if r["id"] == ben]

    for path in ("/people", f"/people/{ben}"):
        assert details(OWNER, path) == [[{"kind": "mobile", "value": "555 010 0142"}]]
        assert details("ben@co.example", path) == [[{"kind": "mobile", "value": "555 010 0142"}]]
        assert details("cara@co.example", path) == [[]]


def test_details_fail_closed_and_by_scope_hides_them(owner: int, api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import chat as chat_route
    people_store.upsert_person(full_name="Cara Teammate", email="cara@co.example")
    people_store.set_person_details(owner, [ContactDetail(kind="phone", value="555 010 0199")])
    people_store.set_authority_scope(owner, [AuthorityScope.WILDCARD])
    people_registry.invalidate()
    token = AuthorityScope.WILDCARD.value
    [mine] = [r for r in api.get(f"/people/by-scope/{token}", headers={"x-caller-email": OWNER}).json()
              if r["id"] == owner]
    assert mine["details"] == [{"kind": "phone", "value": "555 010 0199"}]
    theirs = api.get(f"/people/by-scope/{token}", headers={"x-caller-email": "cara@co.example"}).json()
    assert [r["details"] for r in theirs if r["id"] == owner] == [[]]

    def broken(_request: Any) -> int:
        raise RuntimeError("no caller")

    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", broken)
    rows = api.get("/people", headers={"x-caller-email": OWNER}).json()
    assert [r["details"] for r in rows if r["id"] == owner] == [[]]
