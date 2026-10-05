"""The morning reflection can search the knowledge base (``search_knowledge``).

Chat looks knowledge up before each turn; the reflection reads the org's
state instead, so it gets the knowledge base as a tool, reads its passages
back in full, and keeps them out of the one-line tool log.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openexecutive.knowledge import retriever
from openexecutive.memory import episodic
from openexecutive.orchestrator.knowledge_tools import (
    RESULT_CHARS,
    handle_search_knowledge,
)
from openexecutive.workflows._synthesis import execute_tool_calls
from openexecutive.workflows.executive_reflection import (
    ExecutiveReflectionInput,
    ExecutiveReflectionWorkflow,
)

# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_what_retrieve_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_retrieve(query: str, **_kw: Any) -> str:
        seen.append(query)
        return "### From your company documents:\n[fleet_terms.pdf] 6% off list for fleet orders."

    monkeypatch.setattr(retriever, "retrieve", fake_retrieve)
    out = await handle_search_knowledge({"query": "  fleet discount  "})
    assert seen == ["fleet discount"]
    assert "6% off list" in out


@pytest.mark.asyncio
async def test_search_says_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retriever, "retrieve", lambda query, **_kw: "")
    out = await handle_search_knowledge({"query": "fleet discount"})
    assert out == "Nothing in the knowledge base matches that."


@pytest.mark.asyncio
async def test_search_needs_a_query(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, **_kw: Any) -> str:
        raise AssertionError("retrieve must not run without a query")

    monkeypatch.setattr(retriever, "retrieve", boom)
    out = json.loads(await handle_search_knowledge({"query": "   "}))
    assert "error" in out


@pytest.mark.asyncio
async def test_search_failure_is_an_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, **_kw: Any) -> str:
        raise RuntimeError("chroma down")

    monkeypatch.setattr(retriever, "retrieve", boom)
    out = json.loads(await handle_search_knowledge({"query": "fleet discount"}))
    assert "error" in out


@pytest.mark.asyncio
async def test_search_result_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retriever, "retrieve", lambda query, **_kw: "x" * (RESULT_CHARS * 2))
    out = await handle_search_knowledge({"query": "fleet discount"})
    assert len(out) == RESULT_CHARS


# --------------------------------------------------------------------------
# Wide results in the shared tool loop
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, name: str, tool_input: dict[str, Any], id: str) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = tool_input
        self.id = id


class _Response:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks
        self.stop_reason = "tool_use"


@pytest.mark.asyncio
async def test_wide_results_only_widen_the_named_tool() -> None:
    long = "y" * 1000

    async def handler(_input: dict[str, Any]) -> str:
        return long

    response = _Response([_Block("search_knowledge", {}, "a"), _Block("lookup_person", {}, "b")])
    summaries = await execute_tool_calls(
        response,
        {"search_knowledge": handler, "lookup_person": handler},
        result_chars=160,
        wide_results={"search_knowledge": 4000},
    )
    by_tool = {s["tool"]: s["result_preview"] for s in summaries}
    assert len(by_tool["search_knowledge"]) == 1000
    assert len(by_tool["lookup_person"]) == 160


# --------------------------------------------------------------------------
# The reflection pass
# --------------------------------------------------------------------------


class _Text:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Final:
    def __init__(self, text: str) -> None:
        self.content = [_Text(text)]
        self.stop_reason = "end_turn"


class _Recorder:
    """Hands out responses in turn and keeps every request."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_reflection_searches_knowledge_and_reads_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "refl_knowledge.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)

    # The passage the decision turns on sits past the 160 characters other
    # tools get back.
    passage = "### From your company documents:\n" + ("context " * 40) + "FLEET-TERMS: 6% off list."
    assert passage.index("FLEET-TERMS") > 160
    queries: list[str] = []

    def fake_retrieve(query: str, **_kw: Any) -> str:
        queries.append(query)
        return passage

    monkeypatch.setattr(retriever, "retrieve", fake_retrieve)

    from openexecutive import providers

    recorder = _Recorder([
        _Response([_Block("search_knowledge", {"query": "Westline fleet terms"}, "tu_k")]),
        _Final("**Quiet:** Nothing else worth acting on this morning."),
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _model: recorder)

    events: list[Any] = []
    async for event in ExecutiveReflectionWorkflow().run(
        inputs=ExecutiveReflectionInput(), store=None  # type: ignore[arg-type]
    ):
        events.append(event)

    assert "error" not in [e.type for e in events]
    first = recorder.calls[0]
    assert "search_knowledge" in [t["name"] for t in first["tools"]]
    assert "`search_knowledge`" in first["system"]
    assert queries == ["Westline fleet terms"]

    # The model reads the whole passage back, not a 160-character preview.
    tool_turn = recorder.calls[1]["messages"][-1]["content"]
    returned = next(b for b in tool_turn if b.get("tool_use_id") == "tu_k")
    assert "FLEET-TERMS: 6% off list." in str(returned["content"])

    # The tool log in the artifact stays one short line.
    artifact = next(e for e in events if e.type == "artifact").content
    log_line = next(line for line in artifact.splitlines() if "`search_knowledge`" in line)
    assert "FLEET-TERMS" not in log_line
