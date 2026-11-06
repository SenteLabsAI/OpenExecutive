"""A card says what it needs from its reader (Alert.needs): an
answer typed on the card, a pick, an OK, or nothing. An answer goes back
into the work (POST /alerts/{id}/answer); Approve is refused where it would
do nothing useful."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openexecutive.alerts import approvals
from openexecutive.alerts import store as alert_store
from openexecutive.alerts.models import AlertEvent
from openexecutive.orchestrator import alert_tools

from .test_alert_approve import _insert, _Turns, caller, client, db, events, turns  # noqa: F401


def test_the_card_keeps_what_it_needs(db: Path) -> None:  # noqa: F811
    aid = _insert(db, "Features", needs="choice", question="Which domain?", choices=["a.com", "a.ai"])
    alert = alert_store.get_alert(aid)
    assert alert is not None
    assert (alert.needs, alert.question, alert.choices) == ("choice", "Which domain?", ["a.com", "a.ai"])
    old = _insert(db, "Old card")
    assert alert_store.get_alert(old).needs == ""  # type: ignore[union-attr]


def test_a_repeat_asks_the_newest_question(db: Path) -> None:  # noqa: F811
    aid = _insert(db, "Ask", dedup_key="k", needs="answer", question="First?")
    hit = alert_store.coalesce_alert(
        source="authority_gate", dedup_key="k", severity="medium", body="b", needs="answer", question="Second?",
        db_path=db,
    )
    assert hit is not None and hit[0] == aid
    assert alert_store.get_alert(aid).question == "Second?"  # type: ignore[union-attr]
    # A repeat that says nothing, or only "for you to know", keeps the ask.
    alert_store.coalesce_alert(
        source="authority_gate", dedup_key="k", severity="medium", body="c", needs="fyi", db_path=db,
    )
    assert alert_store.get_alert(aid).needs == "answer"  # type: ignore[union-attr]
    alert_store.coalesce_alert(source="authority_gate", dedup_key="k", severity="medium", body="c", db_path=db)
    assert alert_store.get_alert(aid).question == "Second?"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({"needs": "answer", "question": "Which features?"}, ("answer", "Which features?", [])),
        ({"needs": "answer", "subject": "Need the list"}, ("answer", "Need the list", [])),
        ({"needs": "choice", "question": "Q?", "choices": ["A", " a ", "B", "C", "D", "E"]}, ("choice", "Q?", ["A", "B", "C", "D"])),
        ({"needs": "choice", "question": "Q?", "choices": ["Only one"]}, ("answer", "Q?", [])),
        ({"needs": "ok", "question": "ignored", "choices": ["x", "y"]}, ("ok", "", [])),
        ({"needs": "fyi"}, ("fyi", "", [])),
        ({"needs": "approve"}, ("", "", [])),
        ({}, ("", "", [])),
    ],
)
def test_create_alert_reads_what_the_card_needs(given: dict[str, Any], expected: tuple[str, str, list[str]]) -> None:
    assert alert_tools._card_ask({"subject": "S", **given}) == expected


@pytest.mark.parametrize("needs", ["", "ok", "fyi"])
def test_a_repeat_never_turns_a_card_that_asks_nothing_into_a_question(db: Path, needs: str) -> None:  # noqa: F811
    aid = _insert(db, f"Held {needs}", dedup_key=f"h{needs}", needs=needs)
    alert_store.coalesce_alert(
        source="authority_gate", dedup_key=f"h{needs}", severity="medium", body="b", needs="answer",
        question="Yes?", db_path=db,
    )
    alert = alert_store.get_alert(aid)
    assert alert is not None and (alert.needs, alert.question) == (needs, "")


@pytest.mark.parametrize("source", ["decision_scheduling", "roster_request"])
def test_a_card_with_its_own_buttons_takes_no_answer(client: TestClient, db: Path, source: str) -> None:  # noqa: F811
    aid = _insert(db, f"Own {source}", source=source, needs="answer", question="Q?")
    assert client.post(f"/alerts/{aid}/answer", json={"answer": "x"}).status_code == 409


def test_a_held_action_takes_no_answer(client: TestClient, db: Path) -> None:  # noqa: F811
    aid = _insert(db, "Held ask", needs="answer", question="Q?")
    approvals.record_held_action(aid, 99)
    assert client.post(f"/alerts/{aid}/answer", json={"answer": "x"}).status_code == 409


def test_the_same_mail_handled_again_gets_its_own_card(db: Path) -> None:  # noqa: F811
    aid = _insert(db, "Asked", source="email", external_id="m1")
    assert alert_tools._fresh_external_id("email", "m1") == "m1"  # open: the pipeline folds or drops
    assert alert_tools._fresh_external_id("email", "m1", folds=True).startswith("m1:")
    alert_store.set_status(aid, "read", db_path=db)  # opened, still open
    assert alert_tools._fresh_external_id("email", "m1") == "m1"
    alert_store.set_status(aid, "dismissed", db_path=db)
    assert alert_tools._fresh_external_id("email", "m1").startswith("m1:")
    assert alert_tools._fresh_external_id("email", "m2") == "m2"
    assert alert_tools._fresh_external_id("email", "") == ""


def test_the_event_carries_the_ask_to_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[AlertEvent] = []
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", seen.append)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(alert_tools, "_fresh_external_id", lambda s, e, folds=False: e)
    out = json.loads(asyncio.run(alert_tools.handle_create_alert({
        "subject": "Domain", "body": "Which one?", "needs": "choice", "question": "Which domain?",
        "choices": ["openexecutive.com", "openexecutive.ai"],
    })))
    assert out["status"] == "alert_scheduled"
    assert (seen[0].needs, seen[0].question, seen[0].choices) == (
        "choice", "Which domain?", ["openexecutive.com", "openexecutive.ai"],
    )


def test_an_answer_carries_on_the_work(client: TestClient, db: Path, turns: _Turns, events: list) -> None:  # noqa: F811
    aid = _insert(db, "Feature list", needs="answer", question="Which 3 to 5 features?")
    res = client.post(f"/alerts/{aid}/answer", json={"answer": "The spring launch, two features"})
    assert res.status_code == 202, res.text
    assert res.json()["session_id"] == "session-1"
    message = turns.calls[0]["message"]
    assert "Carry on with the work" in message and "do not call ack_alert" in message
    assert "Your question: Which 3 to 5 features?" in message
    # The answer is theirs, after the quoted card.
    assert message.index("</proposal>") < message.index("My answer:\nThe spring launch, two features")
    assert "The spring launch, two features" in turns.calls[0]["memory_text"]
    assert alert_store.get_alert(aid).status == "ack"  # type: ignore[union-attr]
    assert any(e == "alert_answered" for e, _ in events)
    # Once only.
    assert client.post(f"/alerts/{aid}/answer", json={"answer": "again"}).status_code == 409


def test_only_a_card_that_asks_takes_an_answer(client: TestClient, db: Path) -> None:  # noqa: F811
    plain = _insert(db, "Plain")
    fyi = _insert(db, "Heads up", needs="fyi")
    assert client.post(f"/alerts/{plain}/answer", json={"answer": "x"}).status_code == 409
    assert client.post(f"/alerts/{fyi}/answer", json={"answer": "x"}).status_code == 409
    assert client.post(f"/alerts/{plain}/answer", json={"answer": "  "}).status_code in (409, 422)


@pytest.mark.parametrize("needs", ["answer", "choice", "fyi"])
def test_approve_is_refused_where_it_would_do_nothing(client: TestClient, db: Path, needs: str) -> None:  # noqa: F811
    aid = _insert(db, f"Card {needs}", needs=needs, question="Q?", choices=["a", "b"])
    assert client.post(f"/alerts/{aid}/approve", json={}).status_code == 409


def test_someone_else_cannot_answer(client: TestClient, db: Path, caller: dict) -> None:  # noqa: F811
    aid = _insert(db, "Theirs", needs="answer", question="Q?", routed_to_person_id=7)
    caller.update(person_id=8, principal=False)
    assert client.post(f"/alerts/{aid}/answer", json={"answer": "x"}).status_code == 403
    caller.update(person_id=7)
    assert client.post(f"/alerts/{aid}/answer", json={"answer": "x"}).status_code == 202


def test_an_app_call_naming_no_tool_never_becomes_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_gate, levels

    held: list[Any] = []
    monkeypatch.setattr(department_gate, "hold", lambda *a, **k: held.append(a) or 1)
    out = json.loads(levels.hold("", {}, mcp=True, reason="rule"))
    assert "needs the tool's name" in out["error"] and held == []


def test_an_answer_whose_turn_cant_start_fails_the_approval(client: TestClient, db: Path, turns: _Turns) -> None:  # noqa: F811
    aid = _insert(db, "Pick a name", needs="answer", question="Which name?")
    turns.refuse = RuntimeError("no turn today")
    res = client.post(f"/alerts/{aid}/answer", json={"answer": "Harbor"})
    assert res.status_code == 500
    approval = approvals.latest_for(aid)
    assert approval is not None and approval.status == approvals.STATUS_FAILED


def test_the_lead_gate_makes_no_card_for_an_app_call_naming_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import take_the_lead

    held: list[Any] = []
    ran: list[Any] = []
    monkeypatch.setattr(take_the_lead, "check", lambda *a, **k: take_the_lead.Hit("rule", "a rule"))
    monkeypatch.setattr(take_the_lead, "hold", lambda *a, **k: held.append(a) or 1)

    async def inner(tool_input: dict[str, Any]) -> str:
        ran.append(tool_input)
        return "{}"

    gated = take_the_lead.gated_call_tool(inner, source="scheduled")
    out = json.loads(asyncio.run(gated({"name": "", "arguments": {}})))
    assert "needs the tool's name" in out["error"] and held == [] and ran == []
    # A named tool with a hit still waits on a card.
    named = json.loads(asyncio.run(gated({"name": "some_server__do_the_thing", "arguments": {}})))
    assert named["status"] == "waiting_for_approval" and len(held) == 1 and ran == []
