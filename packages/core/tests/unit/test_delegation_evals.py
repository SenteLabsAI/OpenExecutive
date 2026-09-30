"""Act as me in the eval runner: a ``delegation`` block runs the turn against
an in-memory mailbox, and the drafts it saves reach the judge."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.delegation.gmail import DraftSpec
from openexecutive.delegation.settings import DelegationOverride
from openexecutive.evals import judges as judges_module
from openexecutive.evals import runner as runner_module
from openexecutive.evals.scenarios import (
    load_scenarios,
    scenario_delegation,
    validate_scenario_yaml,
)

_BLOCK = """
delegation:
  person: {full_name: Olivia Owner, email: Olivia@Fernway.example}
  thread:
    id: t-pilot
    subject: Brand refresh pilot
    messages:
      - {from: "Dana Prospect <dana@northpeak.example>", text: "Can we start Oct 5?", reply_to: x@y.example}
      - {from: "olivia@fernway.example", text: "Checking my calendar."}
"""


def test_a_block_becomes_a_fresh_mailbox_each_time() -> None:
    scenario = validate_scenario_yaml("id: s1\nquery: q\n" + _BLOCK)
    first, second = scenario_delegation(scenario), scenario_delegation(scenario)
    assert isinstance(first, DelegationOverride) and first.enabled
    assert first.gmail is not second.gmail
    assert first.person.email == "olivia@fernway.example" and first.person.is_principal
    thread = first.gmail.threads["t-pilot"]
    assert [m.labels for m in thread.messages] == [["INBOX"], ["SENT"]]
    assert thread.messages[0].reply_to == "x@y.example"
    assert scenario_delegation({"id": "s2"}) is None


@pytest.mark.parametrize("block", [
    "delegation: yes\n",
    "delegation:\n  person: {full_name: X}\n",
    "delegation:\n  person: {full_name: X, email: x@y.example}\n  thread: {id: '../x', messages: []}\n",
    "delegation:\n  person: {full_name: X, email: x@y.example}\n  thread: {id: t1, messages: [{text: hi}]}\n",
])
def test_a_malformed_block_fails_the_scenario(block: str) -> None:
    with pytest.raises(ValueError, match="delegation"):
        validate_scenario_yaml("id: s1\nquery: q\n" + block)


def test_the_judge_sees_the_drafts_only_for_these_scenarios() -> None:
    section = judges_module._delegation_section(
        {"delegation": {}, "quality_criteria": {"never_claims_the_email_was_sent": True}},
        [{"to": ["dana@northpeak.example"], "cc": [], "subject": "Re: Pilot", "body": "Yes to Oct 5."}],
    )
    assert "ACT AS ME" in section and "Yes to Oct 5." in section and "To: dana@northpeak.example" in section
    assert "never claims the email was sent" in section
    assert "(none)" in judges_module._delegation_section({"delegation": {}}, [])
    assert judges_module._delegation_section({"id": "x"}, None) == ""


def test_the_runner_runs_the_turn_with_the_mailbox_and_judges_its_drafts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "d.yaml").write_text("id: d\ndomain: delegation\ndescription: d\nquery: reply as me\n" + _BLOCK)
    monkeypatch.setenv("EVAL_SCENARIOS_PATH", str(tmp_path))
    judged: dict[str, Any] = {}

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        override = kwargs["session"].delegation_override
        assert isinstance(override, DelegationOverride)
        await override.gmail.create_draft(DraftSpec(to=["dana@northpeak.example"], subject="Re: x", body="Yes."))
        return "Your draft is waiting in your Gmail Drafts."

    async def fake_judge(scenario: dict[str, Any], response: str, drafts: Any = None) -> dict[str, Any]:
        judged["drafts"] = drafts
        return {"overall": 5}

    from openexecutive.orchestrator.executive import Executive

    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)

    async def drive() -> None:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        run_one = runner_module._make_run_one(
            kind="chat", store=None, sem=asyncio.Semaphore(1), queue=queue,
            passed=[0], total=1, cancel_event=None,
        )
        scenario = next(s for s in load_scenarios(kind="chat") if s["id"] == "d")
        await run_one(0, scenario)

    asyncio.run(drive())
    assert judged["drafts"][0]["body"] == "Yes." and judged["drafts"][0]["to"] == ["dana@northpeak.example"]


def test_the_shipped_scenarios_load() -> None:
    ids = {s["id"] for s in load_scenarios(kind="chat")}
    assert {"delegation_001", "delegation_002", "delegation_003"} <= ids


# ── The inbox watcher's scenarios (type: inbox) ─────────────────────────────

_INBOX = """
id: i1
type: inbox
description: d
inbox:
  person: {full_name: Olivia Owner, email: Olivia@Fernway.example}
  relation: contact
  expect: draft
  thread:
    id: t-kickoff
    subject: Kickoff
    messages:
      - {from: "Dana Park <dana@northpeak.example>", text: "Next week?", cc: [ops@northpeak.example]}
      - {from: "olivia@fernway.example", text: "Tuesday works."}
      - {from: "Dana Park <dana@northpeak.example>", text: "2pm or 4pm?", verified: false}
"""


def test_the_inbox_scenarios_are_their_own_kind() -> None:
    from openexecutive.evals.scenarios import scenario_inbox, scenario_kind

    scenario = validate_scenario_yaml(_INBOX)
    assert scenario_kind(scenario) == "inbox"
    case = scenario_inbox(scenario)
    # The message answered is the newest one from someone else.
    assert case.message.text == "2pm or 4pm?" and case.message.sender_authenticated is False
    assert case.thread.messages[0].sender_authenticated is True
    assert case.thread.messages[0].cc == ["ops@northpeak.example"]
    assert [m.labels for m in case.thread.messages] == [["INBOX"], ["SENT"], ["INBOX"]]
    assert case.person.email == "olivia@fernway.example" and case.person.is_principal
    assert case.relation == "contact" and case.expect_draft is True
    shipped = {s["id"] for s in load_scenarios(kind="inbox")}
    assert {f"delegation_inbox_00{i}" for i in range(1, 8)} <= shipped
    assert not shipped & {s["id"] for s in load_scenarios(kind="chat")}


@pytest.mark.parametrize(("change", "error"), [
    ("  relation: contact\n", "  relation: boss\n"),
    ("  expect: draft\n", "  expect: maybe\n"),
    ("  person: {full_name: Olivia Owner, email: Olivia@Fernway.example}\n", "  person: {full_name: X}\n"),
])
def test_a_malformed_inbox_block_fails_the_scenario(change: str, error: str) -> None:
    with pytest.raises(ValueError, match="inbox"):
        validate_scenario_yaml(_INBOX.replace(change, error))


def test_an_inbox_scenario_needs_someone_elses_message() -> None:
    only_hers = _INBOX.split("    messages:\n")[0] + (
        "    messages:\n      - {from: \"olivia@fernway.example\", text: \"Tuesday works.\"}\n"
    )
    with pytest.raises(ValueError, match="someone else"):
        validate_scenario_yaml(only_hers)
    with pytest.raises(ValueError, match="inbox"):
        validate_scenario_yaml("id: i2\ntype: inbox\n")


def _reply(**kw: Any) -> Any:
    from openexecutive.delegation.inbox import Reply

    base: dict[str, Any] = {
        "to": ["dana@northpeak.example"], "subject": "Re: Kickoff", "body": "Tuesday still works.",
        "open_questions": ["2pm or 4pm?"], "flags": [], "in_reply_to": None, "references": None,
    }
    return Reply(**{**base, **kw})


@pytest.mark.parametrize(("expect", "wants", "reply", "judged", "overall", "passed"), [
    ("draft", True, "reply", True, 4, True),
    ("draft", False, None, False, 1, False),
    ("no_draft", True, "reply", False, 1, False),
    ("no_draft", False, None, False, 5, True),
    # A draft was wanted but composing failed: that's a failure either way.
    ("draft", True, "compose_failed", False, 1, False),
    ("no_draft", True, "compose_failed", False, 1, False),
    # No verdict is a failed call, never a pass for "no draft".
    ("no_draft", None, None, False, 0, False),
])
def test_the_inbox_runner_checks_the_verdict_before_judging(
    monkeypatch: pytest.MonkeyPatch, expect: str, wants: bool | None, reply: str | None, judged: bool,
    overall: int, passed: bool,
) -> None:
    from openexecutive.delegation import inbox
    from openexecutive.delegation.inbox_classifier import Verdict

    seen: dict[str, Any] = {}
    verdict = None if wants is None else (
        Verdict(True, "scheduling", 0.9) if wants else Verdict(False, "thanks", 0.9)
    )

    async def fake_reply_for(person: Any, message: Any, thread: Any, *, relation: str, own: set[str]) -> Any:
        seen["args"] = (person.email, message.text, relation, own)
        return verdict, _reply() if reply == "reply" else reply

    async def fake_judge(scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        seen["outcome"] = outcome
        return {"overall": 4}

    monkeypatch.setattr(inbox, "reply_for", fake_reply_for)
    monkeypatch.setattr(runner_module, "judge_inbox", fake_judge)
    scenario = validate_scenario_yaml(_INBOX.replace("expect: draft", f"expect: {expect}"))
    result = asyncio.run(runner_module.run_inbox_scenario(scenario))
    assert seen["args"] == ("olivia@fernway.example", "2pm or 4pm?", "contact", {"olivia@fernway.example"})
    assert ("outcome" in seen) is judged
    assert result["scores"]["overall"] == overall and result["passed"] is passed
    # Its newest message isn't verified: handled as a stranger, and said so.
    assert result["outcome"]["handled_as"] == "stranger"
    assert result["outcome"]["drafted"] is (reply == "reply")
    if reply == "reply":
        assert result["outcome"]["reply"]["open_questions"] == ["2pm or 4pm?"]
    assert result["outcome"]["no_reply_because"] == (reply if reply not in ("reply", None) else None)


def test_the_inbox_kind_streams_through_the_suite_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import inbox
    from openexecutive.delegation.inbox_classifier import Verdict

    (tmp_path / "i.yaml").write_text(_INBOX)
    monkeypatch.setenv("EVAL_SCENARIOS_PATH", str(tmp_path))

    async def fake_reply_for(*a: Any, **kw: Any) -> Any:
        return Verdict(True, "scheduling", 0.9), _reply()

    async def fake_judge(scenario: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(inbox, "reply_for", fake_reply_for)
    monkeypatch.setattr(runner_module, "judge_inbox", fake_judge)

    async def drive() -> list[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        passed = [0]
        run_one = runner_module._make_run_one(
            kind="inbox", store=None, sem=asyncio.Semaphore(1), queue=queue,
            passed=passed, total=1, cancel_event=None,
        )
        scenario = next(s for s in load_scenarios(kind="inbox") if s["id"] == "i1")
        await run_one(0, scenario)
        assert passed == [1]
        events = []
        while not queue.empty():
            event = await queue.get()
            assert event is not None
            events.append(event)
        return events

    events = asyncio.run(drive())
    assert [e["type"] for e in events] == ["scenario_start", "scenario_done"]
    assert events[1]["outcome"]["reply"]["body"] == "Tuesday still works."


def test_the_inbox_judge_sees_the_thread_the_draft_and_its_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import asdict
    from types import SimpleNamespace

    prompts: list[str] = []

    class Provider:
        async def messages_create(self, **kw: Any) -> Any:
            prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(text='{"overall": 4, "notes": "fine"}')])

    monkeypatch.setattr(judges_module, "get_provider", lambda model: Provider())
    scenario = validate_scenario_yaml(_INBOX)
    outcome = {"verdict": {"kind": "scheduling"}, "reply": asdict(_reply(flags=["others_on_thread"]))}
    assert asyncio.run(judges_module.judge_inbox(scenario, outcome)) == {"overall": 4, "notes": "fine"}
    prompt = prompts[0]
    assert "2pm or 4pm?" in prompt and "Tuesday works." in prompt
    assert "To: dana@northpeak.example" in prompt and "Tuesday still works." in prompt
    assert '["2pm or 4pm?"]' in prompt and "others_on_thread" in prompt
    assert "THE SENDER IS: contact" in prompt
