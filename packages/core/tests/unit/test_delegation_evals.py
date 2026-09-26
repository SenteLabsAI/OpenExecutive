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
