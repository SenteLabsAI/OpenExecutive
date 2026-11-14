"""The one check (``orchestrator.levels``): Careful, Balanced and Hands-off
decide run or card for every noticed step, on every channel."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.memory import episodic
from openexecutive.orchestrator import levels, outside_mode
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = "olivia@co.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "levels.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    yield path
    people_registry.invalidate()


@pytest.mark.parametrize("reason", sorted(levels.REASONS - {levels.STRANGER, levels.NOT_OWNER, levels.SHARED, levels.AI_APP}))
def test_hands_off_runs_every_noticed_step(reason: str) -> None:
    assert levels.decide(reason, mode=outside_mode.TRUST) == levels.RUN


@pytest.mark.parametrize("mode", [outside_mode.ASK, outside_mode.OUTSIDERS])
@pytest.mark.parametrize("reason", sorted(levels.REASONS))
def test_below_hands_off_every_noticed_step_is_a_card(mode: str, reason: str) -> None:
    assert levels.decide(reason, mode=mode) == levels.CARD


@pytest.mark.parametrize("mode", sorted(outside_mode.MODES))
def test_a_strangers_mail_is_a_card_at_every_level(mode: str) -> None:
    assert levels.decide(levels.STRANGER, mode=mode) == levels.CARD


@pytest.mark.parametrize("reason", [levels.NOT_OWNER, levels.AI_APP])
@pytest.mark.parametrize("mode", sorted(outside_mode.MODES))
def test_someone_elses_words_are_a_card_at_every_level(mode: str, reason: str) -> None:
    assert levels.decide(reason, mode=mode) == levels.CARD


@pytest.mark.parametrize("session", [
    Session(from_mcp_client=True),
    Session(from_web_chat=True, from_host=True),
])
def test_hands_off_is_not_for_anyone_outside_people(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert outside_mode.for_turn(session) == outside_mode.OUTSIDERS


def test_the_level_belongs_to_whoever_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A teammate's turn runs at their level like the owner's, and a shared
    chat someone else wrote in follows that person's level too."""
    owner = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    mate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert outside_mode.for_turn(Session(from_web_chat=True, caller_person_id=mate)) == outside_mode.TRUST
    assert outside_mode.for_turn(Session(session_id="email:t", caller_person_id=owner)) == outside_mode.TRUST
    assert outside_mode.for_turn(Session(shared_chat=True, others_spoke_last=True)) == outside_mode.TRUST


def test_a_shared_chat_follows_the_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert levels.decide(levels.SHARED, mode=outside_mode.TRUST) == levels.RUN


def test_an_error_deciding_is_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("x")

    monkeypatch.setattr(levels, "turn_level", boom)
    assert levels.decide(levels.OUTSIDE) == levels.CARD


@pytest.mark.parametrize("session", [
    Session(from_web_chat=True),
    Session(session_id="email:t1"),
    Session(origin_channel="slack"),
    Session(origin_channel="telegram"),
    Session(shared_chat=True),
])
def test_hands_off_is_hands_off_on_every_channel(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert outside_mode.for_turn(session) == outside_mode.TRUST


def test_mail_from_someone_not_in_people_never_runs_hands_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    assert outside_mode.for_turn(Session(session_id="email:t1", sender_off_team=True)) == outside_mode.OUTSIDERS


def test_approved_relaxes_and_resets() -> None:
    assert not levels.relaxed() or levels.turn_level() == outside_mode.TRUST
    with levels.approved():
        assert levels.relaxed()


def test_only_to_owner_reads_every_recipient() -> None:
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()

    def mail(**args: Any) -> dict[str, Any]:
        return {"name": "google_workspace__send_gmail_message", "arguments": args}

    assert levels.only_to_owner("call_tool", mail(to=OWNER, subject="s", body="b"))
    assert levels.only_to_owner("call_tool", mail(to=OWNER.upper()))
    assert not levels.only_to_owner("call_tool", mail(to=OWNER, cc="ben@co.example"))
    assert not levels.only_to_owner("call_tool", mail(to="ben@co.example"))
    assert not levels.only_to_owner("call_tool", mail())
    assert not levels.only_to_owner(
        "call_tool", {"name": "google_workspace__search_drive_files", "arguments": {"to": OWNER}}
    )
    assert not levels.only_to_owner("call_tool", "not a dict")


def test_a_step_that_cant_be_carried_out_is_told_not_dropped() -> None:
    out = json.loads(levels.not_carriable_error("run_script", levels.PRIVATE))
    assert "run_script" in out["error"] and "can't wait on a card" in out["error"]
    assert levels.carriable("google_workspace__send_gmail_message", mcp=True)
    assert not levels.carriable("not_a_tool", mcp=False)


def test_hold_returns_the_card_and_the_chip_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_gate

    seen: dict[str, Any] = {}

    def fake_hold(requester: Any, tool: str, tool_input: dict[str, Any], verdict: Any, **kw: Any) -> int:
        seen.update(tool=tool, input=tool_input, held=kw.get("held"), why=verdict.reason)
        return 42

    monkeypatch.setattr(department_gate, "hold", fake_hold)
    with set_session(Session(from_web_chat=True)):
        result = levels.hold("message_person", {"text": "hi"}, mcp=False, reason=levels.SHARED)
    parsed = json.loads(result)
    assert parsed == {**parsed, "status": "waiting_for_approval", "decision_instance_id": 42}
    assert seen == {"tool": "message_person", "input": {"text": "hi"}, "held": True,
                    "why": levels._WHY[levels.SHARED]}
    chip = levels.waiting_chip(result, label="message_person", iteration=1)
    assert chip is not None and chip["decision_id"] == 42 and chip["link"] == "/today"
    assert levels.waiting_chip(json.dumps({"error": "x"}), label="x") is None


def test_hold_that_fails_tells_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_gate

    def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("db")

    monkeypatch.setattr(department_gate, "hold", boom)
    out = json.loads(levels.hold("message_person", {}, mcp=False, reason=levels.OUTSIDE,
                                 session=SimpleNamespace(session_id="s")))
    assert "Approve card" in out["error"]


def test_card_or_with_no_turn_keeps_the_checks_own_error() -> None:
    assert levels.card_or("blocked", "google_workspace__send_gmail_message", {}, reason=levels.NEW_PERSON) == "blocked"


def test_a_reply_or_other_mail_tool_is_never_owner_only() -> None:
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    for name, args in (
        ("microsoft_365__send-mail", {"to": OWNER, "body": {"Message": {"toRecipients": []}}}),
        ("microsoft_365__reply-mail-message", {"to": OWNER, "messageId": "m"}),
        ("google_workspace__send_gmail_message", {"to": OWNER, "thread_id": "t"}),
    ):
        assert not levels.only_to_owner("call_tool", {"name": name, "arguments": args}), name


def test_card_or_while_a_card_is_carried_out_keeps_the_error() -> None:
    with set_session(Session(unattended=True)):
        assert levels.card_or("blocked", "x", {}, reason=levels.NEW_PERSON) == "blocked"


def test_a_skill_that_runs_more_steps_is_not_relaxed_by_its_tap() -> None:
    assert {"run_workflow", "run_executive_research"} <= levels.NOT_RELAXED_SKILLS


def test_the_owners_own_ai_app_follows_their_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another AI app the owner is signed in to and allowed to act is them:
    Hands-off runs what it asks, like the web app. Any other stays a card."""
    from openexecutive.orchestrator import people_tools
    from openexecutive.orchestrator.schedule_tools import current_session

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.TRUST)
    monkeypatch.setattr(people_tools, "is_principal_on_verified_surface", lambda _s: True)
    own = Session(from_mcp_client=True, mcp_client_may_act=True, caller_person_id=1)
    assert outside_mode.for_turn(own) == outside_mode.TRUST
    token = current_session.set(own)
    try:
        assert levels.decide(levels.AI_APP, mode=outside_mode.TRUST) == levels.RUN
        assert levels.decide(levels.AI_APP, mode=outside_mode.OUTSIDERS) == levels.CARD
    finally:
        current_session.reset(token)
    read_only = Session(from_mcp_client=True, caller_person_id=1)
    assert outside_mode.for_turn(read_only) == outside_mode.OUTSIDERS
    monkeypatch.setattr(people_tools, "is_principal_on_verified_surface", lambda _s: False)
    assert outside_mode.for_turn(own) == outside_mode.OUTSIDERS


def test_hold_many_is_one_card_for_them_all(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_gate

    seen: dict[str, Any] = {}

    def fake_hold(requester: Any, tool: str, tool_input: dict[str, Any], verdict: Any, **kw: Any) -> int:
        seen.update(input=tool_input, more=kw.get("more"))
        return 7

    monkeypatch.setattr(department_gate, "hold", fake_hold)
    steps = [{"file_id": f"f{n}"} for n in range(3)]
    with set_session(Session(from_web_chat=True)):
        result = json.loads(levels.hold_many("google_workspace__update_drive_file", steps, mcp=True,
                                             reason=levels.OWN_MAIL))
    assert result["decision_instance_id"] == 7 and "one tap does them all" in result["message"]
    assert seen == {"input": steps[0], "more": steps[1:]}


def test_the_card_says_when_a_mail_read_was_flagged() -> None:
    from openexecutive.orchestrator import outside_content

    state = outside_content.begin_turn(Session(), user_message="redo the draft")
    try:
        assert levels.mail_reason() == levels.OWN_MAIL
        state.flags.append((outside_content.MAIL, ("forward_or_share",), ""))
        assert levels.mail_reason() == levels.MAIL_FLAGGED
        assert "instructions" in levels._WHY[levels.MAIL_FLAGGED]
    finally:
        outside_content.end_turn(state)


def test_a_held_browser_step_never_keeps_the_page_the_model_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import department_gate, extensions

    seen: list[dict[str, Any]] = []

    def fake_hold(requester: Any, tool: str, tool_input: dict[str, Any], verdict: Any, **kw: Any) -> int:
        seen.append(tool_input)
        return 7

    monkeypatch.setattr(department_gate, "hold", fake_hold)
    monkeypatch.setattr(extensions, "card_handler", lambda tool: (lambda _i: None) if tool == "browser_click" else None)
    step = {"ref": 4, "_at": "https://bank.example/", "_element": 'button "Pay"', "_own": True}
    with set_session(Session(from_web_chat=True)):
        # Held by Approvals before the tool ran: those keys are the model's.
        levels.hold("browser_click", dict(step), mcp=False, reason=levels.OUTSIDE)
        # Held by the tool itself on an email or chat site: they are its own.
        levels.hold("browser_click", dict(step), mcp=False, reason=levels.SEND_SITE)
        # Another tool's keys are left as they are.
        levels.hold("message_person", {"_x": 1}, mcp=False, reason=levels.OUTSIDE)
    assert seen == [{"ref": 4}, step, {"_x": 1}]
