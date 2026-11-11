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
from openexecutive.departments.authority import department_level as _company_level  # noqa: E402
from openexecutive.departments.models import AuthorityLevel
from openexecutive.memory import decision_ledger as ledger
from openexecutive.memory import episodic
from openexecutive.orchestrator import department_gate as gate
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
    ria_id = people_store.upsert_person(full_name="Ria Ops", email=RIA, department_slugs=["operations"])
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
    verdict = gate.decide(unknown, "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False and verdict.approver_person_id == team["owner"].id
    # No sign-in at all on the web is the operator, as before.
    assert gate.speaker(Session(from_web_chat=True)) is None


@pytest.mark.parametrize("channel", ["google_chat", "telegram", "discord", "slack"])
def test_a_chat_channel_sender_on_no_people_entry_is_gated(team: dict[str, Person], channel: str) -> None:
    unknown = gate.speaker(Session(origin_channel=channel, caller_person_id=None))
    assert unknown is not None and unknown.id is None
    assert gate.decide(unknown, "send_slack_dm", _send(), mcp=False).run is False


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


# ── the decision ──────────────────────────────────────────────────────────────


def _send(text: str = "Can we move the demo to Friday?") -> dict[str, Any]:
    return {"user_id": "U_RIA", "text": text}


def test_auto_execute_lets_it_run(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.AUTO_EXECUTE)
    verdict = gate.decide(team["sam"], "send_slack_dm", _send(), mcp=False)
    assert verdict.run is True and verdict.department == "marketing"


def test_propose_only_holds_it_for_an_approver(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    verdict = gate.decide(team["sam"], "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False
    assert verdict.approver_person_id == team["owner"].id
    assert verdict.level == "propose_only"


def test_balanced_lets_someone_who_approves_everything_send(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    people_store.set_authority_scope(team["sam"].id, [AuthorityScope.WILDCARD])  # type: ignore[arg-type]
    sam = _reload(team["sam"])
    assert gate.decide(sam, "send_slack_dm", _send("The invoice is $1,500"), mcp=False).run is True


def test_words_in_a_message_need_no_approval_range(team: dict[str, Person]) -> None:
    """Money, contract or hiring words no longer pick an approval range:
    the level alone decides."""
    _level("marketing", AuthorityLevel.AUTO_EXECUTE)
    sam = _reload(team["sam"])
    letter = _send("Send Bob the termination letter, effective today. Also expense the $12 lunch.")
    assert gate.decide(sam, "send_slack_dm", letter, mcp=False).run is True
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    people_store.set_authority_scope(team["sam"].id, [AuthorityScope.SPEND_LT_2K])  # type: ignore[arg-type]
    sam = _reload(team["sam"])
    assert gate.decide(sam, "send_slack_dm", _send("Expense the $12 lunch"), mcp=False).run is False


def test_the_company_level_decides_without_a_stored_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside the routing tests, a department runs at the company's
    Approvals level, whatever "How it acts" once stored."""
    from openexecutive.orchestrator import outside_mode

    for mode, level in (
        (outside_mode.TRUST, AuthorityLevel.AUTO_EXECUTE),
        (outside_mode.OUTSIDERS, AuthorityLevel.PROPOSE_ONLY),
        (outside_mode.ASK, AuthorityLevel.ESCALATE),
    ):
        monkeypatch.setattr(outside_mode, "get_mode", lambda m=mode, **_: m)
        assert _company_level("marketing") is level


def test_an_unreadable_level_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the Approvals level can't be read, or isn't one it knows, a
    department's work waits for an approver, as on Careful."""
    from openexecutive.orchestrator import outside_mode

    def broken(**_: object) -> str:
        raise RuntimeError("no settings")

    monkeypatch.setattr(outside_mode, "get_mode", broken)
    assert _company_level("marketing") is AuthorityLevel.ESCALATE
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: "unknown")
    assert _company_level("marketing") is AuthorityLevel.ESCALATE


def test_on_hands_off_someone_on_no_people_entry_still_waits(
    team: dict[str, Person], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmatched chat sender or a signed-in web caller on nobody's entry:
    Hands-off is the owner's trust in their own team, not in them."""
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    unmatched = gate.speaker(Session(origin_channel="slack", caller_person_id=None))
    assert unmatched is not None and unmatched.id is None
    verdict = gate.decide(unmatched, "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False and verdict.approver_person_id == team["owner"].id


def test_escalate_always_waits_for_someone_else(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.ESCALATE)
    people_store.set_authority_scope(team["sam"].id, [AuthorityScope.WILDCARD])  # type: ignore[arg-type]
    sam = _reload(team["sam"])
    verdict = gate.decide(sam, "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False
    assert verdict.approver_person_id == team["owner"].id
    assert verdict.level == "escalate"


def test_the_strictest_department_wins(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.AUTO_EXECUTE)
    _level("operations", AuthorityLevel.ESCALATE)
    people_store.upsert_person(
        full_name="Sam Seller", email=SAM, department_slugs=["marketing", "operations"],
        person_id=team["sam"].id,
    )
    sam = _reload(team["sam"])
    verdict = gate.decide(sam, "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False and verdict.department == "operations"


def test_no_department_waits_like_propose_only(team: dict[str, Person]) -> None:
    pid = people_store.upsert_person(full_name="New Hire", email="new@co.example")
    newbie = _reload(Person(id=pid, full_name=""))
    verdict = gate.decide(newbie, "send_slack_dm", _send(), mcp=False)
    assert verdict.run is False and verdict.department == ""


def test_a_message_only_to_the_owner_goes(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.ESCALATE)
    verdict = gate.decide(
        team["sam"], "message_person", {"person_id": team["owner"].id, "text": "FYI"}, mcp=False,
    )
    assert verdict.run is True


# ── holding it ────────────────────────────────────────────────────────────────


def _handlers(calls: list[Any]) -> dict[str, Any]:
    async def send(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return json.dumps({"status": "sent"})

    async def read(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return "[]"

    return {"send_slack_dm": send, "list_people": read, "assign_open_loop": send}


def test_a_held_send_becomes_a_card_and_does_nothing(
    team: dict[str, Person], events: list[tuple[str, dict[str, Any]]],
) -> None:
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    calls: list[Any] = []
    handlers = gate.gated_handlers(_handlers(calls), team["sam"], session_id="s-1")
    result = json.loads(asyncio.run(handlers["send_slack_dm"](_send())))
    assert result["status"] == "waiting_for_approval"
    assert "Olivia Owner" in result["message"] and "Do not retry" in result["message"]
    assert calls == []

    instance = ledger.get_decision_instance(result["decision_instance_id"])
    assert instance is not None
    assert instance.decision_class == gate.DECISION_CLASS
    assert instance.approver_person_id == team["owner"].id
    assert instance.originating_session_id == "s-1"
    payload = json.loads(instance.proposed_payload_json)
    assert payload["tool"] == "send_slack_dm" and payload["requested_by_person_id"] == team["sam"].id
    # The whole call in plain words, for the card to show before it can be approved.
    assert payload["details"] == [
        {"field": "User id", "value": "U_RIA"}, {"field": "Message", "value": _send()["text"]},
    ]

    [card] = alerts_store.list_alerts(limit=10)
    assert card.routed_to_person_id == team["owner"].id
    assert f"decision_instance:{instance.id}" in card.topic_tags
    assert card.headline.startswith("Sam Seller asked the Executive to:")
    assert [e for e, _ in events] == ["department_action_held"]
    assert events[0][1]["actor"] == f"person:{team['sam'].id}"

    # Asking again while it waits is the same card.
    again = json.loads(asyncio.run(handlers["send_slack_dm"](_send())))
    assert again["decision_instance_id"] == instance.id
    assert len(alerts_store.list_alerts(limit=10)) == 1


def test_reads_pass_untouched(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.ESCALATE)
    calls: list[Any] = []
    base = _handlers(calls)
    handlers = gate.gated_handlers(base, team["sam"])
    assert handlers["list_people"] is base["list_people"]
    assert handlers["send_slack_dm"] is not base["send_slack_dm"]


@pytest.mark.parametrize("level", [AuthorityLevel.ESCALATE, AuthorityLevel.PROPOSE_ONLY])
def test_the_chase_is_refused_under_escalate(team: dict[str, Person], level: AuthorityLevel) -> None:
    """Putting someone on the chase sends to them later, and can't wait on a
    card: under escalate it is refused, otherwise it runs."""
    _level("marketing", level)
    calls: list[Any] = []
    handlers = gate.gated_handlers(_handlers(calls), team["sam"])
    result = json.loads(asyncio.run(handlers["assign_open_loop"]({"person_id": 2, "task": "Chase Ria"})))
    if level == AuthorityLevel.ESCALATE:
        assert "error" in result and calls == []
    else:
        assert result["status"] == "sent" and len(calls) == 1


def test_auto_execute_runs_the_real_handler(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.AUTO_EXECUTE)
    calls: list[Any] = []
    handlers = gate.gated_handlers(_handlers(calls), team["sam"])
    assert json.loads(asyncio.run(handlers["send_slack_dm"](_send())))["status"] == "sent"
    assert calls == [_send()]


def test_a_connected_send_is_held_and_a_read_is_not(team: dict[str, Person]) -> None:
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    calls: list[Any] = []

    async def call_tool(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return "ok"

    gated = gate.gated_call_tool(call_tool, team["sam"])
    read = {"name": "search_gmail_messages", "arguments": {"query": "invoice"}}
    assert asyncio.run(gated(read)) == "ok"
    send = {"name": "send_gmail_message", "arguments": {"to": RIA, "subject": "Demo", "body": "Friday?"}}
    held = json.loads(asyncio.run(gated(send)))
    assert held["status"] == "waiting_for_approval"
    assert calls == [read]
    bad = json.loads(asyncio.run(gated({"name": "send_gmail_message", "arguments": "x"})))
    assert "error" in bad


def test_a_gate_that_cant_be_read_holds(team: dict[str, Person], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(gate, "decide", broken)
    calls: list[Any] = []
    handlers = gate.gated_handlers(_handlers(calls), team["sam"])
    result = json.loads(asyncio.run(handlers["send_slack_dm"](_send())))
    assert result["status"] == "waiting_for_approval" and calls == []


# ── approving it ──────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(decisions_route.router)
    return TestClient(app)


def _hold(team: dict[str, Person]) -> int:
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    handlers = gate.gated_handlers(_handlers([]), team["sam"])
    return int(json.loads(asyncio.run(handlers["send_slack_dm"](_send())))["decision_instance_id"])


def test_approving_does_the_exact_call_once(
    client: TestClient, team: dict[str, Person], monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    done: list[dict[str, Any]] = []

    async def carry_out(payload: dict[str, Any], *, by_principal: bool) -> str:
        done.append(payload)
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(ttl, "carry_out", carry_out)
    decision_id = _hold(team)
    response = client.post(f"/decisions/{decision_id}/approve", json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ledger.STATUS_APPROVED_UNCHANGED
    assert [(p["tool"], p["input"]) for p in done] == [("send_slack_dm", _send())]
    assert client.post(f"/decisions/{decision_id}/approve", json={}).status_code == 409
    [card] = alerts_store.list_alerts(limit=10, status="ack")
    assert card.external_id == f"decision:{decision_id}"
    assert [e for e, _ in events] == ["department_action_held", "department_action_done"]


def test_the_asker_cant_approve_their_own(client: TestClient, team: dict[str, Person]) -> None:
    decision_id = _hold(team)
    headers = {"x-caller-email": SAM}
    assert client.post(f"/decisions/{decision_id}/approve", json={}, headers=headers).status_code == 404
    instance = ledger.get_decision_instance(decision_id)
    assert instance is not None and instance.status == ledger.STATUS_PROPOSED


def test_declining_is_recorded(
    client: TestClient, team: dict[str, Person], events: list[tuple[str, dict[str, Any]]],
) -> None:
    decision_id = _hold(team)
    assert client.post(f"/decisions/{decision_id}/reject", json={}).status_code == 200
    assert [e for e, _ in events][-1] == "department_action_declined"


# ── wired into a chat turn ────────────────────────────────────────────────────


@pytest.mark.parametrize("hands_off", [False, True])
@pytest.mark.parametrize("who", ["sam", "owner"])
def test_a_teammates_chat_turn_goes_through_the_gate(
    team: dict[str, Person], monkeypatch: pytest.MonkeyPatch, who: str, hands_off: bool,
) -> None:
    """A team member's send waits; the owner's own goes, as before. Hands-off
    is the owner's trust in their own Executive: it doesn't waive the range
    they set for their people."""
    if hands_off:
        from openexecutive.orchestrator import outside_mode

        monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    from unittest.mock import patch

    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import current_session

    from .test_executive_tool_error_isolation import (
        _FinalMsg,
        _run_loop,
        _ScriptedProvider,
        _TextBlock,
        _tool_results,
        _ToolUseBlock,
    )

    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)
    _level("marketing", AuthorityLevel.PROPOSE_ONLY)
    sent: list[Any] = []

    async def send(tool_input: dict[str, Any]) -> str:
        sent.append(tool_input)
        return json.dumps({"status": "sent"})

    class _Gateway:
        async def search_tools(self, _input: dict[str, Any]) -> str:
            return json.dumps({"tools": []})

        async def call_tool(self, tool_input: dict[str, Any]) -> str:
            sent.append(tool_input)
            return "ok"

        async def load_mcp_server(self, _input: dict[str, Any]) -> str:
            return "{}"

    provider = _ScriptedProvider([
        _FinalMsg([
            _ToolUseBlock("tu-slack", "send_slack_dm", _send()),
            _ToolUseBlock("tu-mail", "call_tool", {
                "name": "send_gmail_message", "arguments": {"to": RIA, "subject": "Demo", "body": "Friday?"},
            }),
        ], stop_reason="tool_use"),
        _FinalMsg([_TextBlock("It's waiting.")], stop_reason="end_turn"),
    ])
    token = current_session.set(Session(caller_person_id=team[who].id))
    try:
        with patch.dict("openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS", {"send_slack_dm": send}):
            _run_loop(provider, Executive(mcp_gateway=_Gateway()))  # type: ignore[arg-type]
    finally:
        current_session.reset(token)

    results = _tool_results(provider, 1)
    if who == "owner":
        assert len(sent) == 2 and json.loads(results["tu-slack"])["status"] == "sent"
        assert alerts_store.list_alerts(limit=10) == []
        return
    assert sent == []
    assert json.loads(results["tu-slack"])["status"] == "waiting_for_approval"
    assert "waiting_for_approval" in results["tu-mail"]
    assert len(alerts_store.list_alerts(limit=10)) == 2


# ── several calls held together ───────────────────────────────────────────────


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
