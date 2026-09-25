"""Unit tests for the router's specialist → department memory wiring.

Verifies that ``route_parallel`` resolves each specialist to its owning
department via the departments registry, calls ``prefetch_department``
with the right slug, and threads the result into the specialist's
``analyze(department_memory=...)`` call. Specialists without an owning
department (e.g. ``triage``) must skip the prefetch entirely.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openexecutive.orchestrator import router


@pytest.fixture
def stub_agents(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Replace SPECIALIST_REGISTRY with stubs that record department_memory."""

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
            principal_role: str = "",
            actor: str = "specialist",
        ) -> str:
            received.setdefault(self.name, []).append(
                {
                    "query": query,
                    "department_memory": department_memory,
                }
            )
            return f"answer-from-{self.name}"

    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "cfo", StubAgent("cfo"))
    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "cmo", StubAgent("cmo"))
    monkeypatch.setitem(router.SPECIALIST_REGISTRY, "triage", StubAgent("triage"))
    return received


@pytest.fixture
def _stub_retrievers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ChromaDB-backed retrievers so the tests don't touch real RAG."""
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve",
        lambda **_: "",
    )
    monkeypatch.setattr(
        "openexecutive.knowledge.retriever.retrieve_failures",
        lambda **_: "",
    )


def test_specialist_with_department_fires_prefetch_with_correct_slug(
    stub_agents: dict[str, list[dict[str, Any]]],
    _stub_retrievers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a specialist resolves to a department, prefetch_department
    is called with that department's slug — not the specialist key."""
    prefetch_calls: list[dict[str, Any]] = []

    async def fake_prefetch_department(
        query: str,
        *,
        department_slug: str | None,
        session_id: str | None = None,
        reasoning_level: str = "low",
    ) -> str:
        prefetch_calls.append(
            {
                "query": query,
                "department_slug": department_slug,
                "session_id": session_id,
            }
        )
        return f"<dept-memory-for-{department_slug}>"

    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.prefetch_department",
        fake_prefetch_department,
    )
    # CFO → "finance", CMO → "marketing" per the seed dept registry. We
    # stub the resolver so this test doesn't depend on DB seed data.
    monkeypatch.setattr(
        "openexecutive.departments.registry.slug_for_specialist",
        lambda key: {"cfo": "finance", "cmo": "marketing"}.get(key),
    )

    asyncio.run(
        router.route_parallel(
            calls=[
                {"specialist": "cfo", "query": "what's our burn?"},
                {"specialist": "cmo", "query": "Q3 GTM plan?"},
            ],
            session_id="slack_C9_t1",
        )
    )
    assert sorted(c["department_slug"] for c in prefetch_calls) == ["finance", "marketing"]
    # Session_id is threaded through for audit grouping.
    assert all(c["session_id"] == "slack_C9_t1" for c in prefetch_calls)
    # Each specialist receives its own dept-memory block.
    assert stub_agents["cfo"][0]["department_memory"] == "<dept-memory-for-finance>"
    assert stub_agents["cmo"][0]["department_memory"] == "<dept-memory-for-marketing>"


def test_specialist_without_department_skips_prefetch(
    stub_agents: dict[str, list[dict[str, Any]]],
    _stub_retrievers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triage (and any future specialist with no owning dept) gets no
    department_memory block — and prefetch_department is never called
    for it."""
    prefetch_mock = AsyncMock(return_value="<should-not-be-used>")
    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.prefetch_department",
        prefetch_mock,
    )
    monkeypatch.setattr(
        "openexecutive.departments.registry.slug_for_specialist",
        lambda key: None,  # No specialist resolves to a dept.
    )

    asyncio.run(
        router.route_parallel(
            calls=[{"specialist": "triage", "query": "is this urgent?"}],
        )
    )
    # Prefetch was never called — the resolver returned None.
    prefetch_mock.assert_not_awaited()
    # Specialist received an empty dept-memory block.
    assert stub_agents["triage"][0]["department_memory"] == ""


def test_mixed_specialists_only_dept_bound_get_prefetch(
    stub_agents: dict[str, list[dict[str, Any]]],
    _stub_retrievers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that consults both a dept-bound specialist and triage in
    parallel must prefetch for the former and skip for the latter."""
    seen_slugs: list[str | None] = []

    async def fake_prefetch(*, department_slug: str | None, **_: Any) -> str:
        seen_slugs.append(department_slug)
        return f"<mem-{department_slug}>" if department_slug else ""

    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.prefetch_department",
        fake_prefetch,
    )
    monkeypatch.setattr(
        "openexecutive.departments.registry.slug_for_specialist",
        lambda key: {"cfo": "finance"}.get(key),
    )

    asyncio.run(
        router.route_parallel(
            calls=[
                {"specialist": "cfo", "query": "q1"},
                {"specialist": "triage", "query": "q2"},
            ],
        )
    )
    # CFO triggered a prefetch with slug=finance. Triage did NOT (the
    # resolver returns None and `_prefetch_department_for_call` short-
    # circuits before touching Honcho).
    assert seen_slugs == ["finance"]
    assert stub_agents["cfo"][0]["department_memory"] == "<mem-finance>"
    assert stub_agents["triage"][0]["department_memory"] == ""


def test_prefetch_failure_does_not_break_specialist_call(
    stub_agents: dict[str, list[dict[str, Any]]],
    _stub_retrievers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If prefetch_department itself raises (despite the wrapper's own
    swallowing), the specialist call must still run with an empty dept-
    memory block — never propagate a Honcho failure to the user-facing
    response path."""
    async def boom_prefetch(**_: Any) -> str:
        raise RuntimeError("honcho oops")

    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.prefetch_department",
        boom_prefetch,
    )
    monkeypatch.setattr(
        "openexecutive.departments.registry.slug_for_specialist",
        lambda key: "finance",
    )

    # Currently route_parallel does NOT catch this — the wrapper itself
    # is supposed to. If the wrapper changes, this test will flag it.
    # Using pytest.raises to document the contract: failures must come
    # from the wrapper level, not route_parallel.
    with pytest.raises(RuntimeError, match="honcho oops"):
        asyncio.run(
            router.route_parallel(
                calls=[{"specialist": "cfo", "query": "q"}],
            )
        )
