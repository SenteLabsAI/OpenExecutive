"""Unit tests for the specialist router — covers episodic_context plumbing.

Verifies route_to_specialist and route_parallel forward `episodic_context`
to BaseAgent.analyze. The agents in SPECIALIST_REGISTRY are patched so no
real API call is made.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.orchestrator.router import (  # noqa: E402
    SPECIALIST_REGISTRY,
    route_parallel,
    route_to_specialist,
)


def test_route_to_specialist_passes_episodic_to_analyze(monkeypatch) -> None:
    monkeypatch.setattr(
        "openexecutive.orchestrator.router.load_company_stage", lambda: ""
    )
    analyze_mock = AsyncMock(return_value="analysis result")
    with patch.object(SPECIALIST_REGISTRY["cso"], "analyze", analyze_mock):
        result = asyncio.run(
            route_to_specialist(
                specialist_name="cso",
                query="strategic question",
                context="ctx",
                retrieved_knowledge="rag",
                episodic_context="EPISODIC",
            )
        )
    assert result == "analysis result"
    analyze_mock.assert_awaited_once_with(
        query="strategic question",
        context="ctx",
        retrieved_knowledge="rag",
        episodic_context="EPISODIC",
        failure_cases="",
        department_memory="",
        company_stage="",
        actor="specialist_workflow",
    )


def test_route_parallel_tags_chat_consults_with_the_specialist_actor() -> None:
    cso_mock = AsyncMock(return_value="x")
    with patch.object(SPECIALIST_REGISTRY["cso"], "analyze", cso_mock):
        asyncio.run(
            route_parallel(
                [{"specialist": "cso", "query": "q"}],
                retrieved_knowledge_map={"cso": ""},
            )
        )
    assert cso_mock.await_args.kwargs["actor"] == "specialist"


def test_route_to_specialist_defaults_episodic_to_empty_string() -> None:
    analyze_mock = AsyncMock(return_value="x")
    with patch.object(SPECIALIST_REGISTRY["cfo"], "analyze", analyze_mock):
        asyncio.run(route_to_specialist(specialist_name="cfo", query="q"))
    assert analyze_mock.await_args.kwargs["episodic_context"] == ""


def test_route_parallel_distributes_episodic_to_each_specialist() -> None:
    """One per-turn value should reach every specialist in the batch.

    Passing an empty retrieved_knowledge_map={} short-circuits the per-call
    auto-RAG (which would otherwise call into ChromaDB).
    """
    cso_mock = AsyncMock(return_value="cso-out")
    cfo_mock = AsyncMock(return_value="cfo-out")
    with (
        patch.object(SPECIALIST_REGISTRY["cso"], "analyze", cso_mock),
        patch.object(SPECIALIST_REGISTRY["cfo"], "analyze", cfo_mock),
    ):
        results = asyncio.run(
            route_parallel(
                calls=[
                    {"specialist": "cso", "query": "strategy q", "context": "c1"},
                    {"specialist": "cfo", "query": "finance q", "context": "c2"},
                ],
                retrieved_knowledge_map={},
                episodic_context="SHARED_PAST_DECISIONS",
            )
        )
    assert results == ["cso-out", "cfo-out"]
    assert cso_mock.await_args.kwargs["episodic_context"] == "SHARED_PAST_DECISIONS"
    assert cfo_mock.await_args.kwargs["episodic_context"] == "SHARED_PAST_DECISIONS"


def test_route_parallel_preserves_per_specialist_retrieved_knowledge() -> None:
    """retrieved_knowledge_map is per-specialist; episodic_context is per-turn.
    They must compose independently.
    """
    cso_mock = AsyncMock(return_value="x")
    cfo_mock = AsyncMock(return_value="y")
    with (
        patch.object(SPECIALIST_REGISTRY["cso"], "analyze", cso_mock),
        patch.object(SPECIALIST_REGISTRY["cfo"], "analyze", cfo_mock),
    ):
        asyncio.run(
            route_parallel(
                calls=[
                    {"specialist": "cso", "query": "q1"},
                    {"specialist": "cfo", "query": "q2"},
                ],
                retrieved_knowledge_map={"cso": "STRAT_RAG", "cfo": "FIN_RAG"},
                episodic_context="EPISODIC",
            )
        )
    assert cso_mock.await_args.kwargs["retrieved_knowledge"] == "STRAT_RAG"
    assert cfo_mock.await_args.kwargs["retrieved_knowledge"] == "FIN_RAG"
    assert cso_mock.await_args.kwargs["episodic_context"] == "EPISODIC"
    assert cfo_mock.await_args.kwargs["episodic_context"] == "EPISODIC"


def test_route_to_specialist_unknown_returns_error_string() -> None:
    """Pre-existing behaviour — guard against regression."""
    result = asyncio.run(route_to_specialist(specialist_name="nope", query="x"))
    assert "Unknown specialist" in result
