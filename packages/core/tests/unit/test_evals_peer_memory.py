"""A scenario may hand the Executive a ``<peer_memory>`` block directly.

Evals run with no ``person_id``, so the Honcho prefetch never fires; the
``peer_memory_context`` scenario key is the only way to exercise how the
Executive USES the block, and the judge is shown the same block so it can
score "did not re-ask" and "the current message wins over an older note".
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openexecutive.evals import judges as judges_module
from openexecutive.evals import runner as runner_module
from openexecutive.evals.scenarios import load_scenarios

_MEMORY = "IDENTITY: Name: Priya Nair\n\n## Explicit Observations\n\n[2026-08-14 09:12:03] 3 wants tables"


def _write_scenarios(tmp_path: Path) -> None:
    (tmp_path / "with_memory.yaml").write_text(
        "id: with_memory\ndomain: operations\ndescription: d\n"
        "peer_memory_context: |\n  IDENTITY: Name: Priya Nair\n\n  ## Explicit Observations\n\n"
        "  [2026-08-14 09:12:03] 3 wants tables\n"
        "query: summary please\nexpected_topics: [summary]\n"
        "quality_criteria:\n  uses_table: true\n  verbose: false\n"
    )
    (tmp_path / "without_memory.yaml").write_text(
        "id: without_memory\ndomain: operations\ndescription: d\nquery: hello\nexpected_topics: [x]\n"
    )


def test_runner_passes_the_scenario_block_as_peer_memory_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_scenarios(tmp_path)
    monkeypatch.setenv("EVAL_SCENARIOS_PATH", str(tmp_path))
    seen: dict[str, Any] = {}

    async def fake_chat(self: Any, **kwargs: Any) -> str:
        seen[kwargs["user_message"]] = kwargs.get("peer_memory_context")
        return "a response"

    async def fake_judge(scenario: dict[str, Any], response: str) -> dict[str, Any]:
        return {"overall": 5}

    from openexecutive.orchestrator.executive import Executive

    monkeypatch.setattr(Executive, "chat", fake_chat)
    monkeypatch.setattr(runner_module, "judge_chat", fake_judge)

    async def drive() -> list[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        run_one = runner_module._make_run_one(
            kind="chat", store=None, sem=asyncio.Semaphore(2), queue=queue,
            passed=[0], total=2, cancel_event=None,
        )
        scenarios = load_scenarios(kind="chat")
        scenarios = [s for s in scenarios if s["id"] in ("with_memory", "without_memory")]
        for i, s in enumerate(scenarios):
            await run_one(i, s)
        events: list[dict[str, Any]] = []
        while not queue.empty():
            ev = await queue.get()
            if ev is not None:
                events.append(ev)
        return events

    events = asyncio.run(drive())
    assert {e["type"] for e in events} == {"scenario_start", "scenario_done"}
    assert seen["summary please"] == _MEMORY + "\n"
    assert seen["hello"] is None


def test_judge_prompt_shows_the_block_and_true_criteria_only_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    class _Msg:
        content = [type("T", (), {"text": '{"overall": 4}'})()]

    class _Provider:
        async def messages_create(self, **kwargs: Any) -> Any:
            prompts.append(kwargs["messages"][0]["content"])
            return _Msg()

    monkeypatch.setattr(judges_module, "get_provider", lambda model: _Provider())
    with_memory = {
        "id": "a", "query": "summary please", "expected_topics": ["summary"],
        "peer_memory_context": _MEMORY,
        "quality_criteria": {"uses_table": True, "verbose": False, "concise": True},
    }
    without = {"id": "b", "query": "hello", "expected_topics": ["x"], "quality_criteria": {"uses_table": True}}
    asyncio.run(judges_module.judge_chat(with_memory, "resp"))
    asyncio.run(judges_module.judge_chat(without, "resp"))
    assert "BACKGROUND THE ASSISTANT WAS GIVEN ABOUT THE ASKER" in prompts[0]
    assert "3 wants tables" in prompts[0]
    assert "Additional criteria this response must satisfy: uses table, concise" in prompts[0]
    assert "overall must be 2 or lower" in prompts[0]
    assert "verbose" not in prompts[0]
    # No peer memory: the judge prompt is exactly what it was before.
    assert "BACKGROUND" not in prompts[1] and "Additional criteria" not in prompts[1]


def test_peer_memory_scenario_ships_and_loads_as_chat() -> None:
    found = [s for s in load_scenarios(kind="chat") if s["id"] == "peer_memory_001"]
    assert len(found) == 1
    scenario = found[0]
    assert scenario["peer_memory_context"].startswith("IDENTITY: Name: Priya Nair")
    assert "52 units" in scenario["query"]
    assert scenario["quality_criteria"]["current_statement_overrides_memory"] is True
