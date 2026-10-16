"""/memories/reflection (api/routes/reflection.py): each person sees their
own misses, the principal those from chats they may open, and forgetting
follows the same rule."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import reflection as route
from openexecutive.memory import chat_review as cr
from openexecutive.memory import episodic, session_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _miss(person_id: int, session_id: str, text: str) -> int:
    conn = cr._connect(None)
    try:
        cur = conn.execute(
            f"INSERT INTO {cr.TURNS_TABLE} (session_id, person_id, surface, message, reply, calls, "  # noqa: S608
            "created_at, verdict, kind, wanted, signals, graded_at) "
            "VALUES (?, ?, '{}', ?, 'Noted.', '[{\"tool\": \"list_people\", \"result\": \"ok\"}]', ?, "
            "'miss', 'company', 'The thing done.', '[\"thumbs down\"]', ?)",
            (session_id, person_id, text, NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            f"INSERT INTO {cr.WEEKS_TABLE} (week, person_id, graded, misses) VALUES ('2026-W42', ?, 3, 1) "  # noqa: S608
            "ON CONFLICT(week, person_id) DO UPDATE SET graded = graded + 3, misses = misses + 1",
            (person_id,),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_a_request_without_a_sign_in_is_refused(client: TestClient, ids: dict[str, int],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    resp = client.get("/memories/reflection")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "sign_in_required"
    resp = client.get("/memories/reflection", headers={"x-caller-email": "nobody@x.example"})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_on_roster"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/memories/reflection").status_code == 200


def test_the_principal_sees_everyone_and_a_teammate_their_own(client: TestClient, ids: dict[str, int]) -> None:
    _miss(ids["principal"], "s-1", "set up an alert for the invoice")
    session_store.create_session("slack:channel:C7:ts1", "Pricing", NOW.isoformat(), ids["teammate"])
    _miss(ids["teammate"], "slack:channel:C7:ts1", "send Dana the price list")
    everyone: dict[str, Any] = client.get("/memories/reflection", headers=OWNER).json()
    assert everyone["sees_everyone"] is True
    assert sorted(m["person_name"] for m in everyone["misses"]) == ["Ben Teammate", "Olivia Owner"]
    assert everyone["weeks"] == [{"week": "2026-W42", "graded": 6, "misses": 2}]
    miss = next(m for m in everyone["misses"] if m["person_name"] == "Olivia Owner")
    assert miss["session_id"] == "s-1" and miss["tools"] == ["list_people"]
    assert miss["wanted"] == "The thing done." and miss["kind"] == "company" and miss["signals"] == ["thumbs down"]
    one = client.get("/memories/reflection", headers=OWNER, params={"person_id": ids["teammate"]}).json()
    assert [m["person_name"] for m in one["misses"]] == ["Ben Teammate"]
    assert one["weeks"] == [{"week": "2026-W42", "graded": 3, "misses": 1}]
    own = client.get("/memories/reflection", headers=TEAMMATE).json()
    assert own["sees_everyone"] is False
    assert [m["person_name"] for m in own["misses"]] == ["Ben Teammate"]
    resp = client.get("/memories/reflection", headers=TEAMMATE, params={"person_id": ids["principal"]})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_yours"


def test_a_teammates_own_chat_stays_theirs(client: TestClient, ids: dict[str, int]) -> None:
    """The principal can't open a teammate's web chat or DM, nor a chat that
    read its owner's mail (`chat._session_access`), so their misses from
    those stay out of the principal's view too; the counts still show."""
    session_store.create_session("web-ben", "Pay", NOW.isoformat(), ids["teammate"])
    _miss(ids["teammate"], "web-ben", "ask about my salary review")
    session_store.create_session("web-olivia", "Mail", NOW.isoformat(), ids["principal"])
    session_store.mark_mail_private("web-olivia", ids["principal"])
    _miss(ids["principal"], "web-olivia", "reply to the auditor")
    everyone: dict[str, Any] = client.get("/memories/reflection", headers=OWNER).json()
    assert [m["session_id"] for m in everyone["misses"]] == ["web-olivia"]
    assert everyone["weeks"] == [{"week": "2026-W42", "graded": 6, "misses": 2}]
    one = client.get("/memories/reflection", headers=OWNER, params={"person_id": ids["teammate"]}).json()
    assert one["misses"] == []
    own = client.get("/memories/reflection", headers=TEAMMATE).json()
    assert [m["session_id"] for m in own["misses"]] == ["web-ben"]


def test_forgetting_follows_the_same_rule(client: TestClient, ids: dict[str, int]) -> None:
    mine = _miss(ids["principal"], "s-1", "set up an alert for the invoice")
    theirs = _miss(ids["teammate"], "s-2", "send Dana the price list")
    assert client.delete(f"/memories/reflection/misses/{mine}", headers=TEAMMATE).status_code == 404
    assert client.delete(f"/memories/reflection/misses/{theirs}", headers=TEAMMATE).status_code == 204
    assert client.delete(f"/memories/reflection/misses/{theirs}", headers=TEAMMATE).status_code == 404
    assert client.delete(f"/memories/reflection/misses/{mine}", headers=OWNER).status_code == 204
    assert client.get("/memories/reflection", headers=OWNER).json()["misses"] == []
