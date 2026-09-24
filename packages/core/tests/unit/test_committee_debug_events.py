"""Committee path must emit debug events for the Agent Activity panel.

The route-level test in ``tests/integration/test_chat_committee.py`` stubs
out ``Executive`` entirely, so it cannot verify that the real
``stream_chat_with_committee`` actually emits debug events. This file
exercises the method directly with the surrounding components patched.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator.committee_reviewers import Critique
from openexecutive.orchestrator.debug_events import DebugCollector
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module's audit rows out of the default `./episodic_memory.db`.

    A committee turn schedules episodic extraction fire-and-forget, and the
    resulting `memory_extraction` row goes through `audit.log_event`, which
    targets the default DB whatever `db_path` the extraction was handed. The
    rows then surface as real data in other modules' assertions during a full
    run only — the audit-log pollution trap in CLAUDE.md.
    """
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *a, **kw: None, raising=False
    )


class _NoopAsyncStream:
    """Async context manager yielding zero text deltas — enough for the
    revision call to complete without making real Anthropic requests."""

    async def __aenter__(self) -> _NoopAsyncStream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _NoopAsyncStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


def _emit_debug_events_for_committee_turn() -> list[dict[str, Any]]:
    """Run a committee turn with all I/O stubbed and return collected events."""
    session = Session(session_id="t", company_profile=CompanyProfile())
    collector = DebugCollector(turn_id="turn-x")

    async def _fake_draft_loop(*_a: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        # Populate the sinks like the real loop does for a single consult.
        if kwargs.get("consulted_out") is not None:
            kwargs["consulted_out"].extend(["cfo"])
        if kwargs.get("specialist_outputs_out") is not None:
            kwargs["specialist_outputs_out"]["cfo"] = "burn 100k/mo"
        yield "draft "
        yield "text"

    canned_critiques = [
        Critique("quality_judge", "high", "too generic", "be specific"),
        Critique("cfo_domain", "medium", "missing numbers", "use 100k"),
        Critique("cso_domain", "low", "ok", "none"),
    ]

    async def _fake_review(*_a: Any, **_k: Any) -> list[Critique]:
        return canned_critiques

    def _fake_stream(**_kwargs: Any) -> _NoopAsyncStream:
        return _NoopAsyncStream()

    class _FakeProvider:
        def messages_stream(self, **kwargs: Any) -> _NoopAsyncStream:
            return _fake_stream(**kwargs)

    with (
        patch.object(Executive, "_stream_agent_loop", new=_fake_draft_loop),
        patch(
            "openexecutive.orchestrator.committee.Committee.review",
            new=_fake_review,
        ),
        # Revision call resolves provider via ``get_provider``; stub it so
        # the no-text fake stream completes without an Anthropic call.
        patch(
            "openexecutive.orchestrator.executive.get_provider",
            return_value=_FakeProvider(),
        ),
    ):
        exec_ = Executive()

        async def _drive() -> list[dict[str, Any]]:
            collected: list[dict[str, Any]] = []
            async for item in exec_.stream_chat_with_committee(
                user_message="should we hire?",
                session=session,
                debug_collector=collector,
            ):
                if isinstance(item, dict):
                    collected.append(item)
            return collected

        return asyncio.run(_drive())


def test_committee_emits_review_start_done_and_revision_start() -> None:
    events = _emit_debug_events_for_committee_turn()
    debug_kinds = [
        e.get("kind") for e in events if e.get("type") == "debug_event"
    ]
    assert "committee_review_start" in debug_kinds
    assert "committee_review_done" in debug_kinds
    assert "committee_revision_start" in debug_kinds
    # Ordering: start before done before revision_start.
    i_start = debug_kinds.index("committee_review_start")
    i_done = debug_kinds.index("committee_review_done")
    i_rev = debug_kinds.index("committee_revision_start")
    assert i_start < i_done < i_rev


def test_committee_review_start_event_carries_planned_reviewers() -> None:
    events = _emit_debug_events_for_committee_turn()
    start = next(
        e for e in events
        if e.get("type") == "debug_event" and e.get("kind") == "committee_review_start"
    )
    data = start["data"]
    assert data["reviewers"] == ["quality_judge", "cfo_domain", "cso_domain"]
    assert data["consulted"] == ["cfo"]
    assert data["draft_length"] > 0


def test_committee_review_done_event_carries_severity_breakdown() -> None:
    events = _emit_debug_events_for_committee_turn()
    done = next(
        e for e in events
        if e.get("type") == "debug_event" and e.get("kind") == "committee_review_done"
    )
    critiques = done["data"]["critiques"]
    severities = sorted(c["severity"] for c in critiques)
    assert severities == ["high", "low", "medium"]
    # critique_preview is the short text — not the full critique.
    for c in critiques:
        assert "critique_preview" in c
        assert len(c["critique_preview"]) <= 200
