"""Integration: assign a task over HTTP, read it back, close it."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.people import router as people_router
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    people_store.upsert_person(full_name="Pat Principal", is_principal=True,
                               email="pat@northwind.test")
    people_store.upsert_person(full_name="Sara Kim", email="sara@northwind.test")
    people_store.upsert_person(full_name="Ben Ortiz", email="ben@northwind.test")
    app = FastAPI()
    app.include_router(people_router)
    return TestClient(app)


def _id(name: str) -> int:
    person = next(p for p in people_store.list_people() if p.full_name == name)
    assert person.id is not None
    return person.id


def test_assign_list_close(client: TestClient) -> None:
    sara, ben = _id("Sara Kim"), _id("Ben Ortiz")
    as_sara = {"x-caller-email": "sara@northwind.test"}

    # A teammate assigns Ben a task …
    res = client.post(f"/people/{ben}/open-loops", headers=as_sara,
                      json={"task": "send me the Q3 numbers", "due_date": "2099-01-01"})
    assert res.status_code == 201, res.text
    loop = res.json()
    assert loop["owner_person_id"] == ben
    assert loop["description"] == "Sara Kim asked Ben Ortiz for: send me the Q3 numbers"

    # … but cannot list Ben's loops; Ben and the principal (no header) can.
    assert client.get(f"/people/{ben}/open-loops", headers=as_sara).status_code == 403
    mine = client.get(f"/people/{ben}/open-loops", headers={"x-caller-email": "ben@northwind.test"})
    assert [lp["loop_id"] for lp in mine.json()] == [loop["loop_id"]]
    assert len(client.get(f"/people/{ben}/open-loops").json()) == 1

    # The same task again is a conflict; an unrostered caller is refused.
    again = client.post(f"/people/{ben}/open-loops", headers=as_sara,
                        json={"task": "send me the Q3 numbers"})
    assert again.status_code == 409
    stranger = client.post(f"/people/{sara}/open-loops",
                           headers={"x-caller-email": "who@else.test"}, json={"task": "anything"})
    assert stranger.status_code == 403
    assert client.post(f"/people/{ben}/open-loops", json={"task": ""}).status_code == 422

    # Ben closes it.
    closed = client.post(f"/open-loops/{loop['loop_id']}/close",
                         headers={"x-caller-email": "ben@northwind.test"}, json={"reason": "done"})
    assert closed.status_code == 204
    assert client.get(f"/people/{ben}/open-loops").json() == []
