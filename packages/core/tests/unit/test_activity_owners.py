"""The Activity feed keeps one person's items to that person.

A card made in someone's chat ("Waiting on you: sign in to …"), a follow-up
from their chat, a reminder for them and the principal's own brief are
theirs: a teammate reading Activity never sees them, and the principal never
sees a teammate's. Work done for the team shows to everyone.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import today as today_route
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic, session_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence as wf_persistence

pytestmark = pytest.mark.usefixtures("stored_department_levels")


@pytest.fixture()
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int, int]:
    db = tmp_path / "activity_owners.db"
    for module in (episodic, dept_store, people_store, alert_store, wf_persistence):
        monkeypatch.setattr(module, "DB_PATH", db)
    from openexecutive.audit import logger as audit_logger

    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db))
    dept_registry.invalidate()
    people_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    dept_store.seed_default_departments(db_path=db)
    owner = people_store.upsert_person(
        full_name="Jordan Avery", is_principal=True, email="jordan@example.com", kind="team", db_path=db,
    )
    mate = people_store.upsert_person(full_name="Sam Lee", email="sam@example.com", kind="team", db_path=db)
    people_registry.invalidate()
    assert owner is not None and mate is not None
    return db, owner, mate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _feed(viewer: int | None, **kw: object) -> set[str]:
    return {i.summary for i in today_route._build_activity(50, viewer=viewer, **kw).items}  # type: ignore[arg-type]


def test_a_card_for_one_person_stays_theirs(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    alert_store.insert_alert(
        source="browser_handover", external_id="h1", severity="medium",
        headline="Waiting on you: sign in to the pharmacy", body="b",
        topic_tags=["browser-handover"], routed_to_person_id=owner, db_path=db,
    )
    alert_store.insert_alert(
        source="system", external_id="t1", severity="high", headline="Payroll file is late", body="b", db_path=db,
    )

    assert "Waiting on you: sign in to the pharmacy" in _feed(owner)
    assert "Waiting on you: sign in to the pharmacy" not in _feed(mate)
    assert "Waiting on you: sign in to the pharmacy" not in _feed(None)
    # A card for nobody in particular is the team's.
    assert "Payroll file is late" in _feed(mate)


def test_a_follow_up_from_a_chat_is_the_chatters(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    session_store.create_session("s-owner", "pharmacy", _now(), caller_person_id=owner, db_path=db)
    session_store.create_session("s-mate", "launch", _now(), caller_person_id=mate, db_path=db)
    session_store.create_session("s-legacy", "old", _now(), caller_person_id=None, db_path=db)
    for text, session in (
        ("Check the prescription shipped", "s-owner"),
        ("Chase the Reddit post", "s-mate"),
        ("Old chat follow-up", "s-legacy"),
    ):
        episodic.insert_scheduled_action(
            run_at=_now(), channel="email", channel_ref="ops@example.com", intent_text=text,
            originating_session_id=session, status="done", db_path=db,
        )

    assert {"Check the prescription shipped", "Old chat follow-up"} <= _feed(owner)
    assert "Chase the Reddit post" not in _feed(owner)
    assert "Chase the Reddit post" in _feed(mate)
    assert not {"Check the prescription shipped", "Old chat follow-up"} & _feed(mate)


def test_a_message_for_one_person_is_theirs_unless_a_department_sent_it(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    episodic.insert_scheduled_action(
        run_at=_now(), channel="email", channel_ref="jordan@example.com|thread-9",
        intent_text="Right now: lock pricing today", status="done", db_path=db,
    )
    episodic.insert_scheduled_action(
        run_at=_now(), channel="email", channel_ref="ops@example.com",
        intent_text="Reminder for Jordan", assigned_to_person_id=owner, kind="proactive_nudge",
        status="done", db_path=db,
    )
    episodic.insert_scheduled_action(
        run_at=_now(), channel="email", channel_ref="sam@example.com",
        intent_text="Asked Sam for the campaign numbers", department="marketing", status="done", db_path=db,
    )

    assert {"Right now: lock pricing today", "Reminder for Jordan"} <= _feed(owner)
    assert not {"Right now: lock pricing today", "Reminder for Jordan"} & _feed(mate)
    assert "Asked Sam for the campaign numbers" in _feed(mate)
    assert "Asked Sam for the campaign numbers" in _feed(owner)


def test_the_principals_brief_run_is_theirs(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    wf_persistence.create_run("r-brief", "morning_brief", "Morning brief", {}, db_path=db)
    wf_persistence.complete_run("r-brief", "", db_path=db)
    wf_persistence.create_run("r-mine", "deep_research", "Research", {}, db_path=db, owner_person_id=mate)
    wf_persistence.complete_run("r-mine", "", db_path=db)

    def kinds(viewer: int | None) -> list[str]:
        return [
            i.summary for i in today_route._build_activity(50, viewer=viewer).items if i.kind == "workflow_done"
        ]

    assert len(kinds(owner)) == 1  # the brief, not Sam's research
    assert len(kinds(mate)) == 1  # Sam's research, not the brief
    assert kinds(owner) != kinds(mate)


def test_everyone_keeps_every_item_for_the_executives_own_use(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    alert_store.insert_alert(
        source="system", external_id="m1", severity="medium", headline="Sam's own card", body="b",
        routed_to_person_id=mate, db_path=db,
    )
    assert "Sam's own card" not in _feed(owner)
    assert "Sam's own card" in _feed(None, everyone=True)


def test_the_activity_screen_serves_each_reader_their_own(team: tuple[Path, int, int]) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    db, owner, mate = team
    alert_store.insert_alert(
        source="browser_handover", external_id="h2", severity="medium",
        headline="Waiting on you: sign in to the pharmacy", body="b",
        topic_tags=["browser-handover"], routed_to_person_id=owner, db_path=db,
    )
    app = FastAPI()
    app.include_router(today_route.router)
    client = TestClient(app)

    def seen(email: str) -> set[str]:
        body = client.get("/today/activity", headers={"x-caller-email": email}).json()
        return {i["summary"] for i in body["items"]}

    assert "Waiting on you: sign in to the pharmacy" in seen("jordan@example.com")
    assert "Waiting on you: sign in to the pharmacy" not in seen("sam@example.com")
    assert "Waiting on you: sign in to the pharmacy" not in seen("stranger@example.com")


@pytest.mark.asyncio
async def test_list_my_activity_reads_the_askers_feed(team: tuple[Path, int, int]) -> None:
    import json
    from types import SimpleNamespace

    from openexecutive.orchestrator import activity_tools
    from openexecutive.orchestrator.schedule_tools import current_session

    db, owner, mate = team
    alert_store.insert_alert(
        source="browser_handover", external_id="h3", severity="medium",
        headline="Waiting on you: sign in to the pharmacy", body="b",
        topic_tags=["browser-handover"], routed_to_person_id=owner, db_path=db,
    )

    async def ask(person: int, *, private: bool = True) -> set[str]:
        session = SimpleNamespace(caller_person_id=person, unattended=False, from_web_chat=private)
        token = current_session.set(session)  # type: ignore[arg-type]
        try:
            out = json.loads(await activity_tools.handle_list_my_activity({}))
        finally:
            current_session.reset(token)
        return {i["what"] for i in out["items"]}

    assert "Waiting on you: sign in to the pharmacy" in await ask(owner)
    assert "Waiting on you: sign in to the pharmacy" not in await ask(mate)
    # Where others read the reply (a channel, a call), not even the owner's own.
    assert "Waiting on you: sign in to the pharmacy" not in await ask(owner, private=False)


def test_the_feed_fails_rather_than_guess_who_a_message_is_for(
    team: tuple[Path, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> dict[tuple[str, str], object]:
        raise RuntimeError("people store locked")

    monkeypatch.setattr(today_route, "_channel_people", broken)
    with pytest.raises(RuntimeError):
        today_route._build_activity(50, viewer=team[2])


def test_the_feed_fails_rather_than_guess_whose_a_legacy_chat_is(
    team: tuple[Path, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> object:
        raise RuntimeError("people store locked")

    monkeypatch.setattr(people_registry, "get_principal", broken)
    with pytest.raises(RuntimeError):
        today_route._build_activity(50, viewer=team[2])
