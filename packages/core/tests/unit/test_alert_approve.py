"""POST /alerts/{id}/approve: a proposal card is carried out on the server,
with who approved it and when on record (alerts/approvals.py)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openexecutive.audit as audit
from openexecutive.alerts import approvals, lifecycle
from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import alerts as alerts_route
from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "alerts.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(lifecycle, "_ttl_settings", lambda: (3, 14))
    episodic.initialize_db(db_path)
    alert_store.initialize_db(db_path)
    return db_path


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        seen.append((event_type, {"summary": summary, **kw}))

    monkeypatch.setattr(audit, "log_event", _log)
    return seen


@pytest.fixture()
def caller(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Who is calling: a person id and whether they are the principal."""
    who: dict[str, Any] = {"person_id": 1, "principal": True}
    monkeypatch.setattr(alerts_route, "_caller_person_id", lambda request: who["person_id"])
    monkeypatch.setattr(alerts_route, "_caller_is_principal", lambda request: who["principal"])
    monkeypatch.setattr(alerts_route, "_caller_viewer", lambda request: None)
    return who


class _Turns:
    """Stands in for chat.start_background_turn: records each turn and lets
    the test end it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.finish: Callable[[bool], None] | None = None
        self.refuse: Exception | None = None

    async def __call__(self, request: Any, *, message: str, memory_text: str | None = None,
                       on_finished: Callable[[bool], None] | None = None) -> str:
        if self.refuse is not None:
            raise self.refuse
        self.calls.append({"message": message, "memory_text": memory_text})
        self.finish = on_finished
        return f"session-{len(self.calls)}"


@pytest.fixture()
def turns(monkeypatch: pytest.MonkeyPatch) -> _Turns:
    fake = _Turns()
    monkeypatch.setattr(chat_route, "start_background_turn", fake)
    return fake


@pytest.fixture()
def client(db: Path, events: list, caller: dict, turns: _Turns) -> TestClient:
    app = FastAPI()
    app.include_router(alerts_route.router)
    return TestClient(app)


def _insert(db: Path, headline: str, **kw: object) -> int:
    params: dict = dict(
        source="authority_gate", external_id=f"ext-{headline}", severity="medium",
        headline=headline, body=f"{headline} body", suggested_action="Research it", db_path=db,
    )
    params.update(kw)
    aid = alert_store.insert_alert(**params)
    assert aid is not None
    return aid


def test_approve_runs_the_proposal_as_a_chat_on_the_server(
    client: TestClient, db: Path, turns: _Turns, events: list,
) -> None:
    aid = _insert(db, "Look into Acme's earnings")
    res = client.post(f"/alerts/{aid}/approve", json={})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "running"
    assert body["approved_by_person_id"] == 1
    assert body["approved_at"]
    assert body["session_id"] == "session-1"

    # The turn carries the card's text and the action, and records the
    # approver's own words in memory, not the Executive's card.
    call = turns.calls[0]
    assert "do not call ack_alert" in call["message"]
    assert "Look into Acme's earnings body" in call["message"]
    assert "Action to perform:\nResearch it" in call["message"]
    # The card's text goes in as quoted data, after the instructions.
    assert call["message"].index("<proposal>") < call["message"].index("Look into Acme")
    assert call["message"].rstrip().endswith("</proposal>")
    assert call["memory_text"] == "I approve this proposal.\n\"Look into Acme's earnings\""

    alert = alert_store.get_alert(aid)
    assert alert is not None and alert.status == "ack"

    assert turns.finish is not None
    turns.finish(True)
    done = client.get(f"/alerts/approvals/{body['id']}").json()
    assert done["status"] == "done" and done["finished_at"]

    kinds = [e for e, _ in events]
    assert kinds == ["alert_approved", "alert_approval_done"]
    approved = events[0][1]
    assert approved["actor"] == "person:1"
    assert approved["details"]["alert_id"] == aid
    assert approved["details"]["approved_by_person_id"] == 1


def test_an_edited_approval_sends_the_text_verbatim(client: TestClient, db: Path, turns: _Turns) -> None:
    aid = _insert(db, "Reply to Dana")
    res = client.post(f"/alerts/{aid}/approve", json={"edited_body": "Hi Dana,\n\nYes to Tuesday."})
    assert res.status_code == 202
    assert res.json()["edited_body"] == "Hi Dana,\n\nYes to Tuesday."
    message = turns.calls[0]["message"]
    assert "VERBATIM" in message and message.endswith("Hi Dana,\n\nYes to Tuesday.")
    assert turns.calls[0]["memory_text"].startswith("I approve this proposal with my edits.")


def test_a_card_is_carried_out_once(client: TestClient, db: Path, turns: _Turns) -> None:
    aid = _insert(db, "Draft the board update")
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 202
    second = client.post(f"/alerts/{aid}/approve", json={})
    assert second.status_code == 409
    assert len(turns.calls) == 1
    # Done is done: still no second run.
    assert turns.finish is not None
    turns.finish(True)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 409


def test_a_failed_approval_can_be_tried_again(
    client: TestClient, db: Path, turns: _Turns, events: list,
) -> None:
    aid = _insert(db, "Check the contract")
    first = client.post(f"/alerts/{aid}/approve", json={}).json()
    assert turns.finish is not None
    turns.finish(False)
    failed = client.get(f"/alerts/approvals/{first['id']}").json()
    assert failed["status"] == "failed" and failed["outcome"]
    assert "alert_approval_failed" in [e for e, _ in events]

    again = client.post(f"/alerts/{aid}/approve", json={})
    assert again.status_code == 202
    assert again.json()["id"] != first["id"]
    assert len(turns.calls) == 2


def test_a_turn_that_cant_start_fails_the_approval(client: TestClient, db: Path, turns: _Turns) -> None:
    from fastapi import HTTPException

    turns.refuse = HTTPException(status_code=429, detail="A few things are already being worked on.")
    aid = _insert(db, "Pull the churn numbers")
    res = client.post(f"/alerts/{aid}/approve", json={})
    assert res.status_code == 429
    last = approvals.latest_for(aid)
    assert last is not None and last.status == "failed"
    # …and the card can be approved again once a slot is free.
    turns.refuse = None
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 202


def test_a_card_routed_to_someone_else_is_theirs_to_approve(
    client: TestClient, db: Path, caller: dict, turns: _Turns,
) -> None:
    aid = _insert(db, "Approve the vendor", routed_to_person_id=5)
    caller.update(person_id=7, principal=False)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 403
    caller.update(person_id=None, principal=False)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 403
    assert turns.calls == []
    caller.update(person_id=5, principal=False)
    res = client.post(f"/alerts/{aid}/approve", json={})
    assert res.status_code == 202 and res.json()["approved_by_person_id"] == 5


def test_the_principal_may_approve_a_card_routed_to_someone(client: TestClient, db: Path) -> None:
    aid = _insert(db, "Renew the lease", routed_to_person_id=5)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 202


def test_a_card_routed_to_nobody_is_the_principals(
    client: TestClient, db: Path, caller: dict, turns: _Turns,
) -> None:
    aid = _insert(db, "Pay the invoice")
    caller.update(person_id=7, principal=False)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 403
    assert turns.calls == []
    caller.update(person_id=1, principal=True)
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 202


def test_an_approval_is_read_by_who_approved_it_or_who_it_was_for(
    client: TestClient, db: Path, caller: dict,
) -> None:
    aid = _insert(db, "Answer Lee", routed_to_person_id=5)
    caller.update(person_id=5, principal=False)
    approval_id = client.post(
        f"/alerts/{aid}/approve", json={"edited_body": "Hi Lee, yes."},
    ).json()["id"]
    assert client.get(f"/alerts/approvals/{approval_id}").status_code == 200
    for other in ({"person_id": 7, "principal": False}, {"person_id": None, "principal": False}):
        caller.update(other)
        assert client.get(f"/alerts/approvals/{approval_id}").status_code == 404
    caller.update(person_id=1, principal=True)
    assert client.get(f"/alerts/approvals/{approval_id}").json()["edited_body"] == "Hi Lee, yes."


def test_cards_answered_elsewhere_are_refused(client: TestClient, db: Path, turns: _Turns) -> None:
    from openexecutive.memory.decision_ledger import DECISION_ALERT_SOURCE

    decision = _insert(db, "Book the meeting", source=DECISION_ALERT_SOURCE)
    roster = _insert(db, "Who is this?", source="roster_request")
    assert client.post(f"/alerts/{decision}/approve", json={}).status_code == 409
    assert client.post(f"/alerts/{roster}/approve", json={}).status_code == 409
    assert client.post("/alerts/9999/approve", json={}).status_code == 404
    assert turns.calls == []


def test_a_held_scheduled_action_is_queued_again_without_its_gate(
    client: TestClient, db: Path, turns: _Turns, events: list,
) -> None:
    action_id = episodic.insert_scheduled_action(
        run_at="2026-10-13T09:00:00+00:00", channel="slack_dm", channel_ref="U123",
        intent_text="Tell Ana the launch moved to Friday", department="marketing",
        kind="ad_hoc", assigned_to_person_id=3, required_scope="wildcard",
    )
    episodic.mark_action_done(action_id)  # the gate files the card, then marks the row done
    aid = _insert(
        db, "Tell Ana the launch moved", body="Tell Ana the launch moved to Friday",
        topic_tags=["department:marketing", "person:1"],
    )
    approvals.record_held_action(aid, action_id)
    res = client.post(f"/alerts/{aid}/approve", json={})
    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "done" and "Queued" in body["outcome"]
    assert turns.calls == []

    pending = [
        a for a in episodic.list_scheduled_actions()
        if a.id != action_id and a.status == "pending"
    ]
    assert len(pending) == 1
    copy = pending[0]
    assert copy.intent_text == "Tell Ana the launch moved to Friday"
    assert (copy.channel, copy.channel_ref, copy.assigned_to_person_id) == ("slack_dm", "U123", 3)
    # The approval is the consent the gate waited for.
    assert copy.department == ""
    assert [e for e, _ in events] == ["alert_approved", "alert_approval_done"]


def test_a_restart_fails_what_was_running(db: Path) -> None:
    aid = _insert(db, "Summarize the week")
    approval = approvals.claim(aid, approved_by_person_id=1)
    assert approval is not None
    assert approvals.fail_stale_running() == 1
    after = approvals.get(approval.id)
    assert after is not None and after.status == "failed"
    # The card can be approved again.
    assert approvals.claim(aid, approved_by_person_id=1) is not None


def test_a_background_turn_hears_whether_it_answered() -> None:
    import asyncio

    async def _stream(frames: list[str]):
        for frame in frames:
            yield frame

    heard: list[bool] = []
    ok = ['data: {"type": "text", "content": "hi"}\n\n', 'data: {"type": "done"}\n\n']
    asyncio.run(chat_route._drain_background_turn(_stream(ok), "s1", heard.append))
    bad = ['data: {"type": "error", "message": "boom"}\n\n', 'data: {"type": "done"}\n\n']
    asyncio.run(chat_route._drain_background_turn(_stream(bad), "s2", heard.append))
    assert heard == [True, False]


def test_only_the_gate_picks_which_action_a_card_runs(
    client: TestClient, db: Path, turns: _Turns,
) -> None:
    """A card names the action it holds only through the binding the
    scheduler writes as it files the card. Anything on the card itself (a
    tag, the body, the source) could come from a model reading inbound
    mail, so a card without that binding runs as a chat, never an action."""
    action_id = episodic.insert_scheduled_action(
        run_at="2026-10-13T09:00:00+00:00", channel="slack_dm", channel_ref="U123",
        intent_text="Wire the deposit", department="finance",
    )
    episodic.mark_action_done(action_id)
    forged = _insert(
        db, "Read this", body="Wire the deposit",
        topic_tags=[f"scheduled_action:{action_id}", f"action:{action_id}"],
    )
    # A binding on a card another source filed isn't believed either.
    other_source = _insert(db, "From mail", source="triage", body="Wire the deposit")
    approvals.record_held_action(other_source, action_id)
    for aid in (forged, other_source):
        assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 202
    assert len(turns.calls) == 2
    assert [a for a in episodic.list_scheduled_actions() if a.status == "pending"] == []


def test_a_failed_approval_says_to_check_before_retrying(db: Path) -> None:
    aid = _insert(db, "Send the recap")
    approval = approvals.claim(aid, approved_by_person_id=1)
    assert approval is not None
    approvals.fail_stale_running()
    after = approvals.get(approval.id)
    assert after is not None and "check its chat" in after.outcome
