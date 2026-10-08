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
    # Recent activity also lists workflow runs, whose store binds its own path.
    from openexecutive.workflows import persistence as wf_persistence

    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    wf_persistence.initialize_runs_db(db)
    yield


def _lead(**ask_first: bool) -> ttl.Lead:
    return ttl.Lead(scope=ttl.SCOPE_EXECUTIVE, enabled=True, ask_first={**dict.fromkeys(ttl.KINDS, True), **ask_first})


# ── the switches and rules ────────────────────────────────────────────────────


def test_off_until_turned_on_and_everything_asks_first() -> None:
    lead = ttl.get(ttl.SCOPE_EXECUTIVE)
    assert lead.enabled is False and all(lead.asks_first(k) for k in ttl.KINDS)
    assert ttl.executive_on() is False and ttl.as_you_on(1) is False


def test_every_kind_has_a_label_and_a_hint() -> None:
    assert set(ttl.KIND_LABELS) == set(ttl.KIND_HINTS) == set(ttl.KINDS)
    assert str(ttl.BIG_SEND_RECIPIENTS) in ttl.KIND_HINTS[ttl.BIG_SEND]


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
    ("amount", "1000000", "1000000"),
    ("amount", "1,234,567", "1234567"),
    ("amount", "2.5m", "2500000"),
    ("amount", "99.5", "99.5"),
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


@pytest.mark.parametrize(("kind", "value"), [("person", "ceo@acme.example"), ("domain", "acme.example"),
                                            ("person", "Casey Chief")])
def test_rules_see_who_an_id_addressed_message_reaches(owner: Any, kind: str, value: str) -> None:
    ceo = people_store.upsert_person(full_name="Casey Chief", email="ceo@acme.example")
    rule = ttl.add_rule(ttl.SCOPE_COMPANY, kind, value, created_by="t")
    hit = ttl.check("message_person", {"person_id": ceo, "text": "Quick update"},
                    lead=_lead(**dict.fromkeys(ttl.KINDS, False)), rules=ttl.list_rules([ttl.SCOPE_COMPANY]))
    assert hit is not None and hit.rule_id == rule.id


def test_a_message_to_an_unknown_id_is_someone_new(owner: Any) -> None:
    hit = ttl.check("send_slack_dm", {"user_id": "U-NOBODY", "text": "hi"}, lead=_lead(), rules=[])
    assert hit is not None and hit.kind == "someone_new"
    assert ttl.check("message_person", {"person_id": owner.id, "text": "hi"}, lead=_lead(), rules=[]) is None


def test_a_name_rule_matches_whole_words(owner: Any) -> None:
    ttl.add_rule(ttl.SCOPE_COMPANY, "person", "Ed", created_by="t")
    rules = ttl.list_rules([ttl.SCOPE_COMPANY])
    nothing = _lead(**dict.fromkeys(ttl.KINDS, False))
    assert ttl.check("message_person", {"person_id": owner.id, "text": "We need the metal schedule"},
                     lead=nothing, rules=rules) is None
    assert ttl.check("message_person", {"person_id": owner.id, "text": "Ask Ed first"},
                     lead=nothing, rules=rules) is not None


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


def _teammate() -> int:
    person = people_store.upsert_person(full_name="Sam Teammate", email=SAM)
    people_registry.invalidate()
    return person


def test_a_held_action_waits_on_today_for_the_principal(owner: Any) -> None:
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": _teammate(), "text": "Pay the invoice"})))
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
    # On the team's Today it shows only the plain line, not what it would say.
    assert "budget" not in alert.headline and "budget" not in (alert.body or "")
    assert "budget" in json.loads(decision.proposed_payload_json)["summary"]


def test_the_same_waiting_action_is_one_card(owner: Any) -> None:
    first = _held(owner)
    assert _held(owner) == first
    assert len(alerts_store.list_alerts(limit=10)) == 1
    ledger.mark_resolved(first, ledger.STATUS_REJECTED)
    assert _held(owner) != first  # once answered, a new one is a new card


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
    ledger.mark_resolved(decision_id, ledger.STATUS_REJECTED)  # so the next hold is a new card
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


def test_an_action_reaching_a_contact_goes_to_the_principal(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _team(monkeypatch)
    _holder("Bea Big", "bea@co.example", AuthorityScope.SPEND_GT_10K)
    client = people_store.upsert_person(full_name="Cleo Client", email="cleo@client.example", kind="contact")
    people_registry.invalidate()
    for tool_input in ({"person_id": client, "text": "Pay the $30,000 invoice"},
                       {"text": "Email cleo@client.example: pay the $30,000 invoice"}):
        decision_id = ttl.hold("message_person", tool_input, ttl.Hit("money", "held"),
                               source="reflection", mcp=False)
        decision = ledger.get_decision_instance(decision_id)
        assert decision is not None and decision.approver_person_id == owner.id
        ledger.mark_resolved(decision_id, ledger.STATUS_REJECTED)


def test_just_me_routes_to_approval_ranges_too(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import workspace_settings

    monkeypatch.setattr(workspace_settings, "effective_workspace_mode", lambda session=None: "solo")
    credit = _holder("Cal Credit", "cal@co.example", AuthorityScope.CUSTOMER_CREDIT)
    assert _approver(owner, "money", "Give Acme a line of credit") == credit
    assert _approver(owner, "contracts", "Sign the NDA") == owner.id


def test_the_gate_fails_closed(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(scopes: list[str], **_: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(ttl, "list_rules", broken)
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": _teammate(), "text": "hi"})))
    assert result["status"] == "waiting_for_approval" and calls == []


def test_a_message_only_to_the_principal_just_goes_to_them(owner: Any) -> None:
    # They're who it would wait for: approving it would only show them what it says.
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    text = "Heads up: the supplier agreement renewal needs your signature by Friday."
    assert ttl.check("message_person", {"person_id": owner.id, "text": text}, lead=_lead(), rules=[]) is not None
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": owner.id, "text": text})))
    assert result["status"] == "sent" and calls == [("message_person", {"person_id": owner.id, "text": text})]
    assert ledger.list_instances(ttl.DECISION_CLASS) == [] and alerts_store.list_alerts(limit=10) == []
    [line] = ttl.done([ttl.SCOPE_EXECUTIVE])
    assert line.status == "done"


def test_the_same_message_to_anyone_else_still_waits(owner: Any) -> None:
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    client = people_store.upsert_person(full_name="Cleo Client", email="cleo@client.example", kind="contact")
    people_registry.invalidate()
    text = "The supplier agreement renewal needs a signature by Friday."
    for person_id in (_teammate(), client, 9999):
        result = json.loads(asyncio.run(gated["message_person"]({"person_id": person_id, "text": text})))
        assert result["status"] == "waiting_for_approval"
    assert calls == []


def test_only_a_direct_message_to_the_principal_skips_the_gate(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ttl._only_to_principal("message_person", {"person_id": owner.id, "text": "Sign the NDA"})
    # Acting on them, or reaching others through them, is not a message to them.
    assert not ttl._only_to_principal("assign_open_loop", {"person_id": owner.id, "text": "Sign the NDA"})
    assert not ttl._only_to_principal("archive_person", {"person_id": owner.id})
    assert not ttl._only_to_principal("send_company_broadcast", {"text": "Sign the NDA"})
    assert not ttl._only_to_principal("message_person", {"text": "Sign the NDA"})

    def broken(person_id: int) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(people_store, "get_person", broken)
    assert not ttl._only_to_principal("message_person", {"person_id": owner.id, "text": "Sign the NDA"})


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


@pytest.mark.parametrize(("name", "acts"), [
    ("google_workspace__search_gmail_messages", False),
    ("google_workspace__get_doc_content", False),
    ("ms365__list_calendar_events", False),
    ("google_workspace__modify_sheet_values", True),
    ("google_workspace__append_table_rows", True),
    ("google_workspace__batch_update_doc", True),
    ("ms365__upload_file", True),
    ("google_workspace__insert_doc_elements", True),
    ("some_server__do_the_thing", True),  # not plainly a read, so it's gated
    ("google_workspace__get_and_clear_values", True),
    ("google_workspace__mark_as_read", True),
    ("crm__get_and_pay_invoice", True),
    ("ops__list_and_dispatch", True),
    ("mail__unsubscribe_from_list", True),
    ("drive.searchFiles", False),
    ("ms365__read_mail_message", False),
])
def test_connected_tools_fail_closed(name: str, acts: bool) -> None:
    assert ttl.mcp_tool_acts(name) is acts


def test_clearing_a_sheet_counts_as_deleting(owner: Any) -> None:
    hit = ttl.check("google_workspace__clear_sheet_values", {"spreadsheet_id": "abc"}, lead=_lead(), rules=[], mcp=True)
    assert hit is not None and hit.kind == ttl.DELETE_SHARE


def test_connected_tool_arguments_must_be_an_object(owner: Any) -> None:
    seen: list[str] = []

    async def call_tool(tool_input: dict[str, Any]) -> str:
        seen.append(tool_input["name"])
        return "{}"

    gated = ttl.gated_call_tool(call_tool, source="scheduled")
    out = asyncio.run(gated({"name": "google_workspace__send_gmail_message", "arguments": "[1, 2]"}))
    assert ttl.result_failed(out) and seen == []


def test_a_large_amount_rule_holds_only_larger_amounts() -> None:
    rule = ttl.add_rule(ttl.SCOPE_COMPANY, "amount", "1000000", created_by="t")
    assert rule.value == "1000000"
    lead = _lead()
    rules = ttl.list_rules([ttl.SCOPE_COMPANY])
    assert ttl._rule_hit(rules, "Pay $2,000,000 to Acme", []) is not None
    assert ttl._rule_hit(rules, "Pay $500 to Acme", []) is None
    assert lead.enabled


@pytest.mark.parametrize(("text", "expected"), [
    ("approve a $2 million wire", [2_000_000.0]),
    ("2 million dollars to Acme", [2_000_000.0]),
    ("$1.5bn deal", [1_500_000_000.0]),
    ("$3 thousand", [3_000.0]),
    ("2k EUR", [2_000.0]),
    ("$2 more for lunch", [2.0]),
    ("$5 buys a coffee", [5.0]),
    ("transfer USD 50,000 Friday", [50_000.0]),
    ("EUR 20k", [20_000.0]),
    ("CHF 5000 and 300 chf", [5_000.0, 300.0]),
])
def test_amounts_read_scale_words(text: str, expected: list[float]) -> None:
    assert ttl.amounts(text) == expected


def test_money_holds_a_scaled_amount(owner: Any) -> None:
    hit = ttl.check("message_person", {"person_id": owner.id, "text": "Please send 2k EUR to Acme today"},
                    lead=_lead(), rules=[])
    assert hit is not None and hit.kind == ttl.MONEY


def test_money_and_amount_rules_read_a_code_before_the_number(owner: Any) -> None:
    text = "Confirm we'll transfer USD 50,000 to them Friday"
    hit = ttl.check("message_person", {"person_id": owner.id, "text": text}, lead=_lead(), rules=[])
    assert hit is not None and hit.kind == ttl.MONEY
    ttl.add_rule(ttl.SCOPE_COMPANY, "amount", "10000", created_by="t")
    assert ttl._rule_hit(ttl.list_rules([ttl.SCOPE_COMPANY]), text, []) is not None


def test_an_amount_rule_holds_a_wire_written_in_words() -> None:
    ttl.add_rule(ttl.SCOPE_COMPANY, "amount", "500", created_by="t")
    rules = ttl.list_rules([ttl.SCOPE_COMPANY])
    assert ttl._rule_hit(rules, "Approve a $2 million wire to Acme", []) is not None
    assert ttl.add_rule(ttl.SCOPE_COMPANY, "amount", "2 million", created_by="t").value == "2000000"


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
    assert all(a["hint"] for a in body["ask_first"])
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
    # Confidence (0.6 here) and amounts give way; a link still waits on a tap.
    assert _check(owner, body="Hi Dana, the total is $300.") is None
    assert _check(owner, body="Hi Dana, the deck is at https://x.example/d") == "link"


def test_as_you_keeps_what_always_waits_and_adds_the_rules(owner: Any) -> None:
    handle_it.set_(owner.id, enabled=True, updated_by="t")
    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    assert _check(owner, body="Hi Dana, I'll send the contract over.") == "sensitive"
    ttl.add_rule(ttl.person_scope(owner.id), "words", "Thursday", created_by="t")
    assert _check(owner, body="Hi Dana, Thursday works.") == "lead_rule"


def test_as_you_does_nothing_while_handle_it_is_off(owner: Any) -> None:
    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    assert _check(owner) == "level"


def test_turning_handle_it_off_turns_the_lead_as_you_off(owner: Any) -> None:
    from openexecutive.api.routes import delegation as delegation_route

    ttl.set_(ttl.person_scope(owner.id), enabled=True, updated_by="t")
    delegation_route._set_handle_it(owner.id, enabled=False, mode=None)
    assert not ttl.as_you_on(owner.id)


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


def test_a_failed_wake_doesnt_hold_off_the_next(monkeypatch: pytest.MonkeyPatch, no_last_wake: None) -> None:
    from openexecutive.memory import episodic

    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="t")
    real = episodic.insert_scheduled_action
    locked = [True]

    def insert(**kwargs: Any) -> int:
        if locked[0]:
            raise RuntimeError("database is locked")
        return real(**kwargs)

    monkeypatch.setattr(episodic, "insert_scheduled_action", insert)
    assert ttl.wake("a message on email", now=NOW) is None
    locked[0] = False
    assert ttl.wake("a message on email", now=NOW) is not None


@pytest.mark.parametrize(("text", "failed"), [
    ('{"error": "not found"}', True),
    ('{"status": "sent", "subject": "Re: \\"error\\" in invoice"}', False),
    ('Sent. They wrote: {"error": "x"}', False),
    ("[]", False),
    ("", False),
])
def test_only_a_json_error_counts_as_failed(text: str, failed: bool) -> None:
    assert ttl.result_failed(text) is failed


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


# ── the leading pass finds and uses the deployment's tools ───────────────────


class _Gateway:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def search_tools(self, tool_input: dict[str, Any]) -> str:
        return json.dumps({"tools": [{"name": "google_workspace__create_spreadsheet"}]})

    async def call_tool(self, tool_input: dict[str, Any]) -> str:
        self.called.append(tool_input["name"])
        return json.dumps({"ok": True})


def _lead_tools(monkeypatch: pytest.MonkeyPatch, gateway: Any) -> tuple[list[str], dict[str, Any], bool]:
    from openexecutive.orchestrator import mcp_gateway
    from openexecutive.workflows import executive_reflection

    monkeypatch.setattr(mcp_gateway, "get_active_gateway", lambda: gateway)
    tools, handlers, leading = executive_reflection._with_lead_tools([{"name": "message_person"}], {})
    return [t["name"] for t in tools], handlers, leading


def test_the_pass_gets_no_gateway_tools_while_off(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    names, handlers, leading = _lead_tools(monkeypatch, _Gateway())
    assert names == ["message_person"] and handlers == {} and not leading


def test_the_leading_pass_can_discover_and_use_connected_tools(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="test")
    gateway = _Gateway()
    names, handlers, leading = _lead_tools(monkeypatch, gateway)
    assert leading and names == ["call_tool", "message_person", "search_tools"]
    assert "load_mcp_server" not in handlers  # no new connections unattended
    found = asyncio.run(handlers["search_tools"]({"query": "spreadsheet"}))
    assert "create_spreadsheet" in found
    asyncio.run(handlers["call_tool"]({"name": "google_workspace__create_spreadsheet",
                                       "arguments": {"title": "Project tracker"}}))
    assert gateway.called == ["google_workspace__create_spreadsheet"]
    # Sharing it is held for a yes, like any delete or share.
    held = asyncio.run(handlers["call_tool"]({"name": "google_workspace__share_drive_file",
                                              "arguments": {"file_id": "f1"}}))
    assert json.loads(held)["status"] == "waiting_for_approval"
    assert gateway.called == ["google_workspace__create_spreadsheet"]


def test_the_leading_pass_without_a_gateway_still_leads(owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, updated_by="test")
    names, handlers, leading = _lead_tools(monkeypatch, None)
    assert leading and names == ["message_person"]


@pytest.mark.parametrize(
    ("tool", "arguments", "quote", "expected"),
    [
        ("google_workspace__modify_sheet_values", {"title": "Supplier deliveries", "range": "A1"}, False,
         "Update Supplier deliveries (Google Sheets)"),
        ("google_workspace__create_doc", {"title": "Plant review prep", "content": "secret plan"}, False,
         "Create Plant review prep (Google Docs)"),
        ("google_workspace__share_drive_file",
         {"name": "Plant review prep", "email_address": "plant-team@halcyonmotors.com"}, True,
         "Share Plant review prep with plant-team@halcyonmotors.com"),
        # The activity feed never names who it was shared with.
        ("google_workspace__share_drive_file",
         {"name": "Plant review prep", "email_address": "plant-team@halcyonmotors.com"}, False,
         "Share Plant review prep"),
        ("google_workspace__modify_sheet_values", {"spreadsheet_id": "1abc"}, False,
         "Update a spreadsheet (Google Sheets)"),
        ("crm__update_record", {"name": "Acme renewal"}, False, "Update Acme renewal"),
        ("crm__send_gmail_message", {"to": "x@far.example"}, False, "Send gmail message"),
    ],
)
def test_connected_tools_are_named_by_file_not_content(
    tool: str, arguments: dict[str, Any], quote: bool, expected: str,
) -> None:
    assert ttl.summarize(tool, arguments, mcp=True, quote=quote) == expected


# ── in training ───────────────────────────────────────────────────────────────


def _training() -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, training=True, updated_by="t")


def test_in_training_a_clean_action_waits_for_the_principal(owner: Any) -> None:
    _training()
    sam = _teammate()
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": sam, "text": "Are we on for Thursday?"})))
    assert result["status"] == "waiting_for_approval" and calls == []
    [decision] = ledger.list_instances(ttl.DECISION_CLASS)
    payload = json.loads(decision.proposed_payload_json)
    assert payload["kind"] == ttl.TRAINING and decision.approver_person_id == owner.id
    assert payload["allow"] == {"key": f"take_the_lead|message|person:{sam}", "label": "Message Sam Teammate"}
    assert payload["fields"] == [
        {"field": "text", "label": "Message", "long": True, "value": "Are we on for Thursday?"},
    ]
    [alert] = alerts_store.list_alerts(limit=10)
    assert ttl.TRAINING_TAG in alert.topic_tags and "private:principal" in alert.topic_tags


def test_in_training_an_allowed_action_goes_and_is_counted(owner: Any) -> None:
    _training()
    sam = _teammate()
    key, label = ttl.allowance("message_person", {"person_id": sam, "text": "x"})  # type: ignore[misc]
    ttl.allow(key, label, created_by="t")
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    asyncio.run(gated["message_person"]({"person_id": sam, "text": "Are we on for Thursday?"}))
    assert len(calls) == 1
    [line] = ttl.done([ttl.SCOPE_EXECUTIVE])
    assert line.status == "done" and "You allowed this: Message Sam Teammate." in line.why
    [learned] = ttl.list_allowed()
    assert learned.uses == 1 and learned.feature == "take_the_lead"


def test_allowing_never_lets_the_six_kinds_or_rules_through(owner: Any) -> None:
    _training()
    sam = _teammate()
    key, label = ttl.allowance("message_person", {"person_id": sam, "text": "x"})  # type: ignore[misc]
    ttl.allow(key, label, created_by="t")
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    result = json.loads(asyncio.run(gated["message_person"]({"person_id": sam, "text": "Pay the invoice"})))
    assert result["status"] == "waiting_for_approval" and calls == []
    payload = json.loads(ledger.list_instances(ttl.DECISION_CLASS)[0].proposed_payload_json)
    assert payload["kind"] == "money" and "allow" not in payload


def test_on_without_training_nothing_extra_waits(owner: Any) -> None:
    ttl.set_(ttl.SCOPE_EXECUTIVE, enabled=True, training=False, updated_by="t")
    calls: list[Any] = []
    gated = ttl.gated_handlers(_handlers(calls), source="reflection")
    asyncio.run(gated["message_person"]({"person_id": _teammate(), "text": "Are we on for Thursday?"}))
    assert len(calls) == 1


def test_what_one_allow_covers(owner: Any) -> None:
    sam = _teammate()
    # Any channel that reaches Sam is one "Message Sam".
    assert ttl.allowance("message_person", {"person_id": sam})[0] == f"take_the_lead|message|person:{sam}"  # type: ignore[index]
    book = ttl.allowance("create_calendar_event", {"attendee_person_ids": [sam, owner.id]})
    assert book == (f"take_the_lead|book|people:{min(sam, owner.id)},{max(sam, owner.id)}",
                    "Book meetings with " + " and ".join(
                        ["Olivia Owner", "Sam Teammate"] if owner.id < sam else ["Sam Teammate", "Olivia Owner"]))
    # Someone not on the People list can't be allowed.
    assert ttl.allowance("message_person", {"person_id": 9999}) is None
    assert ttl.allowance("create_calendar_event", {"attendee_person_ids": [9999]}) is None
    assert ttl.allowance("run_workflow", {"workflow_id": "weekly_review"}) == (
        "take_the_lead|workflow|weekly_review", "Start the weekly_review workflow")


def test_edits_change_only_editable_fields() -> None:
    tool_input = {"person_id": 3, "text": "Hi"}
    out, changed = ttl.apply_edits("message_person", tool_input, {"text": " Hi Sam, Thursday? "})
    assert out == {"person_id": 3, "text": "Hi Sam, Thursday?"} and changed == {"text": "Hi Sam, Thursday?"}
    assert ttl.apply_edits("message_person", tool_input, {"text": "Hi"}) == (tool_input, {})
    for bad in ({"person_id": "4"}, {"text": ""}, {"text": 5}, {"text": "x" * (ttl.EDIT_MAX + 1)}, ["text"]):
        with pytest.raises(ttl.EditError):
            ttl.apply_edits("message_person", tool_input, bad)
    with pytest.raises(ttl.EditError):
        ttl.apply_edits("gsheets__update", {"title": "x"}, {"title": "y"}, mcp=True)


def _training_card(owner: Any) -> tuple[int, int]:
    _training()
    sam = _teammate()
    decision_id = ttl.hold("message_person", {"person_id": sam, "text": "Thursday?"},
                           ttl.Hit(ttl.TRAINING, "it's in training"), source="reflection", mcp=False)
    return decision_id, sam


def _carry_out_into(done: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    async def carry_out(payload: dict[str, Any], *, by_principal: bool) -> str:
        done.append(payload)
        return json.dumps({"status": "sent"})

    monkeypatch.setattr(ttl, "carry_out", carry_out)


def test_approve_and_allow(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id, _ = _training_card(owner)
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"allow": True}})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ledger.STATUS_APPROVED_UNCHANGED and len(done) == 1
    [learned] = client.get("/take-the-lead").json()["learned"]
    assert learned["label"] == "Message Sam Teammate" and learned["example"] == {}


def test_edit_then_do_it_like_this(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id, _ = _training_card(owner)
    edits = {"input": {"text": "Hi Sam, still on for Thursday at 2? Olivia"}, "allow": True}
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": edits})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ledger.STATUS_APPROVED_WITH_EDIT
    assert done[0]["input"]["text"] == "Hi Sam, still on for Thursday at 2? Olivia"
    [learned] = client.get("/take-the-lead").json()["learned"]
    assert learned["example"] == {"text": "Hi Sam, still on for Thursday at 2? Olivia"}
    # The acting passes read it as an example.
    note = ttl.learned_note()
    assert "in training" in note and "Message Sam Teammate" in note and "still on for Thursday" in note


def test_a_bad_edit_is_refused_before_anything_happens(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id, _ = _training_card(owner)
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"input": {"person_id": 1}}})
    assert response.status_code == 422 and done == []
    assert ledger.get_decision_instance(decision_id).status == ledger.STATUS_PROPOSED  # type: ignore[union-attr]


def test_only_training_cards_can_be_allowed(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id = _held(owner)  # held for money
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"allow": True}})
    assert response.status_code == 422 and done == [] and ttl.list_allowed() == []


def test_allowing_needs_a_provable_owner(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id, _ = _training_card(owner)
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"allow": True}})
    assert response.status_code == 403 and done == [] and ttl.list_allowed() == []


def test_suggests_after_unchanged_approvals_and_allows_from_settings(
    client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _carry_out_into([], monkeypatch)
    sam = _teammate()
    _training()
    for n in range(ttl.SUGGEST_AFTER):
        decision_id = ttl.hold("message_person", {"person_id": sam, "text": f"Note {n}"},
                               ttl.Hit(ttl.TRAINING, "it's in training"), source="reflection", mcp=False)
        assert client.post(f"/decisions/{decision_id}/approve", json={}).status_code == 200
    [suggested] = client.get("/take-the-lead").json()["suggested"]
    assert suggested["label"] == "Message Sam Teammate" and suggested["approvals"] == ttl.SUGGEST_AFTER
    assert client.post("/take-the-lead/learned", json={"key": "take_the_lead|message|person:1"}).status_code == 404
    body = client.post("/take-the-lead/learned", json={"key": suggested["key"]}).json()
    assert body["suggested"] == [] and [a["label"] for a in body["learned"]] == ["Message Sam Teammate"]
    assert client.delete(f"/take-the-lead/learned/{body['learned'][0]['id']}").json()["learned"] == []


def test_the_three_positions(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    body = client.put("/take-the-lead", json={"enabled": True, "training": True}).json()
    assert body["enabled"] is True and body["training"] is True
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    # Leaving training lets more go on its own: it needs a provable owner…
    assert client.put("/take-the-lead", json={"training": False}).status_code == 409
    # …going back into training or off doesn't.
    assert client.put("/take-the-lead", json={"enabled": False}).status_code == 200
    assert client.put("/take-the-lead", json={"training": True}).status_code == 200


def test_learned_note_is_empty_while_off() -> None:
    ttl.allow("take_the_lead|workflow|x", "Start the x workflow", example={"text": "y"}, created_by="t")
    assert ttl.learned_note() == ""


def test_an_older_switch_table_gains_training(db: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(f"DROP TABLE IF EXISTS {ttl.LEAD_TABLE}")
    conn.execute(
        f"CREATE TABLE {ttl.LEAD_TABLE} (scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)"
    )
    conn.execute(f"INSERT INTO {ttl.LEAD_TABLE} VALUES ('executive', 1, 'now', 't')")
    conn.commit()
    conn.close()
    lead = ttl.get(ttl.SCOPE_EXECUTIVE)
    assert lead.enabled is True and lead.training is False


def test_a_connected_tool_that_reaches_people_is_never_allowed() -> None:
    # A mail send names no file and reaches whoever it's sent to: no Allow.
    assert ttl.allowance("google_workspace__send_gmail_message",
                         {"to": "sam@co.example", "subject": "Hi", "body": "x"}, mcp=True) is None
    assert ttl.allowance("gdrive__share_file", {"title": "Plan", "email_address": "a@b.example"}, mcp=True) is None
    assert ttl.allowance("gsheets__update_values", {"values": [[1]]}, mcp=True) is None
    key, _ = ttl.allowance("gsheets__update_values", {"title": "Supplier deliveries"}, mcp=True)  # type: ignore[misc]
    assert key == "take_the_lead|mcp|gsheets__update_values|supplier deliveries"


def test_only_the_principal_edits_a_card(client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    decision_id = _held(owner)
    monkeypatch.setattr(decisions_route, "_approver_is_principal", lambda request: False)
    monkeypatch.setattr(decisions_route, "_resolver", lambda instance, request: owner.id)
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"input": {"text": "Pay it elsewhere"}}})
    assert response.status_code == 403 and done == []


def test_a_full_list_refuses_the_allow_before_acting(
    client: TestClient, owner: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[dict[str, Any]] = []
    _carry_out_into(done, monkeypatch)
    monkeypatch.setattr(ttl, "ALLOWED_MAX", 1)
    ttl.allow("take_the_lead|workflow|x", "Start the x workflow", created_by="t")
    decision_id, _ = _training_card(owner)
    response = client.post(f"/decisions/{decision_id}/approve", json={"edits": {"allow": True}})
    assert response.status_code == 409 and done == []
    assert ledger.get_decision_instance(decision_id).status == ledger.STATUS_PROPOSED  # type: ignore[union-attr]


def test_the_learned_note_keeps_wording_only_and_stays_short() -> None:
    _training()
    ttl.allow("take_the_lead|book|people:1", "Book meetings with Priya Nair",
              example={"start": "2026-10-09T15:00", "title": "Weekly 1:1"}, created_by="t")
    ttl.allow("take_the_lead|message|person:2", "Message Sam Lee",
              example={"text": "x" * 3000}, created_by="t")
    ttl.allow("take_the_lead|message|person:3", "Message Ana Diaz", created_by="t")
    stored = {a.label: a.example for a in ttl.list_allowed()}
    assert json.loads(stored["Book meetings with Priya Nair"]) == {"title": "Weekly 1:1"}
    assert len(json.loads(stored["Message Sam Lee"])["text"]) <= ttl.EXAMPLE_MAX
    note = ttl.learned_note()
    assert "in training" in note and "Weekly 1:1" in note and "2026-10-09" not in note
    assert "Message Ana Diaz" not in note  # nothing to show without an example
