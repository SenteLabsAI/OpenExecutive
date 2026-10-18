"""A turn another AI app asks over Open Executive's MCP server that may act
(``Session.mcp_client_may_act``): it runs the speaker's own changes inside
Open Executive, proposes what reaches another person on an approval card
that follows their Handle it for me, is still denied the fixed
``MCP_CLIENT_ACTING_WITHHELD_TOOLS``, and names the app on every audit row."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit import unscoped_audit_rows, via_app
from openexecutive.audit.logger import log_event
from openexecutive.delegation import action_cards, handle_it, training
from openexecutive.delegation.settings import pin_turn_delegation, set_enabled
from openexecutive.mcp_server import server as mcp_server
from openexecutive.memory.decision_ledger import get_decision_instance
from openexecutive.orchestrator import action_card_tools as act
from openexecutive.orchestrator.schedule_tools import (
    MCP_CLIENT_ACTING_REFUSAL,
    MCP_CLIENT_ACTING_WITHHELD_TOOLS,
    MCP_CLIENT_REFUSAL,
    MCP_CLIENT_RUNS_WHEN_ACTING,
    MCP_CLIENT_WITHHELD_TOOLS,
    mcp_client_acting,
    mcp_client_withheld_error,
    mcp_client_withholds,
    mcp_rules_turn,
    read_only_turn,
    set_session,
)
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from .test_action_cards import _invite, _message

# The autouse temp DB and fresh turn pin of Act as me's tests.
from .test_delegation_tools import OWNER, TEAM, db, fresh_turn_state  # noqa: F401
from .test_mcp_client_turns import _gateway, _real_gateway, _results, _run_loop

OUTWARD = (
    "message_person", "send_slack_dm", "send_discord_dm", "send_telegram_message",
    "create_calendar_event", "cancel_calendar_event", "create_instant_meeting",
    "send_company_broadcast", "send_department_message",
)
NEVER = (
    "load_mcp_server", "run_script", "run_python_job", "save_workflow", "run_workflow",
    "suggest_workflow", "run_executive_research", "list_saved_tools", "draft_workflow",
)


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], Any]]:
    """Stand-ins for the real send and booking, recording what ran."""
    from openexecutive.orchestrator import calendar_tools

    ran: list[tuple[str, dict[str, Any], Any]] = []

    async def message(tool_input: dict[str, Any], *, alert_fallback: bool = True) -> str:
        ran.append(("message", tool_input, None))
        return json.dumps({"status": "sent"})

    async def invite(tool_input: dict[str, Any]) -> str:
        ran.append(("invite", tool_input, calendar_tools._approved_by.get()))
        return json.dumps({"status": "created", "event_id": "e1"})

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.message_person", message)
    monkeypatch.setattr(calendar_tools, "handle_create_calendar_event", invite)
    return ran


def _acting(person_id: int | None) -> Session:
    return Session(from_mcp_client=True, mcp_client_may_act=True, caller_person_id=person_id)


# --- the sets ---------------------------------------------------------------


def test_the_acting_set_is_the_read_only_set_minus_the_changes_it_may_make() -> None:
    assert MCP_CLIENT_RUNS_WHEN_ACTING <= MCP_CLIENT_WITHHELD_TOOLS
    assert MCP_CLIENT_ACTING_WITHHELD_TOOLS == MCP_CLIENT_WITHHELD_TOOLS - MCP_CLIENT_RUNS_WHEN_ACTING
    for name in (*OUTWARD, *NEVER):
        assert name in MCP_CLIENT_ACTING_WITHHELD_TOOLS, name
    runs = {
        "ack_alert", "add_watchlist_entry", "assign_open_loop",
        "cancel_followup", "close_open_loop", "create_alert", "create_goal", "forget_fact",
        "leave_out_of_brief", "move_followup", "record_decision_outcome", "remember_fact",
        "remove_watchlist_entry", "schedule_followup",
        "tune_watchlist_entry", "update_company_profile", "update_department_goal",
    }
    assert runs == MCP_CLIENT_RUNS_WHEN_ACTING
    # The roster stays out of reach: someone added on one call would be
    # someone a card reaches on the next.
    for name in ("upsert_person", "archive_person", "set_department_head"):
        assert name in MCP_CLIENT_ACTING_WITHHELD_TOOLS


def test_which_turns_the_rules_cover() -> None:
    read_only = Session(from_mcp_client=True)
    assert read_only_turn(read_only) and mcp_rules_turn(read_only) and not mcp_client_acting(read_only)
    acting = _acting(1)
    assert mcp_client_acting(acting) and mcp_rules_turn(acting) and not read_only_turn(acting)
    # The flag alone, off the MCP server, is nothing.
    stray = Session(mcp_client_may_act=True)
    assert not mcp_client_acting(stray) and not mcp_rules_turn(stray)


def test_withholding_and_refusal_follow_the_mode() -> None:
    assert mcp_client_withholds("remember_fact", {})
    assert not mcp_client_withholds("remember_fact", {}, acting=True)
    assert mcp_client_withholds("message_person", {}, acting=True)
    send = {"name": "google_workspace__send_gmail_message", "arguments": {}}
    assert mcp_client_withholds("call_tool", send, acting=True)
    assert MCP_CLIENT_ACTING_REFUSAL in mcp_client_withheld_error("x", acting=True)
    assert MCP_CLIENT_REFUSAL in mcp_client_withheld_error("x")
    assert "in Open Executive itself" in MCP_CLIENT_ACTING_REFUSAL


def test_only_a_turn_that_may_act_is_a_verified_surface(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator.people_tools import (
        is_principal_on_verified_surface,
        teammate_on_verified_surface,
    )

    assert is_principal_on_verified_surface(_acting(roster.principal))
    assert not is_principal_on_verified_surface(Session(from_mcp_client=True, caller_person_id=roster.principal))
    assert not is_principal_on_verified_surface(_acting(None))
    teammate = teammate_on_verified_surface(_acting(roster.teammate))
    assert teammate is not None and teammate.id == roster.teammate
    # Still never noted as the person's own words.
    from openexecutive.memory.history_chat import verified_speaker

    assert not verified_speaker(_acting(roster.principal), people_store.get_person(roster.principal))


# --- the chat loop ----------------------------------------------------------


def test_an_acting_turn_is_offered_its_own_stable_list(roster: SimpleNamespace) -> None:
    offered = _run_loop(_acting(roster.principal), [], _gateway()).offered[0]
    assert offered == sorted(offered)
    assert not MCP_CLIENT_ACTING_WITHHELD_TOOLS & set(offered)
    assert {"remember_fact", "close_open_loop", "schedule_followup", act.PROPOSE_ACTIONS} <= set(offered)
    assert "ghostwrite_email" not in offered and "recall_history" not in offered
    assert _run_loop(_acting(roster.principal), [], _gateway()).offered[0] == offered
    # A read-only turn is offered no card.
    read_only = _run_loop(Session(from_mcp_client=True, caller_person_id=roster.principal), [], _gateway())
    assert act.PROPOSE_ACTIONS not in read_only.offered[0]


@pytest.mark.parametrize(
    "tool",
    ["message_person", "create_calendar_event", "run_workflow", "save_workflow",
     "upsert_person", "archive_person", "set_department_head"],
)
def test_an_acting_turn_refuses_what_it_is_not_offered(roster: SimpleNamespace, tool: str) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "sent"}))
    tool_use = SimpleNamespace(type="tool_use", id="tu-1", name=tool, input={"text": "hi"})
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {tool: handler}):
        provider = _run_loop(_acting(roster.principal), [tool_use])
    handler.assert_not_awaited()
    assert MCP_CLIENT_ACTING_REFUSAL in json.loads(_results(provider)["tu-1"])["error"]


def test_an_acting_turn_runs_a_change_inside_open_executive(roster: SimpleNamespace) -> None:
    from openexecutive.orchestrator import executive as executive_module

    handler = AsyncMock(return_value=json.dumps({"status": "remembered"}))
    tool_use = SimpleNamespace(type="tool_use", id="tu-1", name="remember_fact", input={"fact": "x"})
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"remember_fact": handler}):
        provider = _run_loop(_acting(roster.principal), [tool_use])
    handler.assert_awaited_once()
    assert json.loads(_results(provider)["tu-1"]) == {"status": "remembered"}


def test_the_gateway_still_refuses_a_send_on_an_acting_turn(roster: SimpleNamespace) -> None:
    gateway, reached = _real_gateway()

    async def _go() -> str:
        with set_session(_acting(roster.principal)):
            return str(await gateway.call_tool({
                "name": "google_workspace__send_gmail_message",
                "arguments": {"to": "olivia@co.example", "subject": "s", "body": "b"},
            }))

    assert MCP_CLIENT_ACTING_REFUSAL in json.loads(asyncio.run(_go()))["error"]
    assert reached == []


# --- outward actions become cards -------------------------------------------


def _settle(*person_ids: int, days: int = 30) -> None:
    """Make these roster entries ``days`` old."""
    import sqlite3
    from datetime import timedelta

    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(people_store.DB_PATH))
    try:
        conn.executemany("UPDATE people SET created_at = ? WHERE id = ?", [(when, p) for p in person_ids])
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def act_as_me(roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    set_enabled(roster.principal, True, updated_by="t")
    monkeypatch.setattr(handle_it, "signing_ok", lambda: True)
    _settle(roster.principal, roster.teammate)
    return roster


def _ai_propose(person_id: int, tool_input: dict[str, Any], app: str | None = "Claude Desktop") -> dict[str, Any]:
    session = _acting(person_id)
    pin_turn_delegation(session, "message Ben")

    async def go() -> str:
        with set_session(session), via_app(app):
            return await act.handle_propose_actions(tool_input)

    return json.loads(asyncio.run(go()))


def test_a_card_waits_when_handle_it_is_off(act_as_me: SimpleNamespace, sends: list[Any]) -> None:
    result = _ai_propose(act_as_me.principal, {"why": "x", "actions": [_message(act_as_me.teammate)]})
    assert result["status"] == "waiting_for_approval" and sends == []
    card = get_decision_instance(result["decision_id"])
    assert card is not None and card.status == "proposed"
    payload = json.loads(card.proposed_payload_json)
    assert payload["via"] == "Claude Desktop" and payload["follow_handle_it"] is True


def test_a_card_goes_on_its_own_under_handle_it(act_as_me: SimpleNamespace, sends: list[Any]) -> None:
    handle_it.set_(act_as_me.principal, enabled=True, mode=handle_it.MODE_BALANCED, updated_by="t")
    result = _ai_propose(act_as_me.principal, {"why": "x", "actions": [
        _message(act_as_me.teammate), _invite(act_as_me.principal, act_as_me.teammate),
    ]})
    assert result["status"] == "done_on_its_own"
    assert [k for k, _, _ in sends] == ["message", "invite"]
    # Never booked as approved by them: nobody tapped.
    assert sends[1][2] is None
    done = get_decision_instance(result["decision_id"])
    assert done is not None and done.status == "executed" and done.gate_mode == "auto_execute"
    rows = audit_logger.get_audit_logger().query(limit=200)
    handled = [e for e in rows if e.event_type == "delegation_actions_handled"]
    assert handled and handled[0].details["via"] == "Claude Desktop"


@pytest.mark.parametrize(("action", "reason"), [
    ("contact", "new_contact"),
    ("sensitive", "sensitive"),
    ("link", "link"),
    ("long", "long"),
])
def test_what_still_waits_under_handle_it(
    act_as_me: SimpleNamespace, sends: list[Any], action: str, reason: str
) -> None:
    roster = act_as_me
    handle_it.set_(roster.principal, enabled=True, mode=handle_it.MODE_CAREFUL, updated_by="t")
    proposed = {
        "contact": {"kind": "add_contact", "full_name": "Sam Lee", "email": "sam@northpeak.example"},
        "sensitive": _message(roster.teammate, "Can you check the contract terms?"),
        "link": _message(roster.teammate, "See https://files.example.com/x"),
        # Careful sends only short replies on its own.
        "long": _message(roster.teammate, "Could you look at the pilot schedule? " * 15),
    }[action]
    person = people_store.get_person(roster.principal)
    checked = action_cards.check([proposed], person, now=datetime.now(UTC))
    assert not isinstance(checked, str)
    assert action_cards.on_its_own_refusal(person, checked, now=datetime.now(UTC), follow_handle_it=True) == reason
    # The same card on an ordinary turn needs training, as before.
    assert action_cards.on_its_own_refusal(person, checked, now=datetime.now(UTC)) == "training_off"
    result = _ai_propose(roster.principal, {"why": "y", "actions": [proposed]})
    assert result["status"] == "waiting_for_approval" and sends == []


def test_in_training_the_training_rule_applies(act_as_me: SimpleNamespace, sends: list[Any]) -> None:
    roster = act_as_me
    handle_it.set_(roster.principal, enabled=True, updated_by="t")
    training.set_(roster.principal, {training.ACTIONS: True}, updated_by="t")
    person = people_store.get_person(roster.principal)
    checked = action_cards.check([_message(roster.teammate)], person, now=datetime.now(UTC))
    assert not isinstance(checked, str)
    assert action_cards.on_its_own_refusal(person, checked, now=datetime.now(UTC), follow_handle_it=True) == "not_allowed"
    assert _ai_propose(roster.principal, {"why": "y", "actions": [_message(roster.teammate)]})["status"] == "waiting_for_approval"


def test_the_last_check_uses_the_cards_own_rule(act_as_me: SimpleNamespace, sends: list[Any]) -> None:
    roster = act_as_me
    handle_it.set_(roster.principal, enabled=True, updated_by="t")
    person = people_store.get_person(roster.principal)
    checked = action_cards.check([_message(roster.teammate)], person, now=datetime.now(UTC))
    assert not isinstance(checked, str)
    decision_id = action_cards.propose(person, checked, "y", session_id=None, on_its_own=True, follow_handle_it=True)
    # Turned off in between: held, waiting on a tap.
    handle_it.set_(roster.principal, enabled=False, updated_by="t")
    assert asyncio.run(action_cards.run_on_its_own(decision_id, person)) is None
    card = get_decision_instance(decision_id)
    assert card is not None and card.status == "proposed" and sends == []


def test_no_card_without_act_as_me_or_a_speaker(roster: SimpleNamespace, sends: list[Any]) -> None:
    assert "Act as me" in _ai_propose(roster.principal, {"why": "x", "actions": [_message(roster.teammate)]})["error"]
    session = _acting(None)
    pin_turn_delegation(session, "x")

    async def go() -> str:
        with set_session(session):
            return await act.handle_propose_actions({"why": "x", "actions": [_message(roster.teammate)]})

    assert "can't tell who" in json.loads(asyncio.run(go()))["error"]
    assert sends == []


# --- the record ---------------------------------------------------------------


def test_every_row_inside_via_app_names_the_app() -> None:
    with via_app("Cursor"):
        log_event("tool_invocation", "skill:remember_fact")
        with via_app("Other"):
            log_event("tool_invocation", "inner")
        with unscoped_audit_rows():
            log_event("tool_invocation", "unattended")
    log_event("tool_invocation", "outside")
    rows = {e.summary: e.details for e in audit_logger.get_audit_logger().query(limit=50)}
    assert rows["skill:remember_fact"]["via"] == "Cursor"
    assert rows["inner"]["via"] == "Cursor"
    assert "via" not in (rows["unattended"] or {})
    assert "via" not in (rows["outside"] or {})


def test_the_client_name_falls_back_outside_a_request() -> None:
    assert mcp_server.client_name() == "an AI app"


# --- turning it on ------------------------------------------------------------


def _request(headers: dict[str, str], secret_ok: bool | None) -> Any:
    state = SimpleNamespace() if secret_ok is None else SimpleNamespace(shared_secret_ok=secret_ok)
    return SimpleNamespace(headers=headers, state=state)


@pytest.mark.parametrize(("headers", "secret_ok", "expected"), [
    ({"x-mcp-client-actions": "on"}, True, True),
    ({"x-mcp-client-actions": " ON "}, True, True),
    ({"x-mcp-client-actions": "on"}, None, False),
    ({"x-mcp-client-actions": "on"}, False, False),
    ({"x-mcp-client-actions": "yes"}, True, False),
    ({}, True, False),
])
def test_with_a_secret_only_the_header_on_an_authenticated_request(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], secret_ok: bool | None, expected: bool
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("MCP_CLIENT_ACTIONS", "true")  # ignored with a secret
    monkeypatch.setattr(mcp_server, "_request", lambda: _request(headers, secret_ok))
    assert mcp_server.may_act() is expected


def test_with_a_secret_and_no_request_it_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s3cret")
    assert mcp_server.may_act() is False


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False), (None, False)])
def test_without_a_secret_the_setting_decides_and_the_header_is_ignored(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, expected: bool
) -> None:
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    if setting is None:
        monkeypatch.delenv("MCP_CLIENT_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("MCP_CLIENT_ACTIONS", setting)
    monkeypatch.setattr(mcp_server, "_request", lambda: _request({"x-mcp-client-actions": "on"}, True))
    assert mcp_server.may_act() is expected


def test_ask_executive_plumbs_may_act_and_the_app(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.audit import rows_via

    monkeypatch.setattr(mcp_server, "may_act", lambda: True)
    seen: dict[str, Any] = {}

    async def chat(self: Any, **kwargs: Any) -> str:
        seen.update(kwargs, via=rows_via())
        return "done"

    profile = SimpleNamespace(is_empty=lambda: True)
    with (
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile", return_value=profile),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
        patch("openexecutive.orchestrator.executive.Executive.chat", new=chat),
        patch("openexecutive.briefing.context.render_and_trust", return_value="<board/>"),
        patch.object(mcp_server, "_who", return_value=mcp_server.api_caller.Caller("open")),
    ):
        assert asyncio.run(mcp_server.ask_executive("ack the churn alert")) == "done"
    assert seen["session"].from_mcp_client is True and seen["session"].mcp_client_may_act is True
    assert seen["session"].caller_person_id == roster.principal
    assert seen["briefing_context"] == "<board/>"
    assert seen["via"] == "an AI app"


def test_a_turn_that_read_outside_text_waits(act_as_me: SimpleNamespace, sends: list[Any]) -> None:
    from openexecutive.orchestrator import outside_content

    handle_it.set_(act_as_me.principal, enabled=True, updated_by="t")
    with patch.object(outside_content, "read_outside", return_value=True):
        result = _ai_propose(act_as_me.principal, {"why": "x", "actions": [_message(act_as_me.teammate)]})
    assert result["status"] == "waiting_for_approval" and sends == []


@pytest.mark.parametrize(("days", "expected"), [(1, "new_person"), (6, "new_person"), (8, None)])
def test_no_one_added_lately_is_reached_on_its_own(
    act_as_me: SimpleNamespace, sends: list[Any], days: int, expected: str | None
) -> None:
    roster = act_as_me
    handle_it.set_(roster.principal, enabled=True, updated_by="t")
    newcomer = people_store.upsert_person(full_name="Nia New", email="nia@co.example")
    _settle(newcomer, days=days)
    person = people_store.get_person(roster.principal)
    for proposed in (_message(newcomer), _invite(roster.principal, newcomer)):
        checked = action_cards.check([proposed], person, now=datetime.now(UTC))
        assert not isinstance(checked, str)
        assert action_cards.on_its_own_refusal(
            person, checked, now=datetime.now(UTC), follow_handle_it=True,
        ) == expected


def test_planting_someone_then_messaging_them_never_sends_on_its_own(
    act_as_me: SimpleNamespace, sends: list[Any]
) -> None:
    """Call 1 can't add anyone; whoever was added another way this week
    still waits on call 2's card."""
    from openexecutive.orchestrator import executive as executive_module

    roster = act_as_me
    handle_it.set_(roster.principal, enabled=True, updated_by="t")
    upsert = AsyncMock(return_value=json.dumps({"status": "saved"}))
    tool_use = SimpleNamespace(
        type="tool_use", id="tu-1", name="upsert_person",
        input={"full_name": "Eve Planted", "email": "eve@attacker.example"},
    )
    with patch.dict(executive_module._ALL_SKILL_HANDLERS, {"upsert_person": upsert}):
        provider = _run_loop(_acting(roster.principal), [tool_use])
    upsert.assert_not_awaited()
    assert MCP_CLIENT_ACTING_REFUSAL in json.loads(_results(provider)["tu-1"])["error"]
    assert people_store.find_person_by_email("eve@attacker.example", include_contacts=True) is None
    planted = people_store.upsert_person(full_name="Eve Planted", email="eve@attacker.example")
    result = _ai_propose(roster.principal, {"why": "x", "actions": [_message(planted)]})
    assert result["status"] == "waiting_for_approval" and sends == []
