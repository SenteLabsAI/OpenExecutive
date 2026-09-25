"""Regression tests for the executive_research routing budget.

The bug: ``_MAX_ROUTING_TOOLS_PER_RUN`` counted EVERY successful tool
call, including ``lookup_person`` — the read-only tool the synthesis
prompt explicitly tells the Executive to call first to resolve a
recipient. The Executive looked up its department heads, exhausted the
budget on the lookups, and the loop terminated before a single DM was
sent: "looks up people but never routes."

The fix treats read-only context lookups (``_NON_ROUTING_TOOLS``) as
free: they are passed to ``execute_tool_calls`` as ``free_tools`` (never
refused, never decrement the budget) and excluded from the budget-
accounting counts via ``_routing_ok``.

Two levels:
  - ``execute_tool_calls`` mechanism (hermetic, no provider).
  - ``_executive_synthesis_loop`` end-to-end with a stubbed provider —
    reproduces the original 5-lookups-then-no-DM failure.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from openexecutive.workflows import executive_research as er
from openexecutive.workflows._synthesis import execute_tool_calls

# --------------------------------------------------------------------- #
# Fake provider response shapes
# --------------------------------------------------------------------- #


class _ToolUse:
    def __init__(self, name: str, inp: dict[str, Any], id: str) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = inp
        self.id = id


class _Text:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _SequenceProvider:
    """Hands out one response per ``messages_create`` call."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def messages_create(self, **_kwargs: Any) -> Any:
        # Repeat the last response if the loop asks for more than we staged.
        return self._responses.pop(0) if self._responses else self._last

    @property
    def _last(self) -> Any:  # pragma: no cover - defensive
        return _Resp([_Text("done")], "end_turn")


# --------------------------------------------------------------------- #
# Level A — execute_tool_calls free_tools mechanism
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_free_tool_runs_even_when_budget_is_zero() -> None:
    """With budget exhausted, a free tool still executes; a metered tool
    is refused with the 'over budget' stub."""
    calls: list[str] = []

    async def _lookup(_inp: dict[str, Any]) -> str:
        calls.append("lookup_person")
        return '{"matches": []}'

    async def _dm(_inp: dict[str, Any]) -> str:
        calls.append("send_slack_dm")
        return '{"status": "sent"}'

    resp = _Resp(
        [
            _ToolUse("lookup_person", {"query": "head of sales"}, "a"),
            _ToolUse("send_slack_dm", {"user_id": "U1", "text": "hi"}, "b"),
        ],
        "tool_use",
    )
    summaries = await execute_tool_calls(
        resp,
        {"lookup_person": _lookup, "send_slack_dm": _dm},
        budget_remaining=0,
        free_tools=frozenset({"lookup_person"}),
    )

    assert calls == ["lookup_person"]  # DM refused, lookup ran
    by_tool = {s["tool"]: s for s in summaries}
    assert by_tool["lookup_person"]["ok"] is True
    assert by_tool["send_slack_dm"]["ok"] is False
    assert by_tool["send_slack_dm"]["result_preview"] == "over budget — skipped"


@pytest.mark.asyncio
async def test_free_tool_does_not_decrement_budget() -> None:
    """A budget of 1 must still be spendable on the one outbound call even
    when free lookups precede it in the same response."""
    calls: list[str] = []

    async def _lookup(_inp: dict[str, Any]) -> str:
        calls.append("lookup_person")
        return '{"matches": []}'

    async def _dm(_inp: dict[str, Any]) -> str:
        calls.append("send_slack_dm")
        return '{"status": "sent"}'

    resp = _Resp(
        [
            _ToolUse("lookup_person", {"query": "a"}, "1"),
            _ToolUse("lookup_person", {"query": "b"}, "2"),
            _ToolUse("send_slack_dm", {"user_id": "U1", "text": "hi"}, "3"),
        ],
        "tool_use",
    )
    summaries = await execute_tool_calls(
        resp,
        {"lookup_person": _lookup, "send_slack_dm": _dm},
        budget_remaining=1,
        free_tools=frozenset({"lookup_person"}),
    )

    # Two lookups did NOT eat the budget; the single DM still ran.
    assert calls == ["lookup_person", "lookup_person", "send_slack_dm"]
    assert all(s["ok"] for s in summaries)


def test_routing_ok_excludes_read_only_lookups() -> None:
    calls = [
        {"tool": "lookup_person", "ok": True},
        {"tool": "list_people", "ok": True},
        {"tool": "ask_about_person", "ok": True},
        {"tool": "send_slack_dm", "ok": True},
        {"tool": "create_alert", "ok": True},
        {"tool": "send_discord_dm", "ok": False},  # failed → not counted
    ]
    # Only the two successful OUTBOUND calls count against the budget.
    assert er._routing_ok(calls) == 2


def test_non_routing_tools_are_read_only_set() -> None:
    assert frozenset(
        {"lookup_person", "list_people", "ask_about_person"}
    ) == er._NON_ROUTING_TOOLS


# --------------------------------------------------------------------- #
# Level B — synthesis loop reproduces the original bug
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_synthesis_routes_after_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original failure: iteration 1 looks up five department heads,
    iteration 2 routes the finding. Lookups must NOT consume the routing
    budget, so the outbound call in iteration 2 actually fires.

    Pre-fix, the five lookups counted as five 'ok' routing calls, the
    budget (5) was exhausted, and the loop terminated after iteration 1 —
    the outbound call was never even requested. Post-fix, it fires.

    The outbound call is `create_alert`, a tool this pass offers. (It used
    to be `send_slack_dm`, which the pass withholds — the test only passed
    because a withheld tool still ran when emitted; see
    test_synthesis_skips_a_tool_it_did_not_offer.)
    """
    fired: list[str] = []

    async def _lookup(_inp: dict[str, Any]) -> str:
        fired.append("lookup_person")
        return '{"matches": [{"slack_user_id": "U1"}]}'

    async def _route(inp: dict[str, Any]) -> str:
        fired.append("create_alert")
        return json.dumps({"status": "created", "headline": inp.get("headline", "")})

    from openexecutive.orchestrator import executive

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "lookup_person", _lookup)
    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "create_alert", _route)
    # Keep the synthetic-Session seeding independent of any real DB.
    monkeypatch.setattr("openexecutive.people.store.list_people", lambda: [])

    # iter 1: five lookups (stop_reason tool_use → loop continues).
    # iter 2: one DM + summary text (stop_reason end_turn → loop ends).
    iter1 = _Resp(
        [_ToolUse("lookup_person", {"query": f"head {i}"}, f"l{i}") for i in range(5)],
        "tool_use",
    )
    iter2 = _Resp(
        [
            _ToolUse("create_alert", {"headline": "Competitor cut prices"}, "d1"),
            _Text("**Acted on:**\n- Flagged the price cut."),
        ],
        "end_turn",
    )
    from openexecutive import providers

    seq = _SequenceProvider([iter1, iter2])
    monkeypatch.setattr(providers, "get_provider", lambda _model: seq)

    narrative, tool_calls = await er._executive_synthesis_loop([_finding()])

    # The outbound call fired exactly once, AFTER the five lookups.
    assert fired.count("lookup_person") == 5
    assert fired.count("create_alert") == 1

    routed = [t for t in tool_calls if t["tool"] == "create_alert"]
    assert len(routed) == 1
    assert routed[0]["ok"] is True
    # It was NOT refused as over-budget.
    assert routed[0]["result_preview"] != "over budget — skipped"
    assert "Acted on" in narrative


@pytest.mark.asyncio
async def test_synthesis_stops_when_iteration_makes_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-failed iteration (every call failed) must terminate the
    loop instead of spinning to the iteration ceiling — and must not let
    a failed outbound iteration reset its budget on the next round."""
    call_count = {"n": 0}

    async def _failing_route(_inp: dict[str, Any]) -> str:
        call_count["n"] += 1
        return '{"error": "alerts store unavailable"}'

    from openexecutive.orchestrator import executive

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "create_alert", _failing_route)
    monkeypatch.setattr("openexecutive.people.store.list_people", lambda: [])

    provider_calls = {"n": 0}

    class _CountingProvider:
        async def messages_create(self, **_kwargs: Any) -> Any:
            provider_calls["n"] += 1
            # Always asks for one (failing) outbound call and never wraps
            # up — left to itself this would run the full iteration ceiling.
            return _Resp(
                [_ToolUse("create_alert", {"headline": "x"}, "d")],
                "tool_use",
            )

    from openexecutive import providers

    monkeypatch.setattr(providers, "get_provider", lambda _model: _CountingProvider())

    _narrative, tool_calls = await er._executive_synthesis_loop([_finding()])

    # Loop stopped after the first no-progress iteration: one provider
    # call, one (failed) outbound attempt — not the full 3-iteration ceiling.
    assert provider_calls["n"] == 1
    assert call_count["n"] == 1
    assert all(not t["ok"] for t in tool_calls)


@pytest.mark.asyncio
async def test_synthesis_skips_a_tool_it_did_not_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool the pass withholds (here the raw `send_slack_dm`, which has no
    roster check; likewise the watchlist writes, `ack_alert`, `run_workflow`)
    must not run when the model emits it anyway — e.g. from injected text in
    a finding. The handler map is built from the offered list, so it is
    skipped as unknown."""
    ran: list[str] = []

    async def _dm(_inp: dict[str, Any]) -> str:
        ran.append("send_slack_dm")
        return '{"status": "sent"}'

    async def _watch(_inp: dict[str, Any]) -> str:
        ran.append("add_watchlist_entry")
        return '{"status": "added"}'

    from openexecutive.orchestrator import executive

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "send_slack_dm", _dm)
    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "add_watchlist_entry", _watch)
    monkeypatch.setattr("openexecutive.people.store.list_people", lambda: [])

    seq = _SequenceProvider([
        _Resp(
            [
                _ToolUse("send_slack_dm", {"user_id": "U0XXXX", "text": "hi"}, "a"),
                _ToolUse("add_watchlist_entry", {"slug": "x"}, "b"),
            ],
            "tool_use",
        ),
        _Resp([_Text("done")], "end_turn"),
    ])
    from openexecutive import providers

    monkeypatch.setattr(providers, "get_provider", lambda _model: seq)

    _narrative, tool_calls = await er._executive_synthesis_loop([_finding()])

    assert ran == []
    assert {t["tool"]: t["result_preview"] for t in tool_calls} == {
        "send_slack_dm": "unknown tool — skipped",
        "add_watchlist_entry": "unknown tool — skipped",
    }


def _finding(title: str = "Competitor cut prices") -> er.ResearchFinding:
    return er.ResearchFinding(
        title=title,
        summary="A rival dropped prices 15% this week.",
        severity_hint="high",
        suggested_audience="head of sales",
        confidence="high",
        relevant_urls=["https://example.com/news"],
    )
