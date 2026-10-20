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
from openexecutive.delegation import handle_it, inbox, may_do, training
from openexecutive.delegation.settings import set_enabled
from openexecutive.memory import decision_ledger as ledger
from openexecutive.memory import episodic
from openexecutive.orchestrator import take_the_lead as ttl
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


def _handling(pid: int, mode: str = "balanced") -> None:
    set_enabled(pid, True, updated_by="t")
    inbox.set_watch(pid, True, updated_by="t")
    handle_it.set_(pid, enabled=True, mode=mode, updated_by="t")
    training.set_(pid, {training.REPLIES: False, training.FOLLOW_UPS: False}, updated_by="t")


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
    assert rows["replies_known"].outcome == "off" and "Write drafts as me is off" in rows["replies_known"].why
    assert "actions" not in rows and "sensitive" not in rows
    assert section.notes == []


def test_drafts_and_the_inbox_without_handle_it_wait_for_a_tap(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    set_enabled(pid, True, updated_by="t")
    inbox.set_watch(pid, True, updated_by="t")
    rows = _rows(_you(pid))
    assert rows["chat_drafts"].outcome == "drafts"
    assert rows["replies_known"].outcome == "waits" and "Handle it for me is off" in rows["replies_known"].why
    assert rows["follow_ups"].outcome == "waits"
    assert rows["actions"].outcome == "waits"


def test_suggested_actions_on_go_out(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    set_enabled(pid, True, updated_by="t")
    training.set_(pid, {training.ACTIONS_ON: True}, updated_by="t")
    row = _rows(_you(pid))["actions"]
    assert row.outcome == "goes" and "Suggested actions are On" in row.why


@pytest.mark.parametrize(("mode", "known", "strangers", "follow_ups", "links"), [
    ("careful", "goes", "waits", "waits", "waits"),
    ("balanced", "goes", "waits", "goes", "waits"),
    ("bold", "goes", "goes", "goes", "goes"),
])
def test_each_level_matches_its_rules(
    ids: dict[str, int], mode: str, known: str, strangers: str, follow_ups: str, links: str
) -> None:
    pid = ids["principal"]
    _handling(pid, mode)
    rules = handle_it.RULES[mode]
    # The rows follow the rules the refusals check, not a copy of them.
    assert (strangers == "goes") is rules.strangers
    assert (follow_ups == "goes") is bool(rules.follow_up_to)
    assert (links == "goes") is (rules.details_confidence is not None)
    section = _you(pid)
    rows = _rows(section)
    assert rows["replies_known"].outcome == known
    assert rows["replies_strangers"].outcome == strangers
    assert rows["follow_ups"].outcome == follow_ups
    assert rows["links"].outcome == links
    assert rows["sensitive"].outcome == "always_waits"
    assert may_do.LEVEL_NAMES[mode] in rows["replies_known"].why
    assert section.notes and "needs Draft replies" in section.notes[0]
    assert "a day" in section.footer


def test_everything_names_the_lead_and_what_it_rides_on(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    _handling(pid, "careful")
    ttl.set_(ttl.person_scope(pid), enabled=True, updated_by="t")
    ttl.add_rule(ttl.person_scope(pid), "domain", "acme.com", created_by="t")
    section = _you(pid)
    rows = _rows(section)
    assert rows["replies_strangers"].outcome == "goes" and "Everything" in rows["replies_strangers"].why
    assert rows["follow_ups"].outcome == "goes"
    # At Everything a link still waits, whatever the level underneath allowed.
    assert rows["links"].outcome == "waits" and rows["links"].what == "Replies with a link in them."
    assert rows["rules"].items == ("Anyone at acme.com",)
    assert "Everything" in section.notes[0] and "turns Everything off" in section.notes[0]


def test_only_the_owner_sees_the_company_rules_by_name(ids: dict[str, int]) -> None:
    # Take the lead as you is the owner's, but a row left on (say, by an
    # earlier owner) must not hand the company's rules to anyone else.
    pid = ids["teammate"]
    _handling(pid, "careful")
    ttl.set_(ttl.person_scope(pid), enabled=True, updated_by="t")
    ttl.add_rule(ttl.person_scope(pid), "domain", "acme.com", created_by="t")
    ttl.add_rule(ttl.SCOPE_COMPANY, "words", "acquisition", created_by="t")
    row = _rows(_you(pid, principal=False))["rules"]
    assert row.items == ("Anyone at acme.com",)
    assert "acquisition" not in row.why and "1 rule" in row.why
    assert _rows(_you(pid))["rules"].items == ("Anyone at acme.com", "Words: acquisition")


def test_training_holds_replies_and_counts_what_it_allowed(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    _handling(pid, "bold")
    ttl.set_(ttl.person_scope(pid), enabled=True, updated_by="t")
    training.set_(pid, {training.REPLIES: True}, updated_by="t")
    training.allow_sender(pid, "dana@northpeak.example", "Dana Park", decision_id=None)
    rows = _rows(_you(pid))
    # In training Everything doesn't apply: only allowed senders go.
    assert rows["replies_known"].outcome == "waits" and "1 sender you ticked" in rows["replies_known"].why
    assert "links" not in rows
    # Follow-ups aren't in training, so the lead still covers them.
    assert rows["follow_ups"].outcome == "goes" and "Everything" in rows["follow_ups"].why


def test_without_signed_sign_ins_nothing_is_sent_as_you(ids: dict[str, int]) -> None:
    pid = ids["principal"]
    _handling(pid, "bold")
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


def test_take_the_lead_off_is_one_row() -> None:
    rows = may_do.as_the_executive(is_principal=True).rows
    assert [r.id for r in rows] == ["lead"] and rows[0].outcome == "off"


def test_ask_first_switches_and_meetings(ids: dict[str, int]) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, ask_first={"money": False}, updated_by="t")
    ttl.add_rule(ttl.SCOPE_COMPANY, "words", "discount", created_by="t")
    section = may_do.as_the_executive(is_principal=True)
    rows = _rows(section)
    assert rows["lead"].outcome == "goes"
    assert rows["kind:money"].outcome == "goes"
    assert rows["kind:contracts"].outcome == "waits"
    # What it thinks of itself never books on its own: the meeting gates need
    # someone on a verified surface in the turn.
    assert rows["meetings"].outcome == "always_waits"
    assert rows["meetings_chat"].outcome == "waits"
    assert rows["rules"].items == ("Words: discount",)
    assert "Department levels don't apply" in section.footer


def test_training_waits_until_something_is_allowed() -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, training=True, ask_first={"money": False}, updated_by="t")
    rows = _rows(may_do.as_the_executive(is_principal=True))
    assert rows["lead"].outcome == "waits"
    assert rows["kind:money"].outcome == "waits" and rows["kind:money"].then == "Waits unless allowed"


def test_meetings_asked_for_in_chat_follow_the_switch_and_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.departments.models import AuthorityLevel
    from openexecutive.memory import workspace_settings

    ledger.set_class_mode("meeting_scheduling", "auto_execute")
    # Team workspace: Operations must act on its own too.
    assert may_do._meetings_in_chat().outcome == "waits"

    class _State:
        class config:  # noqa: N801 — a stand-in for DepartmentConfig
            authority_level = AuthorityLevel.AUTO_EXECUTE

    monkeypatch.setattr("openexecutive.departments.registry.get_state", lambda slug: _State())
    assert may_do._meetings_in_chat().outcome == "goes"
    # Solo: the switch alone decides.
    monkeypatch.setattr("openexecutive.departments.registry.get_state", lambda slug: None)
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


def test_the_owner_sees_all_three(client: TestClient, ids: dict[str, int]) -> None:
    _handling(ids["principal"], "bold")
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
