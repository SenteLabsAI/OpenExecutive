"""/memories/history (api/routes/history.py): each person sees, changes and
forgets only their own notes; the company retention is the principal's."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import history as route
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.settings import set_team_members
from openexecutive.memory import episodic
from openexecutive.memory import history as h
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}
WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda et, summary, **kw: events.append((et, kw)))
    return events


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


@pytest.fixture
def client(audit: list[Any]) -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _add(person_id: int, thread: str = "t1") -> int:
    [nid] = h.add_notes(
        person_id, [h.NewNote("promised", "Told Dana Lee the price list comes Friday.",
                              "I'll send the price list on Friday", "2026-10-03")],
        source=h.SOURCE_APPROVED_REPLY, channel=h.CHANNEL_EMAIL, conversation_ref=thread,
        counterpart="Dana Lee <dana@acme.example>", subject="Price list", trust="high", occurred_at=WHEN,
        now=datetime.now(UTC),
    )
    return nid


def test_a_request_without_a_sign_in_is_refused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.get("/memories/history")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "sign_in_required"
    resp = client.get("/memories/history", headers={"x-caller-email": "nobody@x.example"})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_on_roster"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/memories/history").status_code == 200


def test_each_person_sees_only_their_own(client: TestClient, ids: dict[str, int]) -> None:
    _add(ids["principal"])
    _add(ids["teammate"])
    mine = client.get("/memories/history", headers=OWNER).json()
    assert len(mine["notes"]) == 1 and mine["notes"][0]["quote"] == "I'll send the price list on Friday"
    assert mine["reply_notes"] is False and mine["effective_retention_days"] == 90
    assert mine["retention_choices"] == [30, 90, 365, None]
    assert mine["can_note_replies"] is True and mine["can_set_company_retention"] is True
    theirs = client.get("/memories/history", headers=TEAMMATE).json()
    assert len(theirs["notes"]) == 1 and theirs["notes"][0]["id"] != mine["notes"][0]["id"]
    assert theirs["can_note_replies"] is False and theirs["can_set_company_retention"] is False
    assert client.get("/memories/history?q=venue", headers=OWNER).json()["notes"] == []


def test_switching_reply_notes_on_needs_act_as_me(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch,
    audit: list[tuple[str, dict[str, Any]]],
) -> None:
    resp = client.put("/memories/history/settings", json={"reply_notes": True}, headers=TEAMMATE)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_available_yet"
    # Turning it off, or choosing a shorter time, never needs it.
    assert client.put("/memories/history/settings", json={"reply_notes": False}, headers=TEAMMATE).status_code == 200
    monkeypatch.setattr(dsettings, "team_members_available", lambda: True)
    set_team_members(True, updated_by="test")
    assert client.put("/memories/history/settings", json={"reply_notes": True}, headers=TEAMMATE).json()["reply_notes"]
    assert client.put("/memories/history/settings", json={"reply_notes": True}, headers=OWNER).json()["reply_notes"]
    assert h.person_settings(ids["teammate"]).reply_notes and h.person_settings(ids["principal"]).reply_notes
    kinds = {kw["private_to_person"] for et, kw in audit if et == "history_settings_changed"}
    assert kinds == {ids["teammate"], ids["principal"]}
    assert all(kw["private"] is True for _, kw in audit)


def test_retention_only_shorter_and_company_wide_only_for_the_principal(
    client: TestClient, ids: dict[str, int]
) -> None:
    body = client.put("/memories/history/settings", json={"retention_days": 30}, headers=TEAMMATE).json()
    assert body["retention_days"] == 30 and body["effective_retention_days"] == 30
    resp = client.put("/memories/history/settings", json={"retention_days": 365}, headers=TEAMMATE)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_setting"
    resp = client.put("/memories/history/settings", json={"company_retention_days": 365}, headers=TEAMMATE)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "principal_only"
    body = client.put("/memories/history/settings", json={"company_retention_days": 365}, headers=OWNER).json()
    assert body["company_retention_days"] == 365
    resp = client.put("/memories/history/settings", json={"company_retention_days": 12}, headers=OWNER)
    assert resp.status_code == 422
    resp = client.put("/memories/history/settings", json={"surprise": 1}, headers=OWNER)
    assert resp.status_code == 422


def test_pin_correct_and_forget_only_your_own(client: TestClient, ids: dict[str, int]) -> None:
    nid = _add(ids["principal"])
    assert client.patch(f"/memories/history/{nid}", json={"pinned": True}, headers=TEAMMATE).status_code == 404
    assert client.delete(f"/memories/history/{nid}", headers=TEAMMATE).status_code == 404
    out = client.patch(
        f"/memories/history/{nid}", json={"pinned": True, "correction": "Told Dana it comes Monday."}, headers=OWNER
    ).json()
    assert out["pinned"] is True and out["expires_at"] is None and out["correction"] == "Told Dana it comes Monday."
    assert client.delete(f"/memories/history/{nid}", headers=OWNER).json() == {"forgotten": 1}
    assert client.delete(f"/memories/history/{nid}", headers=OWNER).status_code == 404


def test_dont_remember_this_forgets_the_conversation_for_good(client: TestClient, ids: dict[str, int]) -> None:
    first = _add(ids["principal"], "t1")
    _add(ids["principal"], "t1")
    _add(ids["principal"], "t2")
    teammate_note = _add(ids["teammate"], "t1")
    resp = client.post(f"/memories/history/{teammate_note}/forget-conversation", headers=OWNER)
    assert resp.status_code == 404
    assert client.post(f"/memories/history/{first}/forget-conversation", headers=OWNER).json() == {"forgotten": 2}
    assert len(client.get("/memories/history", headers=OWNER).json()["notes"]) == 1
    assert h.is_excluded(ids["principal"], h.conversation_key(h.CHANNEL_EMAIL, "t1"))
    assert len(client.get("/memories/history", headers=TEAMMATE).json()["notes"]) == 1
