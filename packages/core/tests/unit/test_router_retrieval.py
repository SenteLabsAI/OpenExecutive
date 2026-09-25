"""Unit tests for route_parallel's per-specialist retrieval map."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from openexecutive.orchestrator import router


@pytest.fixture
def stub_agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Replace SPECIALIST_REGISTRY with stubs that record what they received."""

    received: dict[str, list[dict[str, Any]]] = {}

    class StubAgent:
        def __init__(self, name: str) -> None:
            self.name = name

        async def analyze(
            self,
            query: str,
            context: str = "",
            retrieved_knowledge: str = "",
            episodic_context: str = "",
            failure_cases: str = "",
            department_memory: str = "",
            *,
            company_stage: str = "",
            actor: str = "specialist",
        ) -> str:
            received.setdefault(self.name, []).append(
                {
                    "query": query,
                    "context": context,
                    "retrieved": retrieved_knowledge,
                    "episodic": episodic_context,
                    "failures": failure_cases,
                    "department_memory": department_memory,
                }
            )
            return f"answer-from-{self.name}"

    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "cfo", StubAgent("cfo"))
    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "chro", StubAgent("chro"))
    return received


def test_route_parallel_auto_retrieves_per_specialist(
    stub_agents: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no map is supplied, each specialist gets its own domain-filtered RAG."""

    seen_calls: list[tuple[str, str | None]] = []

    def fake_retrieve(query: str, specialist_name: str | None = None, **_: Any) -> str:
        seen_calls.append((query, specialist_name))
        return f"<rag for {specialist_name}: {query}>"

    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve", fake_retrieve
    )
    # route_parallel now also fans out a domain-filtered failure-case
    # lookup; stub it so the test never reaches ChromaDB.
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve_failures",
        lambda **_: "",
    )

    results = asyncio.run(
        router.route_parallel(
            calls=[
                {"specialist": "cfo", "query": "what is our burn?"},
                {"specialist": "chro", "query": "should we layoff 10%?"},
            ]
        )
    )

    assert results == ["answer-from-cfo", "answer-from-chro"]
    assert sorted(seen_calls) == sorted([
        ("what is our burn?", "cfo"),
        ("should we layoff 10%?", "chro"),
    ])
    # Each agent receives its own RAG block, not the other specialist's.
    assert "<rag for cfo: what is our burn?>" in stub_agents["cfo"][0]["retrieved"]
    assert "<rag for chro:" in stub_agents["chro"][0]["retrieved"]
    assert "burn" not in stub_agents["chro"][0]["retrieved"]


def test_route_parallel_uses_supplied_map_when_provided(
    stub_agents: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-built maps short-circuit auto-retrieval — used by tests/caching callers."""

    retrieve_called = False

    def boom(*_a: Any, **_k: Any) -> str:
        nonlocal retrieve_called
        retrieve_called = True
        return "should-not-be-used"

    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", boom)

    asyncio.run(
        router.route_parallel(
            calls=[{"specialist": "cfo", "query": "q"}],
            retrieved_knowledge_map={"cfo": "preloaded-context"},
        )
    )

    assert not retrieve_called
    assert stub_agents["cfo"][0]["retrieved"] == "preloaded-context"


def test_route_parallel_preserves_call_order(
    stub_agents: dict[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order of returned results must match order of input calls for zip()."""
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve",
        lambda **_: "ctx",
    )
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve_failures",
        lambda **_: "",
    )

    results = asyncio.run(
        router.route_parallel(
            calls=[
                {"specialist": "chro", "query": "a"},
                {"specialist": "cfo", "query": "b"},
                {"specialist": "chro", "query": "c"},
            ]
        )
    )
    assert results == ["answer-from-chro", "answer-from-cfo", "answer-from-chro"]
