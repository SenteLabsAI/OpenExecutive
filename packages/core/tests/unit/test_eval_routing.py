"""A chat scenario's routing expectation is scored, not just listed.

``required_routing`` names the specialists the Executive must consult and
``no_routing: true`` says it must consult none. The chat runner reads which
specialists the turn consulted off a ``DebugCollector`` and fails the
scenario when the expectation is not met, whatever the judge's score.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.evals import runner as runner_module
from openexecutive.evals.scenarios import (
    load_scenarios,
    routing_check,
    scenario_routing,
    validate_scenario_yaml,
)
from openexecutive.orchestrator.router import SPECIALIST_REGISTRY


def test_routing_check_needs_every_required_specialist() -> None:
    s = {"required_routing": ["cfo", "cso"]}
    assert routing_check(s, ["cso", "cfo", "cfo"]) == {
        "consulted": ["cfo", "cso"], "missing": [], "ok": True,
    }
    assert routing_check(s, ["cfo", "gc"])["missing"] == ["cso"]
    assert routing_check(s, ["cfo", "gc"])["ok"] is False


def test_no_routing_fails_on_any_consult() -> None:
    s = {"no_routing": True}
    assert routing_check(s, [])["ok"] is True
    assert routing_check(s, ["cmo"])["ok"] is False


def test_no_expectation_returns_none() -> None:
    assert routing_check({}, ["cfo"]) is None


@pytest.mark.parametrize(
    "body",
    [
        "required_routing: cfo\n",
        "required_routing: [cfo]\nno_routing: true\n",
        "no_routing: yes please\n",
    ],
)
def test_malformed_routing_is_rejected(body: str) -> None:
    with pytest.raises(ValueError):
        validate_scenario_yaml(f"id: x\ndomain: finance\nquery: q\n{body}")


def test_builtin_scenarios_name_real_specialists() -> None:
    for s in load_scenarios():
        required, _ = scenario_routing(s)
        assert set(required) <= set(SPECIALIST_REGISTRY), s["id"]


def test_runner_fails_a_scenario_whose_routing_is_not_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "needs_cfo.yaml").write_text(
        "id: needs_cfo\ndomain: finance\nquery: runway?\nrequired_routing: [cfo]\n"
    )
    (tmp_path / "skips.yaml").write_text(
        "id: skips\ndomain: routing\nquery: thank-you note\nno_routing: true\n"
    )
    monkeypatch.setenv("EVAL_SCENARIOS_PATH", str(tmp_path))

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        # Both turns consult the CFO: right for one, wrong for the other.
        kwargs["debug_collector"].emit("specialist_start", {"specialist": "cfo"})
        return "a response"

    async def fake_judge(scenario: dict[str, Any], response: str) -> dict[str, Any]:
        return {"overall": 5}

    from openexecutive.orchestrator.executive import Executive

    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)

    async def drive() -> dict[str, dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        run_one = runner_module._make_run_one(
            kind="chat", store=None, sem=asyncio.Semaphore(2), queue=queue,
            passed=[0], total=2, cancel_event=None,
        )
        scenarios = [s for s in load_scenarios(kind="chat") if s["id"] in ("needs_cfo", "skips")]
        for i, s in enumerate(scenarios):
            await run_one(i, s)
        done: dict[str, dict[str, Any]] = {}
        while not queue.empty():
            ev = await queue.get()
            if ev is not None and ev["type"] == "scenario_done":
                done[ev["scenario_id"]] = ev
        return done

    done = asyncio.run(drive())
    assert done["needs_cfo"]["passed"] is True
    assert done["needs_cfo"]["scores"]["routing"]["consulted"] == ["cfo"]
    assert done["skips"]["passed"] is False
    assert done["skips"]["scores"]["routing"]["ok"] is False


def test_both_personas_say_when_to_consult() -> None:
    from openexecutive.prompts.executive_persona import (
        EXECUTIVE_PERSONA_PROMPT,
        EXECUTIVE_PERSONA_SOLO_PROMPT,
    )

    for prompt in (EXECUTIVE_PERSONA_PROMPT, EXECUTIVE_PERSONA_SOLO_PROMPT):
        assert "call `consult_specialist` for that area before you answer" in prompt
        assert "Skip it for lookups" in prompt
