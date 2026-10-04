"""Take the lead (orchestrator/take_the_lead.py): the Executive acts on its
own behind one gate, as the Executive and as you."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import take_the_lead as lead_route
from openexecutive.delegation import handle_it
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator import schedule_tools
from openexecutive.orchestrator import take_the_lead as ttl
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import AuthorityScope

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import DANA, NOW, OWNER, _msg

db = _inbox_tests.db
owner = _inbox_tests.owner

SAM = "sam@co.example"


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch, db: Path) -> Iterator[None]:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
    monkeypatch.setattr(alerts_store, "DB_PATH", db)
    alerts_store.initialize_db(db)
    yield


def _lead(**ask_first: bool) -> ttl.Lead:
    return ttl.Lead(scope=ttl.SCOPE_EXECUTIVE, enabled=True, ask_first={**dict.fromkeys(ttl.KINDS, True), **ask_first})


# ── the switches and rules ────────────────────────────────────────────────────


def test_off_until_turned_on_and_everything_asks_first() -> None:
    lead = ttl.get(ttl.SCOPE_EXECUTIVE)
    assert lead.enabled is False and all(lead.asks_first(k) for k in ttl.KINDS)
    assert ttl.executive_on() is False and ttl.as_you_on(1) is False


def test_it_stores_the_switch_and_ask_first() -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, ask_first={"money": False, "nonsense": False}, updated_by="t")
    lead = ttl.get(ttl.SCOPE_EXECUTIVE)
    assert lead.enabled and not lead.asks_first("money") and lead.asks_first("contracts")
    assert "nonsense" not in lead.ask_first
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=False, updated_by="t")
    assert ttl.get(ttl.SCOPE_EXECUTIVE).asks_first("money") is False  # kept when only the switch changes


@pytest.mark.parametrize(("kind", "value", "stored"), [
    ("domain", "@Acme.COM", "acme.com"),
    ("amount", "$2,500", "2500"),
    ("amount", "5k", "5000"),
    ("person", "Lee@Elsewhere.example", "lee@elsewhere.example"),
    ("words", "  board   meeting ", "board meeting"),
])
def test_rules_are_cleaned(kind: str, value: str, stored: str) -> None:
    assert ttl.clean_rule(kind, value) == stored


@pytest.mark.parametrize(("kind", "value"), [
    ("colour", "red"), ("domain", "not a domain"), ("amount", "lots"), ("words", "   "), ("words", "x" * 300),
])
def test_bad_rules_are_refused(kind: str, value: str) -> None:
    with pytest.raises(ttl.RuleError):
        ttl.clean_rule(kind, value)


def test_rules_belong_to_their_scope() -> None:
    company = ttl.add_rule(ttl.SCOPE_COMPANY, "words", "acquisition", created_by="t")
    mine = ttl.add_rule(ttl.person_scope(4), "domain", "rival.example", created_by="t")
    assert [r.id for r in ttl.list_rules([ttl.SCOPE_COMPANY])] == [company.id]
    assert ttl.delete_rule(company.id, ttl.person_scope(4)) is False  # not that scope's
    assert ttl.delete_rule(mine.id, ttl.person_scope(4)) is True
    assert ttl.list_rules([ttl.person_scope(4)]) == []


# ── the gate ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("tool", "tool_input", "kind"), [
    ("message_person", {"person_id": 1, "text": "Can you send the invoice for March?"}, "money"),
    ("message_person", {"person_id": 1, "text": "The quote came in at $4,000."}, "money"),
    ("message_person", {"person_id": 1, "text": "Please sign the NDA today."}, "contracts"),
    ("message_person", {"person_id": 1, "text": "We should hire a second engineer."}, "people_decisions"),
    ("cancel_calendar_event", {"event_id": "e1"}, "delete_share"),
    ("send_company_broadcast", {"text": "Office closed Friday."}, "big_send"),
    ("create_calendar_event", {"title": "Sync", "attendee_emails": ["stranger@far.example"]}, "someone_new"),
])
def test_the_base_kinds_wait(owner: Any, tool: str, tool_input: dict[str, Any], kind: str) -> None:
    hit = ttl.check(tool, tool_input, lead=_lead(), rules=[])
    assert hit is not None and hit.kind == kind
    assert ttl.check(tool, tool_input, lead=_lead(**{kind: False}), rules=[]) is None or kind == "someone_new"


def test_a_plain_message_to_someone_known_goes(owner: Any) -> None:
    assert ttl.check("message_person", {"person_id": 1, "text": "Are we still on for Thursday?"},
                     lead=_lead(), rules=[]) is None
    assert ttl.check("create_calendar_event", {"title": "Sync", "attendee_emails": [DANA]},
                     lead=_lead(), rules=[]) is None


def test_more_than_five_people_is_a_big_send(owner: Any) -> None:
    emails = [f"p{i}@co.example" for i in range(6)]
    hit = ttl.check("create_calendar_event", {"title": "All hands", "attendee_emails": emails},
                    lead=_lead(someone_new=False), rules=[])
    assert hit is not None and hit.kind == "big_send"


@pytest.mark.parametrize(("kind", "value", "text"), [
    ("words", "acquisition", "Quick note on the Acquisition timeline"),
    ("domain", "rival.example", "Hello ceo@rival.example"),
    ("person", "Jordan", "Ask Jordan about Thursday"),
    ("amount", "500", "It came to €750 in the end"),
])
def test_added_rules_always_hold(owner: Any, kind: str, value: str, text: str) -> None:
    rule = ttl.add_rule(ttl.SCOPE_COMPANY, kind, value, created_by="t")
    everything_off = _lead(**dict.fromkeys(ttl.KINDS, False))
    hit = ttl.check("message_person", {"person_id": 1, "text": text}, lead=everything_off,
                    rules=ttl.list_rules([ttl.SCOPE_COMPANY]))
    assert hit is not None and hit.rule_id == rule.id


def test_an_amount_rule_lets_smaller_amounts_through(owner: Any) -> None:
    ttl.add_rule(ttl.SCOPE_COMPANY, "amount", "1000", created_by="t")
    assert ttl.check("message_person", {"person_id": 1, "text": "Lunch was $40"},
                     lead=_lead(money=False), rules=ttl.list_rules([ttl.SCOPE_COMPANY])) is None


# ── acting under the gate ─────────────────────────────────────────────────────


def _handlers(calls: list[Any]) -> dict[str, Any]:
    async def message_person(tool_input: dict[str, Any]) -> str:
        calls.append(("message_person", tool_input))
        return json.dumps({"status": "sent"})

    async def list_people(tool_input: dict[str, Any]) -> str:
        calls.append(("list_people", tool_input))
        return "[]"

    return {"message_person": message_person, "list_people": list_people}


def test_a_clean_action_runs_and_is_on_what_i_did(owner: Any) -> None:
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = asyncio.run(gated["message_person"]({"person_id": owner.id, "text": "Are we on for Thursday?"}))
    assert json.loads(result)["status"] == "sent" and len(calls) == 1
    [line] = ttl.done([ttl.SCOPE_EXECUTIVE])
    assert line.status == "done" and line.source == "reflection" and "Olivia Owner" in line.summary


def test_reads_are_not_wrapped(owner: Any) -> None:
    handlers = _handlers([])
    assert ttl.gated_handlers(handlers, source="research")["list_people"] is handlers["list_people"]


def test_a_held_action_waits_on_today_for_the_principal(owner: Any) -> None:
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": owner.id, "text": "Pay the invoice"})))
    assert result["status"] == "waiting_for_approval" and calls == []
    [decision] = ledger.list_instances(ttl.DECISION_CLASS)
    assert decision.approver_person_id == owner.id and decision.status == ledger.STATUS_PROPOSED
    payload = json.loads(decision.proposed_payload_json)
    assert payload["tool"] == "message_person" and payload["kind"] == "money"
    [alert] = alerts_store.list_alerts(limit=10)
    assert alert.routed_to_person_id == owner.id and "private:principal" in alert.topic_tags
    assert ttl.done([ttl.SCOPE_EXECUTIVE])[0].status == "waiting"


def test_money_goes_to_whoever_holds_spending_authority(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import workspace_settings

    monkeypatch.setattr(workspace_settings, "effective_workspace_mode", lambda session=None: "team")
    cfo = people_store.upsert_person(full_name="Casey Finance", email="casey@co.example")
    people_store.set_authority_scope(cfo, [AuthorityScope.SPEND_LT_10K])
    people_registry.invalidate()
    decision_id = ttl.hold("message_person", {"person_id": owner.id, "text": "the $5,000 budget"},
                           ttl.Hit("money", "it's about money"), source="research", mcp=False)
    decision = ledger.get_decision_instance(decision_id)
    assert decision is not None and decision.approver_person_id == cfo
    [alert] = alerts_store.list_alerts(limit=10)
    assert "private:principal" not in alert.topic_tags  # a team approval, like a department's


@pytest.mark.parametrize(
    ("kind", "text", "ranges"),
    [
        ("money", "Pay the $450 invoice", ["spend_lt_2k", "spend_lt_10k", "spend_gt_10k"]),
        ("money", "Approve the 8k EUR quote", ["spend_lt_10k", "spend_gt_10k"]),
        ("money", "Wire $25,000 to the agency", ["spend_gt_10k"]),
        ("money", "Pay the invoice", ["spend_gt_10k"]),  # no amount reads as a large one
        ("money", "Extend net 60 payment terms to Acme", ["customer_credit"]),
        ("contracts", "Sign the MSA with our new supplier", ["vendor_onboarding", "legal_sign"]),
        ("contracts", "Send the NDA for signature", ["legal_sign"]),
        ("people_decisions", "Send Jo the offer letter", ["hiring_signoff"]),
        ("big_send", "Quarterly update to all investors", ["board_comms"]),
        ("someone_new", "Draft for the board meeting", ["board_comms"]),
        ("delete_share", "Share the board deck", []),
        ("big_send", "Office closed Friday", []),
    ],
)
def test_each_held_action_picks_its_approval_range(kind: str, text: str, ranges: list[str]) -> None:
    assert [r.value for r in ttl.approval_ranges(kind, text)] == ranges


def _team(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import workspace_settings

    monkeypatch.setattr(workspace_settings, "effective_workspace_mode", lambda session=None: "team")


def _holder(name: str, email: str, *scopes: AuthorityScope) -> int:
    person = people_store.upsert_person(full_name=name, email=email)
    people_store.set_authority_scope(person, list(scopes))
    people_registry.invalidate()
    return person


def _approver(owner: Any, kind: str, text: str) -> int | None:
    decision_id = ttl.hold("message_person", {"person_id": owner.id, "text": text},
                           ttl.Hit(kind, "held"), source="reflection", mcp=False)
    decision = ledger.get_decision_instance(decision_id)
    assert decision is not None
    return decision.approver_person_id


def test_a_small_spend_goes_up_to_the_next_range_held(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _team(monkeypatch)
    big = _holder("Bea Big", "bea@co.example", AuthorityScope.SPEND_GT_10K)
    assert _approver(owner, "money", "Pay the $300 invoice") == big
    small = _holder("Sam Small", "sam@co.example", AuthorityScope.SPEND_LT_2K)
    assert _approver(owner, "money", "Pay the $300 invoice") == small
    assert _approver(owner, "money", "Pay the $30,000 invoice") == big


def test_credit_vendors_and_board_reach_their_holders(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _team(monkeypatch)
    credit = _holder("Cal Credit", "cal@co.example", AuthorityScope.CUSTOMER_CREDIT)
    vendors = _holder("Val Vendor", "val@co.example", AuthorityScope.VENDOR_ONBOARDING)
    board = _holder("Bo Board", "bo@co.example", AuthorityScope.BOARD_COMMS)
    assert _approver(owner, "money", "Give Acme a line of credit") == credit
    assert _approver(owner, "contracts", "Sign the supplier agreement") == vendors
    assert _approver(owner, "big_send", "Note to all investors") == board
    # Nobody holds Legal or the spend ranges: those come to the principal.
    assert _approver(owner, "contracts", "Sign the NDA") == owner.id
    assert _approver(owner, "delete_share", "Share the board deck") == owner.id


def test_just_me_sends_every_held_action_to_the_owner(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import workspace_settings

    monkeypatch.setattr(workspace_settings, "effective_workspace_mode", lambda session=None: "solo")
    _holder("Cal Credit", "cal@co.example", AuthorityScope.CUSTOMER_CREDIT)
    assert _approver(owner, "money", "Give Acme a line of credit") == owner.id


def test_the_gate_fails_closed(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(scopes: list[str], **_: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(ttl, "list_rules", broken)
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": owner.id, "text": "hi"})))
    assert result["status"] == "waiting_for_approval" and calls == []


def test_mcp_reads_pass_and_sends_are_gated(owner: Any) -> None:
    seen: list[str] = []

    async def call_tool(tool_input: dict[str, Any]) -> str:
        seen.append(tool_input["name"])
        return "{}"

    gated = ttl.gated_call_tool(call_tool, source="scheduled")
    asyncio.run(gated({"name": "google_workspace__search_gmail_messages", "arguments": {"q": "invoice"}}))
    held = asyncio.run(gated({"name": "google_workspace__send_gmail_message",
                              "arguments": {"to": "x@far.example", "body": "Your invoice"}}))
    assert seen == ["google_workspace__search_gmail_messages"]
    assert json.loads(held)["status"] == "waiting_for_approval"


# ── what unattended passes get ────────────────────────────────────────────────


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"name": n} for n in names]


def test_off_solo_passes_keep_their_limits(owner: Any) -> None:
    calls: list[Any] = []
    tools, handlers = schedule_tools.unattended_toolkit(
        _tools("create_calendar_event", "message_person", "list_people"),
        {**_handlers(calls), "create_calendar_event": _handlers(calls)["message_person"]}, "solo",
    )
    assert [t["name"] for t in tools] == ["message_person", "list_people"]


def test_on_solo_passes_can_book_and_everything_is_gated(owner: Any) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="t")
    calls: list[Any] = []
    base = _handlers(calls)
    tools, handlers = schedule_tools.unattended_toolkit(
        _tools("create_calendar_event", "message_person", "list_people", "remember_fact"),
        {**base, "create_calendar_event": base["message_person"], "remember_fact": base["list_people"]},
        "solo", source="reflection",
    )
    assert [t["name"] for t in tools] == ["create_calendar_event", "message_person", "list_people"]
    assert handlers["message_person"] is not base["message_person"]
    assert handlers["list_people"] is base["list_people"]


# ── approving and declining ───────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(decisions_route.router)
    app.include_router(lead_route.router)
    return TestClient(app)


def _held(owner: Any) -> int:
    return ttl.hold("message_person", {"person_id": owner.id, "text": "Pay the invoice"},
                    ttl.Hit("money", "it's about money"), source="reflection", mcp=False)


def test_approving_does_the_exact_action_once(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []

    async def carry_out(payload: dict[str, Any], *, by_principal: bool) -> str:
        done.append(payload)
        assert by_principal is True
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(ttl, "carry_out", carry_out)
    decision_id = _held(owner)
    response = client.post(f"/decisions/{decision_id}/approve", json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ledger.STATUS_APPROVED_UNCHANGED
    assert [p["input"]["text"] for p in done] == ["Pay the invoice"]
    assert client.post(f"/decisions/{decision_id}/approve", json={}).status_code == 409
    assert ttl.done([ttl.SCOPE_EXECUTIVE])[0].status == "approved"
    [alert] = alerts_store.list_alerts(limit=10, status="ack")
    assert alert.external_id == f"decision:{decision_id}"


def test_carry_out_runs_the_real_handler(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive

    seen: list[Any] = []

    async def message_person(tool_input: dict[str, Any]) -> str:
        seen.append(tool_input)
        return "{}"

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "message_person", message_person)
    asyncio.run(ttl.carry_out({"tool": "message_person", "input": {"person_id": 1, "text": "x"}}, by_principal=True))
    assert seen == [{"person_id": 1, "text": "x"}]
    with pytest.raises(RuntimeError):
        asyncio.run(ttl.carry_out({"tool": "remember_fact", "input": {}}, by_principal=True))


def test_declining_drops_it(client: TestClient, owner: Any) -> None:
    decision_id = _held(owner)
    assert client.post(f"/decisions/{decision_id}/reject", json={}).status_code == 200
    assert ttl.done([ttl.SCOPE_EXECUTIVE])[0].status == "declined"


def test_a_teammate_cant_see_or_approve_someone_elses(client: TestClient, owner: Any) -> None:
    people_store.upsert_person(full_name="Sam Team", email=SAM)
    people_registry.invalidate()
    decision_id = _held(owner)
    headers = {"x-caller-email": SAM}
    assert client.get(f"/decisions/{decision_id}", headers=headers).status_code == 404
    assert client.post(f"/decisions/{decision_id}/approve", json={}, headers=headers).status_code == 404
    listed = client.get("/decisions", params={"decision_class": ttl.DECISION_CLASS}, headers=headers).json()
    assert listed == []


# ── the routes ────────────────────────────────────────────────────────────────


def test_the_owner_turns_it_on_and_adds_rules(client: TestClient, owner: Any) -> None:
    body = client.get("/take-the-lead").json()
    assert body["enabled"] is False and body["paused"] is False and len(body["ask_first"]) == 6
    body = client.put("/take-the-lead", json={"enabled": True, "ask_first": {"someone_new": False}}).json()
    assert body["enabled"] is True
    assert {a["kind"]: a["on"] for a in body["ask_first"]}["someone_new"] is False
    body = client.post("/take-the-lead/rules", json={"kind": "domain", "value": "rival.example"}).json()
    [rule] = body["rules"]
    assert client.post("/take-the-lead/rules", json={"kind": "amount", "value": "lots"}).status_code == 422
    assert client.delete(f"/take-the-lead/rules/{rule['id']}").json()["rules"] == []
    assert client.put("/take-the-lead", json={"ask_first": {"bogus": True}}).status_code == 422


def test_only_the_owner(client: TestClient, owner: Any) -> None:
    people_store.upsert_person(full_name="Sam Team", email=SAM)
    people_registry.invalidate()
    headers = {"x-caller-email": SAM}
    assert client.get("/take-the-lead", headers=headers).status_code == 403
    assert client.put("/take-the-lead", json={"enabled": True}, headers=headers).status_code == 403


def test_turning_it_on_needs_a_provable_owner(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    assert client.put("/take-the-lead", json={"enabled": True}).status_code == 409
    assert client.put("/take-the-lead", json={"enabled": False}).status_code == 200  # off is always allowed


def test_recent_activity_names_what_it_did_without_quoting(owner: Any) -> None:
    from openexecutive.api.routes import today

    ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="reflection", tool="message_person",
               summary="Message Dana", status="done")
    ttl.record(scope=ttl.SCOPE_EXECUTIVE, source="reflection", tool="message_person",
               summary="Message Sam", status="waiting")
    rows = [i for i in today._build_activity(50).items if i.kind == "took_the_lead"]
    assert [r.summary for r in rows] == ["Message Dana"]


def test_ask_first_lives_in_the_decision_ledger() -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, ask_first={"money": False}, updated_by="t")
    assert ledger.get_class_mode("take_the_lead_action:money") == "auto_execute"
    assert ledger.get_class_mode("take_the_lead_action:contracts") == "propose"


def test_sent_as_you_rows_are_the_callers_own(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes import chat, today
    from openexecutive.delegation.handle_it import HandledReply

    reply = HandledReply(
        decision_id=1, sent_at=NOW.isoformat(), to_name="Dana Park", to_email=DANA, subject="Thursday",
        body="Works for me", open_questions=[], thread_id="t1", source="reply",
    )
    monkeypatch.setattr(handle_it, "handled", lambda pid, **_: [reply] if pid == owner.id else [])
    monkeypatch.setattr(chat, "_resolve_caller_person_id", lambda request: owner.id)
    [row] = today._sent_as_caller(object())  # type: ignore[arg-type]
    assert row.kind == "sent_as_you" and row.actor == "you" and "Dana Park" in row.summary
    monkeypatch.setattr(chat, "_resolve_caller_person_id", lambda request: owner.id + 99)
    assert today._sent_as_caller(object()) == []  # type: ignore[arg-type]


# ── as you ────────────────────────────────────────────────────────────────────


def _check(owner: Any, *, relation: str = "stranger", body: str | None = None) -> str | None:
    from openexecutive.delegation.gmail import MailThread

    from .test_delegation_handle_it import _reply, _verdict

    message = _msg("m1", "t1")
    reply = _reply(body=body) if body is not None else _reply()
    return handle_it.refusal(
        owner.id, message, MailThread(id="t1", messages=[message]), reply, _verdict(confidence=0.6),
        relation=relation, own={OWNER}, exec_address="exec@co.example", now=NOW,
    )


def test_as_you_lifts_the_setting(owner: Any) -> None:
    handle_it.set_(owner.id, enabled=True, mode="careful", updated_by="t")
    assert _check(owner) == "level"
    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    assert _check(owner) is None
    assert _check(owner, body="Hi Dana, the total is $300 and the deck is at https://x.example/d") is None


def test_as_you_keeps_what_always_waits_and_adds_the_rules(owner: Any) -> None:
    handle_it.set_(owner.id, enabled=True, updated_by="t")
    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    assert _check(owner, body="Hi Dana, I'll send the contract over.") == "sensitive"
    ttl.add_rule(ttl.person_scope(owner.id), "words", "Thursday", created_by="t")
    assert _check(owner, body="Hi Dana, Thursday works.") == "lead_rule"


def test_as_you_does_nothing_while_handle_it_is_off(owner: Any) -> None:
    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    assert _check(owner) == "level"


# ── waking on what comes in ───────────────────────────────────────────────────


@pytest.fixture
def no_last_wake() -> Iterator[None]:
    ttl._LAST_WAKE.clear()
    yield
    ttl._LAST_WAKE.clear()


def _wakes() -> list[Any]:
    from openexecutive.memory.episodic import _get_conn

    with _get_conn() as conn:
        return conn.execute(
            "SELECT id, run_at, status FROM scheduled_actions WHERE kind = 'take_the_lead_wake'"
        ).fetchall()


def test_off_nothing_wakes(no_last_wake: None) -> None:
    assert ttl.wake("a message on slack", now=NOW) is None
    assert _wakes() == []


def test_on_new_messages_wake_the_reflection_once_in_a_while(no_last_wake: None) -> None:
    from datetime import timedelta

    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="t")
    first = ttl.wake("a message on email", now=NOW)
    assert first is not None
    assert ttl.wake("a message on slack", now=NOW + timedelta(minutes=1)) is None  # batched
    ttl._LAST_WAKE.clear()
    assert ttl.wake("a message on slack", now=NOW + timedelta(minutes=2)) is None  # one already waiting
    [row] = _wakes()
    assert row["run_at"] == (NOW + ttl.WAKE_AFTER).isoformat()


@pytest.mark.parametrize("on", [True, False])
def test_a_wake_runs_the_reflection_only_while_on(monkeypatch: pytest.MonkeyPatch, no_last_wake: None, on: bool) -> None:
    from openexecutive.memory.episodic import get_scheduled_action
    from openexecutive.scheduler import runner

    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="t")
    action_id = ttl.wake("a message on email", now=NOW)
    assert action_id is not None
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=on, updated_by="t")
    ran: list[str] = []

    async def reflect(now: Any, *, kind: str) -> None:
        ran.append(kind)

    monkeypatch.setattr(runner, "_reflect", reflect)
    action = get_scheduled_action(action_id)
    assert action is not None
    asyncio.run(runner._run_take_the_lead_wake(action, NOW))
    assert ran == (["take_the_lead_wake"] if on else [])
    done = get_scheduled_action(action_id)
    assert done is not None and done.status == "done"
