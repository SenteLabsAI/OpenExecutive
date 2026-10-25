"""A change to a saved workflow from chat that waits for its person's tap
(orchestrator/workflow_change.py): the card, the tap in the web app or in
Telegram, and the builder's note on whose calendar a step reads."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from openexecutive.api.routes import decisions as decisions_route
from openexecutive.api.routes import workflows as workflows_route
from openexecutive.delegation.verified import ChatTap
from openexecutive.memory import decision_ledger, episodic, session_store
from openexecutive.orchestrator import app_control, workflow_change
from openexecutive.orchestrator import workflow_authoring_tools as wat
from openexecutive.orchestrator import workflow_run_tools as wrt
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import approved_targets, dynamic_cadence, dynamic_store
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

GATEWAY_READ = "google_workspace__get_events"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(session_store, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(dynamic_store, "DB_PATH", path)
    monkeypatch.setattr(approved_targets, "DB_PATH", path)
    monkeypatch.setattr(decision_ledger, "_db_path", lambda: path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dynamic_store.initialize_dynamic_workflows_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *_a, **_k: None)
    monkeypatch.setattr(dynamic_cadence, "cancel_cadence_rows", lambda _name: 0)
    monkeypatch.setattr(dynamic_cadence, "schedule_dynamic_workflow_cadence", lambda _defn: 1)

    async def no_errors(_defn: Any) -> list[str]:
        return []

    # The gateway isn't running here; its tool names are checked elsewhere.
    monkeypatch.setattr(wat, "_build_def", _build_without_gateway)
    monkeypatch.setattr(workflows_route, "validate_definition_and_tools", no_errors)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


async def _build_without_gateway(definition: Any) -> tuple[Any, str | None]:
    return DynamicWorkflowDef.model_validate(definition), None


@pytest.fixture
def owner() -> Any:
    pid = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_registry.invalidate()
    return people_store.get_person(pid)


def _prep(goal: str = "List John's calendar events for tomorrow.", tools: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": "meeting_prep",
        "title": "Meeting prep",
        "steps": [
            {"kind": "action", "id": "find", "title": "Find meetings", "goal": goal,
             "tools": tools or [GATEWAY_READ]},
            {"kind": "synthesis", "id": "wrap", "title": "Wrap up"},
        ],
    }


def _stored(owner: int | None = None) -> DynamicWorkflowDef:
    defn = DynamicWorkflowDef.model_validate(_prep())
    saved = dynamic_store.save_if_unchanged(defn, None, owner_person_id=owner)
    assert saved is not None
    return saved


def _web(person: Any) -> Session:
    return Session(session_id="s-1", from_web_chat=True, caller_person_id=person.id, web_caller_signed_in=True)


def _call(handler: Any, args: dict[str, Any], session: Session | None) -> dict[str, Any]:
    async def go() -> str:
        if session is None:
            return str(await handler(args))
        with set_session(session):
            return str(await handler(args))

    return dict(json.loads(asyncio.run(go())))


def _fix() -> dict[str, Any]:
    return _prep(goal="List events on olivia@co.example's calendar for tomorrow.")


def _save_fix(session: Session | None) -> dict[str, Any]:
    d = _fix()
    return _call(wat.handle_save_workflow, {"definition": d, "confirm_token": wat._canonical_token(d),
                                            "overwrite": True}, session)


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def test_the_card_says_what_changes_in_each_step() -> None:
    old = DynamicWorkflowDef.model_validate(_prep())
    new = DynamicWorkflowDef.model_validate(_prep(tools=["oe__my_calendar"]))
    text = workflow_change.card_text(old, new)
    assert text.startswith("Change the workflow “Meeting prep”")
    assert "Step “Find meetings”: now uses My calendar; no longer uses Google Workspace: get events" in text
    assert "wrap" not in text.lower()


def test_new_removed_and_rescheduled_steps_are_named() -> None:
    old = DynamicWorkflowDef.model_validate(_prep())
    added = {"kind": "action", "id": "mail", "title": "Send it", "goal": "Email it.", "tools": ["oe__send_email"]}
    new = DynamicWorkflowDef.model_validate({**_prep(), "steps": [added, _prep()["steps"][1]]})
    lines = workflow_change.change_lines(old, new)
    assert any(line.startswith("New step “Send it”") and "Tools: Send email" in line for line in lines)
    assert "Removed step “Find meetings”" in lines


def test_a_switched_on_tool_workflow_changes_only_on_a_card() -> None:
    approved = DynamicWorkflowDef.model_validate(_prep())
    pending = approved.model_copy(update={"is_active": False})
    assert workflow_change.needs_card(approved, approved)
    assert not workflow_change.needs_card(pending, pending)
    assert not workflow_change.needs_card(None, approved)


# --------------------------------------------------------------------------- #
# Staging it from chat
# --------------------------------------------------------------------------- #


def test_save_leaves_a_card_and_changes_nothing_yet(owner: Any) -> None:
    _stored()
    out = _save_fix(_web(owner))
    assert out["status"] == "waiting_for_approval"
    assert "olivia@co.example" in out["card"]
    stored = dynamic_store.get_definition("meeting_prep")
    assert stored is not None and "John's calendar" in stored.steps[0].goal  # type: ignore[union-attr]
    card = decision_ledger.get_decision_instance(out["decision_id"])
    assert card is not None and card.decision_class == "app_action" and card.approver_person_id == owner.id
    chip = summarize_action(tool_name="save_workflow", tool_input={}, tool_result=json.dumps(out))
    assert chip is not None and chip["decision_id"] == out["decision_id"]


def test_no_card_without_a_verified_speaker_or_where_it_cant_show(owner: Any) -> None:
    _stored()
    assert "know who is asking" in _save_fix(None)["error"]
    slack = Session(session_id="slack-dm-U1", origin_channel="slack", origin_channel_ref="U1",
                    caller_person_id=owner.id)
    assert "error" in _save_fix(slack)


def test_only_the_owner_or_its_creator_may_stage_a_change(owner: Any) -> None:
    ben_id = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    ben = people_store.get_person(ben_id)
    _stored()
    with set_session(_web(ben)):
        refused = workflow_change.may_read("meeting_prep")
    assert refused is not None and "account owner" in refused


# --------------------------------------------------------------------------- #
# The tap
# --------------------------------------------------------------------------- #


def _staged(owner: Any) -> int:
    _stored()
    return int(_save_fix(_web(owner))["decision_id"])


def test_approving_on_the_web_saves_it_and_keeps_it_on(owner: Any) -> None:
    decision_id = _staged(owner)
    instance = decision_ledger.get_decision_instance(decision_id)
    request = SimpleNamespace(state=SimpleNamespace(), headers={"x-caller-email": "olivia@co.example"})
    asyncio.run(decisions_route._carry_out_app_action(instance, decisions_route.ApproveBody(), request, owner.id))  # type: ignore[arg-type]
    stored = dynamic_store.get_definition("meeting_prep")
    assert stored is not None and stored.is_active is True
    assert "olivia@co.example" in stored.steps[0].goal  # type: ignore[union-attr]
    done = decision_ledger.get_decision_instance(decision_id)
    assert done is not None and done.status == decision_ledger.STATUS_APPROVED_UNCHANGED


def test_a_telegram_tap_saves_it(owner: Any) -> None:
    decision_id = _staged(owner)
    instance = decision_ledger.get_decision_instance(decision_id)
    assert instance is not None and decisions_route.shown_in_chat(instance)
    asyncio.run(decisions_route.resolve_in_chat(decision_id, owner, approve=True))
    stored = dynamic_store.get_definition("meeting_prep")
    assert stored is not None and "olivia@co.example" in stored.steps[0].goal  # type: ignore[union-attr]


def test_a_workflow_changed_since_the_card_is_left_alone(owner: Any) -> None:
    decision_id = _staged(owner)
    current = dynamic_store.get_definition("meeting_prep")
    assert current is not None
    dynamic_store.upsert_definition(current.model_copy(update={"title": "Renamed on the page"}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(decisions_route.resolve_in_chat(decision_id, owner, approve=True))
    assert exc.value.status_code == 409
    stored = dynamic_store.get_definition("meeting_prep")
    assert stored is not None and "John's calendar" in stored.steps[0].goal  # type: ignore[union-attr]


def test_a_change_that_reads_as_its_creator_needs_that_creator(owner: Any) -> None:
    ben_id = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    _stored(owner=ben_id)
    mine = _prep(tools=["oe__my_calendar"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(workflows_route.apply_chat_change(
            "meeting_prep", mine, dynamic_store.get_definition("meeting_prep").model_dump(),  # type: ignore[union-attr]
            ChatTap("olivia@co.example"),
        ))
    assert exc.value.status_code == 403


def test_other_app_cards_still_stay_out_of_chat_apps(owner: Any) -> None:
    decision_id = decision_ledger.create_decision_instance(
        decision_class="app_action", department="", originating_session_id=None,
        proposed_payload={"action": "delete_chat", "id": "s-1", "value": "", "title": "x", "card": "Delete"},
        idempotency_key=None, gate_mode="propose", approver_person_id=owner.id, confidence=None,
    )
    instance = decision_ledger.get_decision_instance(decision_id)
    assert instance is not None and not decisions_route.shown_in_chat(instance)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(decisions_route.resolve_in_chat(decision_id, owner, approve=True))
    assert exc.value.status_code == 404


def test_do_in_app_cant_reach_the_hidden_action(owner: Any) -> None:
    assert workflow_change.ACTION not in app_control.DO_IN_APP_TOOL["input_schema"]["properties"]["action"]["enum"]
    out = _call(app_control.handle_do_in_app, {"action": workflow_change.ACTION, "id": "meeting_prep",
                                               "value": "{}"}, _web(owner))
    assert "Unknown action" in out["error"]


# --------------------------------------------------------------------------- #
# Reading one, and the builder's note on whose calendar
# --------------------------------------------------------------------------- #


def test_list_workflows_reads_one_for_its_owner(owner: Any) -> None:
    _stored()
    out = _call(wrt.handle_list_workflows, {"name": "meeting_prep"}, _web(owner))
    assert out["definition"]["steps"][0]["tools"] == [GATEWAY_READ]
    assert "know who is asking" in _call(wrt.handle_list_workflows, {"name": "meeting_prep"}, None)["error"]


def test_the_builder_is_told_whose_calendar_gateway_tools_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import designer

    monkeypatch.setattr(designer, "_exec_email", lambda: "exec@co.example")
    mine = designer._accounts_line(True, "exec@co.example", "olivia@co.example")
    assert "Executive's own (exec@co.example)" in mine and "oe__my_calendar" in mine
    assert "olivia@co.example" in mine
    theirs = designer._accounts_line(False, "exec@co.example", "olivia@co.example")
    assert "oe__my_calendar" not in theirs and "write their address" in theirs
    assert "YOUR account" in json.dumps(wat.DEFINITION_SCHEMA)
