"""A team member's chat-turn sends and edits go through their department's
authority (orchestrator/department_gate.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openexecutive.audit as audit
from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import decisions as decisions_route
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.models import AuthorityLevel
from openexecutive.memory import decision_ledger as ledger
from openexecutive.memory import episodic
from openexecutive.orchestrator import department_gate as gate
from openexecutive.orchestrator import levels
from openexecutive.orchestrator import take_the_lead as ttl
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope, Person

pytestmark = pytest.mark.usefixtures("stored_department_levels")

SAM = "sam@co.example"
RIA = "ria@co.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    for module in (episodic, people_store, alerts_store, dept_store):
        monkeypatch.setattr(module, "DB_PATH", path)
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    alerts_store.initialize_db(path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments()
    people_registry.invalidate()
    dept_registry.invalidate()
    yield path
    people_registry.invalidate()
    dept_registry.invalidate()


@pytest.fixture(autouse=True)
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        seen.append((event_type, {"summary": summary, **kw}))

    monkeypatch.setattr(audit, "log_event", _log)
    return seen


@pytest.fixture
def team() -> dict[str, Person]:
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_store.set_authority_scope(owner_id, [AuthorityScope.WILDCARD])
    sam_id = people_store.upsert_person(full_name="Sam Seller", email=SAM, department_slugs=["marketing"])
    ria_id = people_store.upsert_person(
        full_name="Ria Ops", email=RIA, department_slugs=["operations"], slack_user_id="U_RIA",
    )
    people_registry.invalidate()
    people = {
        "owner": people_store.get_person(owner_id),
        "sam": people_store.get_person(sam_id),
        "ria": people_store.get_person(ria_id),
    }
    assert all(people.values())
    return people  # type: ignore[return-value]


def _level(slug: str, level: AuthorityLevel) -> None:
    dept_store.update_department(slug, authority_level=level)
    dept_registry.invalidate()


def _reload(person: Person) -> Person:
    people_registry.invalidate()
    fresh = people_store.get_person(person.id)  # type: ignore[arg-type]
    assert fresh is not None
    return fresh


# ── whose turn it is ──────────────────────────────────────────────────────────


def test_only_a_team_members_attended_turn_is_gated(team: dict[str, Person]) -> None:
    sam, owner = team["sam"], team["owner"]
    assert gate.speaker(Session(caller_person_id=sam.id)).id == sam.id  # type: ignore[union-attr]
    assert gate.speaker(Session(caller_person_id=owner.id)) is None
    assert gate.speaker(Session(caller_person_id=sam.id, unattended=True)) is None
    assert gate.speaker(Session(caller_person_id=None)) is None
    assert gate.speaker(None) is None
    contact = people_store.upsert_person(full_name="Dana Client", email="dana@else.example", kind="contact")
    assert gate.speaker(Session(caller_person_id=contact)) is None


def test_someone_signed_in_but_not_on_people_is_gated(team: dict[str, Person]) -> None:
    """Not the principal, so not ungated: an archived teammate still let in,
    or an allowlisted email, waits like someone in no department."""
    unknown = gate.speaker(Session(from_web_chat=True, web_caller_signed_in=True))
    assert unknown is not None and unknown.id is None
    # No sign-in at all on the web is the operator, as before.
    assert gate.speaker(Session(from_web_chat=True)) is None


@pytest.mark.parametrize("channel", ["google_chat", "telegram", "discord", "slack"])
def test_a_chat_channel_sender_on_no_people_entry_is_gated(team: dict[str, Person], channel: str) -> None:
    unknown = gate.speaker(Session(origin_channel=channel, caller_person_id=None))
    assert unknown is not None and unknown.id is None


@pytest.mark.parametrize("channel", ["google_chat", "telegram", "discord", "slack"])
def test_the_owner_on_a_chat_channel_is_not_gated(team: dict[str, Person], channel: str) -> None:
    owner = team["owner"]
    assert gate.speaker(Session(origin_channel=channel, caller_person_id=owner.id)) is None


def test_google_chat_gates_its_sender_by_their_people_entry(team: dict[str, Person]) -> None:
    """Google Chat names a sender on People as the turn's caller, as Slack
    does: the owner goes, a teammate gets their own department's authority."""
    owner, sam = team["owner"], team["sam"]
    assert gate.speaker(Session(origin_channel="google_chat", caller_person_id=owner.id)) is None
    teammate = gate.speaker(Session(origin_channel="google_chat", caller_person_id=sam.id))
    assert teammate is not None and teammate.id == sam.id


def test_email_turns_are_left_to_the_mailbox(team: dict[str, Person]) -> None:
    """Whoever writes in is never on People; the mailbox's own gates cover
    its replies."""
    assert gate.speaker(Session(origin_channel="email", caller_person_id=None)) is None


# ── the card a held step waits on ─────────────────────────────────────────────


def _send(text: str = "Can we move the demo to Friday?") -> dict[str, Any]:
    return {"user_id": "U_RIA", "text": text}


def _ask_as(person: Person, session_id: str = "s-1") -> Session:
    return Session(session_id=session_id, from_web_chat=True, web_caller_signed_in=True, caller_person_id=person.id)


def _hold_as(person: Person, reason: str = levels.OWN_MAIL, tool_input: dict[str, Any] | None = None) -> dict[str, Any]:
    return json.loads(levels.hold("send_slack_dm", tool_input or _send(), mcp=False, reason=reason,
                                  session=_ask_as(person)))


def test_there_is_no_department_gate_left(team: dict[str, Person]) -> None:
    """Approvals is the one gate: the department authority ranges no longer
    decide or wrap anything."""
    for gone in ("decide", "gated_handlers", "gated_call_tool", "GATED_TOOLS"):
        assert not hasattr(gate, gone), gone


def test_a_teammates_held_step_is_their_own_card(
    team: dict[str, Person], events: list[tuple[str, dict[str, Any]]],
) -> None:
    result = _hold_as(team["sam"])
    assert result["status"] == "waiting_for_approval" and "Do not retry" in result["message"]
    instance = ledger.get_decision_instance(result["decision_instance_id"])
    assert instance is not None
    assert instance.decision_class == gate.DECISION_CLASS
    # The level is theirs, so the card is theirs to approve.
    assert instance.approver_person_id == team["sam"].id
    assert instance.originating_session_id == "s-1"
    payload = json.loads(instance.proposed_payload_json)
    assert payload["tool"] == "send_slack_dm" and payload["requested_by_person_id"] == team["sam"].id
    assert payload["details"] == [
        {"field": "User id", "value": "U_RIA"}, {"field": "Message", "value": _send()["text"]},
    ]
    [card] = alerts_store.list_alerts(limit=10)
    assert card.routed_to_person_id == team["sam"].id
    assert f"decision_instance:{instance.id}" in card.topic_tags
    assert [e for e, _ in events] == ["department_action_held"]
    assert events[0][1]["actor"] == f"person:{team['sam'].id}"
    # Asking again while it waits is the same card.
    assert _hold_as(team["sam"])["decision_instance_id"] == instance.id
    assert len(alerts_store.list_alerts(limit=10)) == 1


@pytest.mark.parametrize("reason", [levels.STRANGER, levels.NOT_OWNER, levels.AI_APP])
def test_what_only_the_owner_may_allow_goes_to_the_owner(team: dict[str, Person], reason: str) -> None:
    instance = ledger.get_decision_instance(_hold_as(team["sam"], reason)["decision_instance_id"])
    assert instance is not None and instance.approver_person_id == team["owner"].id


def test_a_step_reaching_outside_the_team_goes_to_the_owner(team: dict[str, Person]) -> None:
    held = levels.hold(
        "google_workspace__send_gmail_message", {"to": "dana@else.example", "subject": "Hi", "body": "Hi"},
        mcp=True, reason=levels.OWN_MAIL, session=_ask_as(team["sam"]),
    )
    instance = ledger.get_decision_instance(json.loads(held)["decision_instance_id"])
    assert instance is not None and instance.approver_person_id == team["owner"].id


# ── approving it ──────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(decisions_route.router)
    return TestClient(app)


def _hold(team: dict[str, Person]) -> int:
    return int(_hold_as(team["sam"])["decision_instance_id"])


def test_approving_does_the_exact_call_once(
    client: TestClient, team: dict[str, Person], monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    done: list[dict[str, Any]] = []

    async def carry_out(payload: dict[str, Any], *, by_principal: bool, own_card: bool = False) -> str:
        done.append({**payload, "own_card": own_card})
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(ttl, "carry_out", carry_out)
    decision_id = _hold(team)
    headers = {"x-caller-email": SAM}
    response = client.post(f"/decisions/{decision_id}/approve", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ledger.STATUS_APPROVED_UNCHANGED
    assert [(p["tool"], p["input"]) for p in done] == [("send_slack_dm", _send())]
    # Sam asked and Sam approved: their own card (carry_out decides what that
    # may relax: only a step that runs on their own device).
    assert done[0]["own_card"] is True
    assert client.post(f"/decisions/{decision_id}/approve", json={}, headers=headers).status_code == 409
    [card] = alerts_store.list_alerts(limit=10, status="ack")
    assert card.external_id == f"decision:{decision_id}"
    assert [e for e, _ in events] == ["department_action_held", "department_action_done"]


def test_someone_else_cant_approve_a_teammates_card(client: TestClient, team: dict[str, Person]) -> None:
    decision_id = _hold(team)
    headers = {"x-caller-email": RIA}
    assert client.post(f"/decisions/{decision_id}/approve", json={}, headers=headers).status_code == 404
    instance = ledger.get_decision_instance(decision_id)
    assert instance is not None and instance.status == ledger.STATUS_PROPOSED


def test_declining_is_recorded(
    client: TestClient, team: dict[str, Person], events: list[tuple[str, dict[str, Any]]],
) -> None:
    decision_id = _hold(team)
    headers = {"x-caller-email": SAM}
    assert client.post(f"/decisions/{decision_id}/reject", json={}, headers=headers).status_code == 200
    assert [e for e, _ in events][-1] == "department_action_declined"


def test_an_email_card_can_be_changed_before_approving(
    client: TestClient, team: dict[str, Person], monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[dict[str, Any]] = []

    async def carry_out(payload: dict[str, Any], *, by_principal: bool, own_card: bool = False) -> str:
        done.append(payload)
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(ttl, "carry_out", carry_out)
    mail = {"to": "dana@else.example", "subject": "Hi", "body": "Draft text"}
    held = levels.hold("google_workspace__send_gmail_message", mail, mcp=True, reason=levels.OWN_MAIL,
                       session=_ask_as(team["sam"]))
    decision_id = json.loads(held)["decision_instance_id"]
    instance = ledger.get_decision_instance(decision_id)
    assert instance is not None
    fields = json.loads(instance.proposed_payload_json)["fields"]
    assert [f["field"] for f in fields] == ["subject", "body"]
    owner = {"x-caller-email": "olivia@co.example"}
    # Who it goes to is never changed from the card.
    bad = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"input": {"to": "x@else.example"}}},
                      headers=owner)
    assert bad.status_code == 422
    response = client.post(f"/decisions/{decision_id}/approve",
                           json={"edits": {"input": {"body": "Better text"}}}, headers=owner)
    assert response.status_code == 200, response.text
    assert [p["input"] for p in done] == [{**mail, "body": "Better text"}]


def _rename(n: int) -> dict[str, Any]:
    return {"file_id": f"f{n}", "name": f"2026 Tax - Lot {n}.pdf"}


def test_calls_held_together_are_one_card(team: dict[str, Person]) -> None:
    """Fourteen renames in one round: one card that lists them all."""
    from openexecutive.orchestrator.department_gate import Verdict

    verdict = Verdict(run=False, approver_person_id=team["owner"].id, reason="it read your own mail first")
    steps = [_rename(n) for n in range(1, 4)]
    decision_id = gate.hold(
        team["owner"], "google_workspace__update_drive_file", steps[0], verdict,
        mcp=True, held=True, more=steps[1:],
    )
    instance = ledger.get_decision_instance(decision_id)
    assert instance is not None
    payload = json.loads(instance.proposed_payload_json)
    assert payload["steps"] == steps and payload["input"] == steps[0]
    assert payload["fields"] == [] and "2 more like it" in payload["summary"]
    [card] = alerts_store.list_alerts(limit=10)
    assert all(f"Lot {n}" in card.body for n in range(1, 4))
    # The same calls still waiting are that card, not a new one.
    assert gate.hold(
        team["owner"], "google_workspace__update_drive_file", steps[0], verdict,
        mcp=True, held=True, more=steps[1:],
    ) == decision_id



def test_a_card_for_several_calls_shows_every_one(team: dict[str, Person]) -> None:
    """The web card and the chat-app card show each call Approve runs, not
    only the first."""
    from openexecutive.api.routes.decisions import _served
    from openexecutive.integrations.channel_cards import card_text
    from openexecutive.orchestrator.department_gate import Verdict

    verdict = Verdict(run=False, approver_person_id=team["owner"].id, reason="it read your own mail first")
    steps = [_rename(n) for n in range(1, 4)]
    decision_id = gate.hold(
        team["owner"], "google_workspace__update_drive_file", steps[0], verdict,
        mcp=True, held=True, more=steps[1:],
    )
    instance = ledger.get_decision_instance(decision_id)
    assert instance is not None
    served = json.loads(_served(instance).proposed_payload_json)
    shown = " ".join(d["value"] for d in served["details"])
    assert all(f"Lot {n}" in shown for n in range(1, 4))
    assert {d["field"].split(".")[0] for d in served["details"]} == {"1", "2", "3"}
    text = card_text(decision_id)
    assert text is not None and all(f"Lot {n}" in text for n in range(1, 4))


def test_one_tap_carries_out_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import mcp_gateway

    calls: list[dict[str, Any]] = []

    class Gateway:
        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            calls.append(tool_input)
            if tool_input["arguments"]["file_id"] == "f3":
                return json.dumps({"error": "no such file"})
            return "Renamed."

    monkeypatch.setattr(mcp_gateway, "get_active_gateway", lambda: Gateway())
    steps = [_rename(n) for n in range(1, 3)]
    payload = {"tool": "google_workspace__update_drive_file", "input": steps[0], "mcp": True,
               "held": True, "steps": steps}
    done = json.loads(asyncio.run(ttl.carry_out(payload, by_principal=True)))
    assert done["done"] == 2 and [c["arguments"] for c in calls] == steps
    # It stops at the first that fails: nothing after it runs.
    calls.clear()
    failed = asyncio.run(ttl.carry_out({**payload, "steps": [*steps, _rename(3), _rename(4)]}, by_principal=True))
    assert ttl.result_failed(failed)
    error = json.loads(failed)
    assert error["done"] == 2 and "step 3 didn't" in error["error"] and "rest didn't run" in error["error"]
    assert [c["arguments"]["file_id"] for c in calls] == ["f1", "f2", "f3"]


def test_a_step_that_raises_stops_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import mcp_gateway

    calls: list[str] = []

    class Gateway:
        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            calls.append(tool_input["arguments"]["file_id"])
            if tool_input["arguments"]["file_id"] == "f2":
                raise TimeoutError("timed out")
            return "Renamed."

    monkeypatch.setattr(mcp_gateway, "get_active_gateway", lambda: Gateway())
    steps = [_rename(n) for n in range(1, 4)]
    payload = {"tool": "google_workspace__update_drive_file", "input": steps[0], "mcp": True,
               "held": True, "steps": steps}
    out = json.loads(asyncio.run(ttl.carry_out(payload, by_principal=True)))
    assert out["done"] == 1 and "timed out" in out["error"] and calls == ["f1", "f2"]
