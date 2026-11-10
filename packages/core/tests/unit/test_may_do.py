"""What it does for you (delegation/may_do.py, GET /what-it-may-do): one read-only
answer to what goes out without asking, read from the gates' own settings."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import may_do as route
from openexecutive.delegation import handle_it, inbox, may_do
from openexecutive.delegation.settings import set_enabled
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}
CONTACT = {"x-caller-email": "dana@northpeak.example"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "CALLER_ASSERTION_PUBLIC_KEYS"):
        monkeypatch.delenv(var, raising=False)
    # Sending as someone needs the API to tie a switch to them.
    monkeypatch.setattr(handle_it, "signing_ok", lambda: True)
    monkeypatch.setattr("openexecutive.scheduler.pause.is_paused", lambda: False)
    monkeypatch.setattr("openexecutive.departments.registry.list_states", lambda **_: [])
    monkeypatch.setattr("openexecutive.departments.registry.get_state", lambda slug: None)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_store.upsert_person(full_name="Dana Park", email="dana@northpeak.example", kind="contact")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


def _handling(pid: int) -> None:
    set_enabled(pid, True, updated_by="t")
    inbox.set_watch(pid, True, updated_by="t")


def _level(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: level)


def _rows(section: may_do.Section) -> dict[str, may_do.Row]:
    return {r.id: r for r in section.rows}


def _you(pid: int, *, principal: bool = True) -> may_do.Section:
    return may_do.as_you(pid, can_have_it=True, signing=True, is_principal=principal)


# ── as you ────────────────────────────────────────────────────────────────────


def test_with_everything_off_nothing_goes_out_as_you(ids: dict[str, int]) -> None:
    section = _you(ids["principal"])
    rows = _rows(section)
    assert section.goes == 0
    assert rows["chat_drafts"].outcome == "off"
    assert rows["replies"].outcome == "off" and "Write drafts as me is off" in rows["replies"].why
    assert "actions" not in rows
    assert section.notes == []


def test_drafts_without_the_inbox_watcher_wait_for_a_tap(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    set_enabled(pid, True, updated_by="t")
    rows = _rows(_you(pid))
    assert rows["chat_drafts"].outcome == "drafts" and rows["chat_drafts"].then == "Drafts, sent on your tap"
    assert rows["replies"].outcome == "off" and "Draft replies to your inbox is off" in rows["replies"].why
    assert rows["follow_ups"].outcome == "off"
    assert rows["actions"].outcome == "waits"


@pytest.mark.parametrize(("level", "outcome", "setting"), [
    ("ask", "waits", may_do.SETTING_APPROVALS),
    ("outsiders", "goes", may_do.SETTING_ACT_AS_ME),
    ("trust", "goes", may_do.SETTING_ACT_AS_ME),
])
def test_approvals_decides_what_goes_as_you(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch, level: str, outcome: str, setting: str,
) -> None:
    pid = ids["principal"]
    _handling(pid)
    _level(monkeypatch, level)
    section = _you(pid)
    rows = _rows(section)
    for row_id in ("replies", "follow_ups", "invites"):
        assert rows[row_id].outcome == outcome and rows[row_id].setting == setting
    assert rows["actions"].outcome == outcome
    assert "links" not in rows and "sensitive" not in rows and "rules" not in rows
    assert section.notes == []
    if outcome == "goes":
        assert "a day" in section.footer


def test_in_training_everything_as_you_waits_on_any_level(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import training

    pid = ids["principal"]
    _handling(pid)
    _level(monkeypatch, "trust")
    training.set_(pid, {training.TRAINING: True}, updated_by="t")
    section = _you(pid)
    rows = _rows(section)
    for row_id in ("replies", "follow_ups", "invites", "actions"):
        assert rows[row_id].outcome == "waits" and rows[row_id].setting == may_do.SETTING_ACT_AS_ME
        assert "In training" in rows[row_id].why
    assert section.goes == 0 and section.footer == ""
    training.set_(pid, {training.TRAINING: False}, updated_by="t")
    assert _rows(_you(pid))["replies"].outcome == "goes"


def test_on_balanced_suggested_actions_skip_someone_new(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    pid = ids["principal"]
    _handling(pid)
    _level(monkeypatch, "outsiders")
    assert "added in the last week wait" in _rows(_you(pid))["actions"].why
    _level(monkeypatch, "trust")
    assert _rows(_you(pid))["actions"].why == "Approvals is on Hands-off."
    assert "from anyone" in _rows(_you(pid))["invites"].why


def test_without_signed_sign_ins_nothing_is_sent_as_you(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    _handling(pid)
    section = may_do.as_you(pid, can_have_it=True, signing=False)
    assert section.goes == 0
    assert any("Signed sign-ins are off" in n for n in section.notes)


def test_someone_without_act_as_me_is_told_so(ids: dict[str, int]) -> None:
    section = may_do.as_you(ids["teammate"], can_have_it=False, signing=True)
    assert section.available is False and section.rows == []
    assert "owner" in section.unavailable


# ── as the Executive ──────────────────────────────────────────────────────────


def test_the_executive_section_is_the_owners_alone() -> None:
    section = may_do.as_the_executive(is_principal=False)
    assert section.available is False and section.rows == []


@pytest.mark.parametrize(("mode", "outcome"), [("ask", "waits"), ("outsiders", "waits"), ("trust", "goes")])
def test_approvals_decides_what_it_does_on_its_own(
    monkeypatch: pytest.MonkeyPatch, mode: str, outcome: str,
) -> None:
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: mode)
    rows = {r.id: r for r in may_do.as_the_executive(is_principal=True).rows}
    assert list(rows) == ["lead", "to_you", "meetings_chat"]
    assert rows["lead"].outcome == outcome and rows["lead"].setting == may_do.SETTING_APPROVALS
    assert rows["to_you"].outcome == "goes"


def test_in_training_the_executive_waits_even_on_hands_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import outside_mode, take_the_lead

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    take_the_lead.set_(take_the_lead.SCOPE_EXECUTIVE, training=True, updated_by="t")
    rows = {r.id: r for r in may_do.as_the_executive(is_principal=True).rows}
    assert rows["lead"].outcome == "waits" and rows["lead"].setting == may_do.SETTING_TAKE_THE_LEAD
    assert "In training" in rows["lead"].why
    take_the_lead.set_(take_the_lead.SCOPE_EXECUTIVE, training=False, updated_by="t")
    assert {r.id: r for r in may_do.as_the_executive(is_principal=True).rows}["lead"].outcome == "goes"


def test_meetings_asked_for_in_chat_follow_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import workspace_settings
    from openexecutive.orchestrator import outside_mode

    # Team workspace below Hands-off: the owner's book, a teammate's wait.
    assert may_do._meetings_in_chat().outcome == "waits"
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert may_do._meetings_in_chat().outcome == "goes"
    # Solo: what you ask for books at every level.
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.ASK)
    workspace_settings.set_workspace_mode("solo")
    assert may_do._meetings_in_chat().outcome == "goes"


# ── the route ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _section(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(s for s in body["sections"] if s["key"] == key)


def test_the_owner_sees_all_three(client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    _handling(ids["principal"])
    _level(monkeypatch, "trust")
    body = client.get("/what-it-may-do", headers=OWNER).json()
    assert [s["key"] for s in body["sections"]] == ["as_you", "executive", "departments"]
    you = _section(body, "as_you")
    assert you["available"] and you["goes"] >= 3
    assert _section(body, "executive")["available"] is True
    assert body["paused"] is False


def _roster_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from contextlib import contextmanager

    @contextmanager
    def broken(db_path: Any = None) -> Iterator[Any]:
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(people_store, "_get_conn", broken)


def test_a_roster_it_cannot_read_says_so(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster_down(monkeypatch)
    assert client.get("/what-it-may-do", headers=OWNER).status_code == 503


def test_it_knows_people_the_way_the_rest_of_the_app_does(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment can map a signed-in account to its person in the shared
    # caller resolver; the page must follow that, not look the email up again.
    monkeypatch.setattr(
        "openexecutive.api.routes.chat._resolve_caller_person_id", lambda request: ids["principal"],
    )
    resp = client.get("/what-it-may-do", headers={"x-caller-email": "signed-in@elsewhere.example"})
    assert resp.status_code == 200
    assert _section(resp.json(), "executive")["available"] is True


def test_a_teammate_sees_their_own_and_the_departments(client: TestClient, ids: dict[str, int]) -> None:
    body = client.get("/what-it-may-do", headers=TEAMMATE).json()
    assert _section(body, "as_you")["available"] is False
    assert _section(body, "executive")["available"] is False
    assert _section(body, "departments")["available"] is True


def test_contacts_strangers_and_anonymous_callers_are_refused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/what-it-may-do", headers=CONTACT).status_code == 403
    assert client.get("/what-it-may-do", headers={"x-caller-email": "nobody@x.example"}).status_code == 403
    assert client.get("/what-it-may-do").status_code == 403
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/what-it-may-do").status_code == 200


def test_it_says_when_the_executive_is_paused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openexecutive.scheduler.pause.is_paused", lambda: True)
    assert client.get("/what-it-may-do", headers=OWNER).json()["paused"] is True
