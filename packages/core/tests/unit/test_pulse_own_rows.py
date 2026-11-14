"""Pulse keeps one person's follow-ups and advice to that person.

A follow-up or a piece of advice from someone's own chat is theirs: a teammate
listing follow-ups or advice never gets it, can't open it by id and can't
cancel it. The team's rows show to everyone. The same holds for an AI app
reading through the MCP resources, where the roster also keeps a teammate's
phone and website to the principal and to that person.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.caller import Caller
from openexecutive.api.routes.episodic import router as memories_router
from openexecutive.api.routes.scheduled import router as scheduled_router
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.mcp_server import server as mcp_server
from openexecutive.memory import episodic, session_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import ContactDetail

OWNER = "jordan@example.com"
MATE = "sam@example.com"
_SECRET = "shared-s3cret"


@pytest.fixture()
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int, int]:
    db = tmp_path / "pulse_own_rows.db"
    for module in (episodic, dept_store, people_store):
        monkeypatch.setattr(module, "DB_PATH", db)
    # Audit rows (a cancel, a department's advice mirrored to memory) go to
    # the test's file, never ./episodic_memory.db.
    from openexecutive.audit import logger as audit_logger

    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db))
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "")
    dept_registry.invalidate()
    people_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    owner = people_store.upsert_person(
        full_name="Jordan Avery", is_principal=True, email=OWNER, kind="team", db_path=db,
    )
    mate = people_store.upsert_person(full_name="Sam Lee", email=MATE, kind="team", db_path=db)
    assert owner is not None and mate is not None
    people_registry.invalidate()
    now = datetime.now(UTC).isoformat()
    session_store.create_session("s-owner", "pay review", now, caller_person_id=owner, db_path=db)
    session_store.create_session("s-mate", "launch", now, caller_person_id=mate, db_path=db)
    yield db, owner, mate
    dept_registry.invalidate()
    people_registry.invalidate()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(scheduled_router)
    app.include_router(memories_router)
    return TestClient(app, client=("203.0.113.5", 50000))


def _as(email: str) -> dict[str, str]:
    return {"x-api-key": _SECRET, "x-caller-email": email}


def _follow_up(text: str, session: str | None, db: Path, **kw: Any) -> int:
    return episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channel="email", channel_ref="ops@example.com", intent_text=text,
        originating_session_id=session, db_path=db, **kw,
    )


def _texts(rows: list[dict[str, Any]], key: str) -> set[str]:
    return {r[key] for r in rows}


def test_a_follow_up_from_a_chat_lists_for_the_chatter_alone(team: tuple[Path, int, int]) -> None:
    db, _owner, _mate = team
    _follow_up("Review Dan's pay on Friday", "s-owner", db)
    _follow_up("Chase the launch post", "s-mate", db)
    _follow_up("Weekly marketing check-in", None, db, department="marketing")
    client = _client()

    owner_sees = _texts(client.get("/scheduled", headers=_as(OWNER)).json(), "intent_text")
    mate_sees = _texts(client.get("/scheduled", headers=_as(MATE)).json(), "intent_text")

    assert owner_sees == {"Review Dan's pay on Friday", "Weekly marketing check-in"}
    assert mate_sees == {"Chase the launch post", "Weekly marketing check-in"}


def test_another_persons_follow_up_reads_as_missing_and_cant_be_cancelled(
    team: tuple[Path, int, int],
) -> None:
    db, _owner, _mate = team
    owners = _follow_up("Review Dan's pay on Friday", "s-owner", db)
    mates = _follow_up("Chase the launch post", "s-mate", db)
    shared = _follow_up("Weekly marketing check-in", None, db, department="marketing")
    client = _client()

    assert client.get(f"/scheduled/{owners}", headers=_as(MATE)).status_code == 404
    assert client.delete(f"/scheduled/{owners}", headers=_as(MATE)).status_code == 404
    row = episodic.get_scheduled_action(owners, db_path=db)
    assert row is not None and row.status == "pending"

    # Their own and the team's they may cancel; an admin (the principal) any.
    assert client.get(f"/scheduled/{mates}", headers=_as(MATE)).status_code == 200
    assert client.delete(f"/scheduled/{shared}", headers=_as(MATE)).status_code == 200
    assert client.get(f"/scheduled/{mates}", headers=_as(OWNER)).status_code == 404
    assert client.delete(f"/scheduled/{mates}", headers=_as(OWNER)).status_code == 200


def test_advice_from_a_chat_lists_for_the_chatter_alone(team: tuple[Path, int, int]) -> None:
    db, _owner, _mate = team
    episodic.store_advice("hr", "How to handle Dan's raise", "Hold it until review", session_id="s-owner", db_path=db)
    episodic.store_advice("marketing", "Launch timing", "Post on Tuesday", session_id="s-mate", db_path=db)
    episodic.store_advice("ops", "Team offsite", "Book it for May", department="operations", db_path=db)
    client = _client()

    owner_sees = _texts(client.get("/memories/advice", headers=_as(OWNER)).json(), "advice_summary")
    mate_rows = client.get("/memories/advice", headers=_as(MATE)).json()

    assert owner_sees == {"Hold it until review", "Book it for May"}
    assert _texts(mate_rows, "advice_summary") == {"Post on Tuesday", "Book it for May"}

    owners_id = next(a.id for a in episodic.list_advice(db_path=db) if a.session_id == "s-owner")
    assert client.patch(
        f"/memories/advice/{owners_id}", json={"advice_summary": "x"}, headers=_as(MATE)
    ).status_code == 404
    assert client.delete(f"/memories/advice/{owners_id}", headers=_as(MATE)).status_code == 404
    kept = episodic.get_advice(owners_id, db_path=db)
    assert kept is not None and kept.advice_summary == "Hold it until review"


def test_a_follow_up_assigned_to_someone_is_theirs_too(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    _follow_up("Send Sam the brief", "s-owner", db, created_by_person_id=owner, assigned_to_person_id=mate)
    client = _client()

    for email in (OWNER, MATE):
        assert _texts(client.get("/scheduled", headers=_as(email)).json(), "intent_text") == {
            "Send Sam the brief"
        }


def test_other_peoples_rows_dont_push_the_callers_off_the_page(team: tuple[Path, int, int]) -> None:
    db, _owner, _mate = team
    for i in range(5):
        _follow_up(f"Owner's follow-up {i}", "s-owner", db)
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        channel="email", channel_ref="ops@example.com", intent_text="Chase the launch post",
        originating_session_id="s-mate", db_path=db,
    )

    rows = _client().get("/scheduled?limit=2", headers=_as(MATE)).json()

    assert _texts(rows, "intent_text") == {"Chase the launch post"}


def _mcp(name: str, email: str) -> Any:
    async def _go() -> str:
        with patch.object(mcp_server, "_who", return_value=Caller("user", email)):
            return str(await getattr(mcp_server, name)())

    return json.loads(asyncio.run(_go()))


def test_an_ai_app_reads_advice_as_its_person(team: tuple[Path, int, int]) -> None:
    db, _owner, _mate = team
    episodic.store_advice("hr", "How to handle Dan's raise", "Hold it until review", session_id="s-owner", db_path=db)
    episodic.store_advice("ops", "Team offsite", "Book it for May", department="operations", db_path=db)

    assert _texts(_mcp("memory_advice", MATE), "advice_summary") == {"Book it for May"}
    assert _texts(_mcp("memory_advice", OWNER), "advice_summary") == {
        "Hold it until review", "Book it for May",
    }


def test_an_ai_app_roster_keeps_a_teammates_details_to_them(team: tuple[Path, int, int]) -> None:
    db, owner, mate = team
    third = people_store.upsert_person(full_name="Dana Cho", email="dana@example.com", kind="team", db_path=db)
    assert third is not None
    people_store.set_person_details(third, [ContactDetail(kind="phone", value="+1 555 0100")], db_path=db)
    people_store.set_person_details(mate, [ContactDetail(kind="phone", value="+1 555 0111")], db_path=db)
    people_registry.invalidate()

    def details(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
        return {r["full_name"]: r["details"] for r in rows}

    for_mate = details(_mcp("people_roster", MATE))
    assert for_mate["Dana Cho"] == []
    assert [d["value"] for d in for_mate["Sam Lee"]] == ["+1 555 0111"]
    for_owner = details(_mcp("people_roster", OWNER))
    assert [d["value"] for d in for_owner["Dana Cho"]] == ["+1 555 0100"]
