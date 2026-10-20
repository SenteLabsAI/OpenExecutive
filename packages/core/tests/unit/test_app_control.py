"""App control from chat (orchestrator/app_control.py and its catalog): the
Executive does what the app's buttons do, through the same routes, and leaves
a card for anything that waits for a tap (api/routes/decisions.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import decision_ledger, episodic, session_store
from openexecutive.monitoring import store as monitoring_store
from openexecutive.orchestrator import app_control, app_control_catalog, outside_content
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
BEN = {"x-caller-email": "ben@co.example"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Stores that bind ./episodic_memory.db as a default argument find it here.
    monkeypatch.chdir(tmp_path)
    path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(session_store, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(decision_ledger, "_db_path", lambda: path)
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    episodic.initialize_db(path)
    dept_store.initialize_db(path)
    monitoring_store.initialize_db(path)
    dept_registry.invalidate()
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    chat_route._sessions.clear()
    chat_route._session_starters.clear()
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def roster() -> dict[str, Any]:
    owner = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    ben = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"owner": people_store.get_person(owner), "ben": people_store.get_person(ben)}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(sessions_route.router)
    app.include_router(decisions_route.router)
    with TestClient(app) as c:
        yield c


def _chat(owner_id: int, sid: str, title: str) -> str:
    session_store.create_session(sid, title, "2026-10-01T09:00:00+00:00", caller_person_id=owner_id)
    session_store.save_message(sid, "user", "question", sender_person_id=owner_id)
    return sid


def _web(person: Any, session_id: str) -> Session:
    return Session(session_id=session_id, from_web_chat=True, caller_person_id=person.id,
                   web_caller_signed_in=True)


def _run(handler: Any, args: dict[str, Any], session: Session | None) -> dict[str, Any]:
    async def go() -> str:
        if session is None:
            return str(await handler(args))
        with set_session(session):
            return str(await handler(args))

    return dict(json.loads(asyncio.run(go())))


def _meta(sid: str) -> dict[str, Any] | None:
    return session_store.get_session_metadata(sid)


# --------------------------------------------------------------------------- #
# Doing it now
# --------------------------------------------------------------------------- #


def test_pin_and_rename_this_chat(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-1", "Old name")
    session = _web(roster["owner"], sid)
    out = _run(app_control.handle_do_in_app, {"action": "pin_chat", "id": "this"}, session)
    assert out == {"status": "done", "did": "Pinned “Old name”"}
    assert (_meta(sid) or {}).get("pinned_at")
    out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": "this", "value": "Q3 plan"}, session)
    assert out["status"] == "done"
    assert (_meta(sid) or {})["title"] == "Q3 plan"


def test_find_lists_only_the_speakers_chats(roster: dict[str, Any]) -> None:
    _chat(roster["owner"].id, "s-own", "Pricing review")
    _chat(roster["ben"].id, "s-ben", "Ben's pricing notes")
    out = _run(app_control.handle_find_in_app, {"kind": "chat", "query": "pricing"},
               _web(roster["owner"], "s-own"))
    assert [i["id"] for i in out["items"]] == ["s-own"]


def test_someone_elses_chat_is_not_there(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-owner", "Board prep")
    out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": sid, "value": "mine now"},
               _web(roster["ben"], "s-x"))
    assert "error" in out
    assert (_meta(sid) or {})["title"] == "Board prep"


def test_nobody_verified_is_refused(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-1", "Board prep")
    assert "error" in _run(app_control.handle_do_in_app, {"action": "pin_chat", "id": sid}, None)
    unverified = Session(session_id="s-mail", origin_channel="email")
    assert "error" in _run(app_control.handle_do_in_app, {"action": "pin_chat", "id": sid}, unverified)


def test_owner_only_kinds_are_refused_for_a_teammate(roster: dict[str, Any]) -> None:
    out = _run(app_control.handle_find_in_app, {"kind": "department"}, _web(roster["ben"], "s-b"))
    assert "error" in out
    out = _run(app_control.handle_do_in_app, {"action": "add_department", "id": "new", "value": "Ops"},
               _web(roster["ben"], "s-b"))
    assert "error" in out


def test_a_value_is_required_where_the_action_takes_one(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-1", "Board prep")
    out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": sid}, _web(roster["owner"], sid))
    assert "needs a value" in out["error"]


def test_a_route_refusal_comes_back_in_words(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-1", "Board prep")
    out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": sid, "value": "   "},
               _web(roster["owner"], sid))
    assert "error" in out


# --------------------------------------------------------------------------- #
# Waiting for a tap
# --------------------------------------------------------------------------- #


def _card(decision_id: int) -> decision_ledger.DecisionInstance:
    found = decision_ledger.get_decision_instance(decision_id)
    assert found is not None
    return found


def test_delete_leaves_a_card_and_the_tap_deletes(roster: dict[str, Any], client: TestClient) -> None:
    sid = _chat(roster["owner"].id, "s-del", "Old pricing")
    out = _run(app_control.handle_do_in_app, {"action": "delete_chat", "id": "this"}, _web(roster["owner"], sid))
    assert out["status"] == "waiting_for_approval"
    assert out["card"] == "Delete the chat “Old pricing”"
    assert _meta(sid) is not None  # nothing happened yet
    card = _card(out["decision_id"])
    assert card.decision_class == "app_action"
    assert card.approver_person_id == roster["owner"].id

    # Nobody else sees or answers it.
    assert client.get(f"/decisions/{card.id}", headers=BEN).status_code == 404
    assert client.post(f"/decisions/{card.id}/approve", json={}, headers=BEN).status_code == 404

    res = client.post(f"/decisions/{card.id}/approve", json={}, headers=OWNER)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == decision_ledger.STATUS_APPROVED_UNCHANGED
    assert _meta(sid) is None
    # Once only.
    assert client.post(f"/decisions/{card.id}/approve", json={}, headers=OWNER).status_code == 409


def test_dismissing_the_card_does_nothing(roster: dict[str, Any], client: TestClient) -> None:
    sid = _chat(roster["owner"].id, "s-keep", "Keep me")
    out = _run(app_control.handle_do_in_app, {"action": "delete_chat", "id": sid}, _web(roster["owner"], sid))
    res = client.post(f"/decisions/{out['decision_id']}/reject", json={}, headers=OWNER)
    assert res.status_code == 200
    assert _meta(sid) is not None


def test_a_stale_card_is_turned_down(roster: dict[str, Any], client: TestClient) -> None:
    sid = _chat(roster["owner"].id, "s-old", "Old")
    out = _run(app_control.handle_do_in_app, {"action": "delete_chat", "id": sid}, _web(roster["owner"], sid))
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with episodic._get_conn() as conn:
        conn.execute("UPDATE decision_instances SET created_at = ? WHERE id = ?", (old, out["decision_id"]))
    res = client.post(f"/decisions/{out['decision_id']}/approve", json={}, headers=OWNER)
    assert res.status_code == 409
    assert _meta(sid) is not None


def test_a_refusal_on_the_tap_is_recorded(roster: dict[str, Any], client: TestClient) -> None:
    sid = _chat(roster["owner"].id, "s-gone", "Gone")
    out = _run(app_control.handle_do_in_app, {"action": "delete_chat", "id": sid}, _web(roster["owner"], sid))
    session_store.delete_session(sid)
    res = client.post(f"/decisions/{out['decision_id']}/approve", json={}, headers=OWNER)
    assert res.status_code == 404
    assert _card(out["decision_id"]).status == decision_ledger.STATUS_FAILED


def test_after_an_outside_read_everything_waits(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-out", "Board prep")
    session = _web(roster["owner"], sid)
    state = outside_content.begin_turn(session)
    try:
        outside_content.mark("read_document")
        out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": sid, "value": "pwned"}, session)
    finally:
        outside_content.end_turn(state)
    assert out["status"] == "waiting_for_approval"
    assert (_meta(sid) or {})["title"] == "Board prep"


def test_turning_on_act_as_me_waits_but_off_does_not() -> None:
    action = app_control.CATALOG.actions["change_setting"]
    assert action.needs_tap("act_as_me", "on")
    assert action.needs_tap("handle_it", "on")
    assert action.needs_tap("take_the_lead", "yes")
    assert not action.needs_tap("handle_it", "off")
    assert not action.needs_tap("quiet_hours", "22:00-07:00")


def test_sharing_waits_for_a_tap(roster: dict[str, Any], client: TestClient) -> None:
    sid = _chat(roster["owner"].id, "s-share", "Hiring plan")
    out = _run(app_control.handle_do_in_app, {"action": "share_chat", "id": sid, "value": "Ben"},
               _web(roster["owner"], sid))
    assert out["card"] == "Share the chat “Hiring plan” with Ben"
    res = client.post(f"/decisions/{out['decision_id']}/approve", json={}, headers=OWNER)
    assert res.status_code == 200, res.text
    assert [r["person_id"] for r in session_store.shared_with(sid)] == [roster["ben"].id]


# --------------------------------------------------------------------------- #
# Topics, settings, links, chips, the catalog
# --------------------------------------------------------------------------- #


def test_file_a_chat_under_a_topic_then_rename_the_topic(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-t", "Lease terms")
    session = _web(roster["owner"], sid)
    assert _run(app_control.handle_do_in_app,
                {"action": "move_chat_to_topic", "id": "this", "value": "Main St property"}, session)["status"] == "done"
    found = _run(app_control.handle_find_in_app, {"kind": "topic"}, session)["items"]
    assert [t["title"] for t in found] == ["Main St property"]
    out = _run(app_control.handle_do_in_app,
               {"action": "rename_topic", "id": found[0]["id"], "value": "Main Street"}, session)
    assert out == {"status": "done", "did": "Renamed the topic “Main Street”"}


def test_quiet_hours_from_chat(roster: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import notifications

    monkeypatch.setattr(notifications, "DB_PATH", Path("./episodic_memory.db").resolve(), raising=False)
    session = _web(roster["owner"], "s-q")
    out = _run(app_control.handle_do_in_app,
               {"action": "change_setting", "id": "quiet_hours", "value": "21:30-06:45"}, session)
    assert out == {"status": "done", "did": "Set Quiet hours to 21:30-06:45"}, out
    saved = notifications.get_notification_settings(roster["owner"].id)
    assert (saved.quiet_hours.enabled, saved.quiet_hours.start, saved.quiet_hours.end) == (True, "21:30", "06:45")


def test_open_in_app_gives_an_app_link() -> None:
    out = _run(app_control.handle_open_in_app, {"screen": "notifications"}, None)
    assert out == {"status": "link", "link": "/settings/notifications", "screen": "Settings, Notifications"}
    assert "error" in _run(app_control.handle_open_in_app, {"screen": "https://evil.example"}, None)
    with pytest.raises(ValueError):
        app_control.register_screen("evil", "https://evil.example", "x")


def test_chips() -> None:
    done = summarize_action(tool_name="do_in_app", tool_input={},
                            tool_result=json.dumps({"status": "done", "did": "Pinned “Q3”"}))
    assert done is not None and done["summary"] == "Pinned “Q3”"
    waiting = summarize_action(
        tool_name="do_in_app", tool_input={},
        tool_result=json.dumps({"status": "waiting_for_approval", "decision_id": 7, "card": "Delete the chat “Q3”"}),
    )
    assert waiting is not None
    assert waiting["summary"] == "Waiting for your OK: Delete the chat “Q3”"
    assert waiting["decision_id"] == 7
    assert summarize_action(tool_name="do_in_app", tool_input={}, tool_result=json.dumps({"error": "no"})) is None
    link = summarize_action(tool_name="open_in_app", tool_input={},
                            tool_result=json.dumps({"status": "link", "link": "/today", "screen": "Today"}))
    assert link is not None and link["link"] == "/today"
    assert summarize_action(tool_name="open_in_app", tool_input={},
                            tool_result=json.dumps({"status": "link", "link": "//evil.example"})) is None


def test_the_catalog_is_whole() -> None:
    catalog = app_control.CATALOG
    assert app_control_catalog  # installed on import
    for action in catalog.actions.values():
        assert action.kind in catalog.kinds
        if action.needs_tap("x", "on"):
            assert action.card, action.name
    schema = app_control.DO_IN_APP_TOOL["input_schema"]["properties"]["action"]["enum"]
    assert schema == sorted(catalog.actions)
    for name in ("pin_chat", "rename_chat", "delete_chat"):
        assert name in app_control.DO_IN_APP_TOOL["description"]
    for name, action in catalog.actions.items():
        destructive = name.startswith("delete_") or name.startswith("share_")
        if destructive:
            assert action.needs_tap("x", ""), name


def test_registering_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        app_control.register_action(app_control.CATALOG.actions["pin_chat"])


def test_departments_add_rename_and_delete(roster: dict[str, Any], client: TestClient) -> None:
    session = _web(roster["owner"], "s-d")
    out = _run(app_control.handle_do_in_app, {"action": "add_department", "id": "new", "value": "Field Ops"}, session)
    assert out == {"status": "done", "did": "Added the department “Field Ops”"}, out
    found = _run(app_control.handle_find_in_app, {"kind": "department", "query": "field"}, session)["items"]
    assert [d["title"] for d in found] == ["Field Ops"]
    slug = found[0]["id"]
    out = _run(app_control.handle_do_in_app, {"action": "rename_department", "id": slug, "value": "Field"}, session)
    assert out["status"] == "done", out
    out = _run(app_control.handle_do_in_app, {"action": "delete_department", "id": slug}, session)
    assert out["card"] == "Delete the department “Field”"
    res = client.post(f"/decisions/{out['decision_id']}/approve", json={}, headers=OWNER)
    assert res.status_code == 200, res.text
    assert _run(app_control.handle_find_in_app, {"kind": "department", "query": "field"}, session)["items"] == []


def test_memory_edit_and_delete(roster: dict[str, Any], client: TestClient) -> None:
    episodic.store_decision("sales", "Raise prices 5%")
    decision_id = episodic.list_decisions()[0].id
    session = _web(roster["owner"], "s-m")
    found = _run(app_control.handle_find_in_app, {"kind": "memory", "query": "prices"}, session)["items"]
    assert found == [{"id": f"decision:{decision_id}", "title": "Raise prices 5%", "detail": "decision"}]
    out = _run(app_control.handle_do_in_app,
               {"action": "edit_memory", "id": f"decision:{decision_id}", "value": "Raise prices 7%"}, session)
    assert out["status"] == "done", out
    out = _run(app_control.handle_do_in_app, {"action": "delete_memory", "id": f"decision:{decision_id}"}, session)
    assert out["status"] == "waiting_for_approval"
    assert client.post(f"/decisions/{out['decision_id']}/approve", json={}, headers=OWNER).status_code == 200
    assert _run(app_control.handle_find_in_app, {"kind": "memory"}, session)["items"] == []


@pytest.mark.parametrize("kind", sorted(k for k, v in app_control.CATALOG.kinds.items() if not v.no_item))
def test_every_list_reads(kind: str, roster: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.knowledge import store as knowledge_store

    class _Store:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        def __getattr__(self, _name: str) -> Any:
            return lambda *_a, **_k: {}

    monkeypatch.setattr(knowledge_store, "ChromaDBStore", _Store)
    out = _run(app_control.handle_find_in_app, {"kind": kind}, _web(roster["owner"], "s-k"))
    assert "items" in out, out


def test_topic_pin_merge_and_remove(roster: dict[str, Any]) -> None:
    owner = roster["owner"]
    session = _web(owner, "s-a")
    for sid, topic in (("s-a", "Leases"), ("s-b", "Lease terms")):
        _chat(owner.id, sid, sid)
        _run(app_control.handle_do_in_app, {"action": "move_chat_to_topic", "id": sid, "value": topic}, session)
    topics = {t["title"]: t["id"] for t in _run(app_control.handle_find_in_app, {"kind": "topic"}, session)["items"]}
    assert _run(app_control.handle_do_in_app, {"action": "pin_topic", "id": topics["Leases"]}, session)["status"] == "done"
    out = _run(app_control.handle_do_in_app,
               {"action": "merge_topic", "id": topics["Lease terms"], "value": "Leases"}, session)
    assert out == {"status": "done", "did": "Moved the chats in “Lease terms” into “Leases”"}, out
    out = _run(app_control.handle_do_in_app, {"action": "remove_topic", "id": topics["Leases"]}, session)
    assert out["status"] == "done", out
    assert _meta("s-a") is not None and _meta("s-b") is not None


def test_settings_and_lead_rules(roster: dict[str, Any], client: TestClient) -> None:
    session = _web(roster["owner"], "s-s")
    out = _run(app_control.handle_do_in_app, {"action": "change_setting", "id": "delivery", "value": "digest"}, session)
    assert out == {"status": "done", "did": "Set How messages arrive (instant or digest) to digest"}, out
    out = _run(app_control.handle_do_in_app, {"action": "change_setting", "id": "act_as_me", "value": "off"}, session)
    assert out["status"] == "done", out
    out = _run(app_control.handle_do_in_app, {"action": "change_setting", "id": "act_as_me", "value": "on"}, session)
    assert out["status"] == "waiting_for_approval"
    out = _run(app_control.handle_do_in_app,
               {"action": "add_lead_rule", "id": "new", "value": "domain: acme.com"}, session)
    assert out["status"] == "done", out
    rules = _run(app_control.handle_find_in_app, {"kind": "lead_rule"}, session)["items"]
    assert [r["title"] for r in rules] == ["domain: acme.com"]
    out = _run(app_control.handle_do_in_app, {"action": "remove_lead_rule", "id": rules[0]["id"]}, session)
    assert out["status"] == "waiting_for_approval"


def test_every_card_names_what_it_changes() -> None:
    # After outside content is read any action can become a card: each one
    # says which item and what new value, so the tap does what it shows.
    for action in app_control.CATALOG.actions.values():
        kind = app_control.CATALOG.kinds[action.kind]
        text = app_control.card_text(action, "Item X", "Value Y")
        if not kind.no_item:
            assert "Item X" in text, action.name
        if action.value:
            assert "Value Y" in text, action.name


def test_a_shared_chat_gets_no_list_and_no_change(roster: dict[str, Any]) -> None:
    sid = _chat(roster["owner"].id, "s-shared", "Board prep")
    session = _web(roster["owner"], sid)
    session.shared_chat = True
    out = _run(app_control.handle_find_in_app, {"kind": "chat"}, session)
    assert "error" in out and "items" not in out
    out = _run(app_control.handle_do_in_app, {"action": "rename_chat", "id": sid, "value": "x"}, session)
    assert "error" in out
    assert (_meta(sid) or {})["title"] == "Board prep"


def test_a_chat_turn_never_counts_as_a_signed_request() -> None:
    from openexecutive.api.caller import Caller
    from openexecutive.delegation import verified

    with verified.from_chat():
        assert verified.caller_refusal(Caller("user", "olivia@co.example"), "olivia@co.example") is not None
