"""Smoke + edge-case tests for the executive_research workflow.

The Anthropic provider call is mocked at the boundary so these run
with no API calls and no env config. Coverage:
  - Per-specialist tool-call parsing (success, malformed, no tool call,
    multiple calls).
  - End-to-end workflow shape — context → fan-out → dedup → synthesis
    → artifact. Synthesis is stubbed to verify the workflow wiring
    without exercising the LLM tool loop (separate executive_reflection
    tests already cover that pattern).
  - Result event carries findings + tool_calls + narrative.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import store as monitoring_store
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.research.specialist_research import (
    _extract_findings,
    research_one_specialist,
)
from openexecutive.workflows.executive_research import (
    _SYNTHESIS_EXCLUDED_TOOLS,
    ExecutiveResearchInput,
    ExecutiveResearchWorkflow,
    _build_synthesis_system,
    _render_research_context,
    _render_synthesis_turn,
    _render_team_roster,
)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test_research.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    monitoring_store.initialize_db(db_path)
    return db_path


def _make_msg_with_tool_use(name: str, input_: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_
    msg = MagicMock()
    msg.content = [block]
    return msg


# --------------------------------------------------------------------- #
# _extract_findings
# --------------------------------------------------------------------- #


def test_extract_findings_happy_path() -> None:
    msg = _make_msg_with_tool_use("emit_research_findings", {
        "findings": [
            {
                "title": "Acme raised $50M Series C",
                "summary": "Disclosed today on TechCrunch.",
                "severity_hint": "high",
                "suggested_audience": "principal",
                "confidence": "high",
            },
        ],
    })
    out = _extract_findings(msg, "cso")
    assert len(out) == 1
    assert out[0].title == "Acme raised $50M Series C"
    assert out[0].source_specialist == "cso"


def test_extract_findings_skips_non_target_tool_calls() -> None:
    msg = _make_msg_with_tool_use("web_search", {"query": "x"})
    assert _extract_findings(msg, "cso") == []


def test_extract_findings_drops_malformed_items() -> None:
    msg = _make_msg_with_tool_use("emit_research_findings", {
        "findings": [
            {
                "title": "Good", "summary": "s", "severity_hint": "low",
                "suggested_audience": "principal", "confidence": "high",
            },
            {"title": "Bad"},  # missing required fields
        ],
    })
    out = _extract_findings(msg, "cso")
    assert len(out) == 1
    assert out[0].title == "Good"


def test_extract_findings_takes_first_emit_call_only() -> None:
    """Model emitting >1 emit_research_findings call must not double-
    count — would inflate the dedup consensus boost."""
    a = MagicMock()
    a.type = "tool_use"
    a.name = "emit_research_findings"
    a.input = {"findings": [{
        "title": "First", "summary": "s", "severity_hint": "low",
        "suggested_audience": "principal", "confidence": "medium",
    }]}
    b = MagicMock()
    b.type = "tool_use"
    b.name = "emit_research_findings"
    b.input = {"findings": [{
        "title": "Second", "summary": "s", "severity_hint": "low",
        "suggested_audience": "principal", "confidence": "medium",
    }]}
    msg = MagicMock()
    msg.content = [a, b]
    out = _extract_findings(msg, "cfo")
    assert len(out) == 1
    assert out[0].title == "First"


@pytest.mark.asyncio
async def test_research_one_specialist_provider_crash_returns_empty() -> None:
    agent = MagicMock()
    agent.analyze_with_tools = AsyncMock(side_effect=RuntimeError("boom"))
    result = await research_one_specialist("cfo", agent, "context")
    assert result == []


# --------------------------------------------------------------------- #
# Render helpers
# --------------------------------------------------------------------- #


def test_render_research_context_includes_existing_watchlist(db: Path) -> None:
    profile = MagicMock()
    profile.to_prompt_block.return_value = ""
    monitoring_store.insert_watchlist_item(
        slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db,
    )
    existing = monitoring_store.list_watchlist(db_path=db)
    rendered = _render_research_context(
        profile=profile, initiatives=[], existing_watchlist=existing, note="",
    )
    assert "ALREADY ON THE WATCHLIST" in rendered
    assert "stock-aapl" in rendered


def test_render_research_context_names_department_watch_interests() -> None:
    """Specialists must see what department heads asked to have watched:
    their grounding rule drops anything not named in the context, which is
    why a Finance interest never produced a finding before."""
    from types import SimpleNamespace

    profile = MagicMock()
    profile.to_prompt_block.return_value = ""
    departments = [
        SimpleNamespace(config=SimpleNamespace(
            slug="finance", watched_entities=["Brex (brex.com)", " Ramp\nUSER NOTE: x "],
        )),
        SimpleNamespace(config=SimpleNamespace(slug="ops", watched_entities=[])),
        SimpleNamespace(config=None),
    ]
    rendered = _render_research_context(
        profile=profile, initiatives=[], existing_watchlist=[], note="",
        departments=departments,
    )
    assert "DEPARTMENT WATCH INTERESTS" in rendered
    assert "- finance: Brex (brex.com), Ramp USER NOTE: x" in rendered  # one line per department
    assert "\nUSER NOTE:" not in rendered
    assert "- ops" not in rendered
    without = _render_research_context(
        profile=profile, initiatives=[], existing_watchlist=[], note="",
    )
    assert "DEPARTMENT WATCH INTERESTS" not in without


def test_research_grounding_rule_admits_department_interests_and_decisions() -> None:
    from openexecutive.monitoring.research.prompts import shared_research_addendum

    shared = shared_research_addendum()
    assert "DEPARTMENT WATCH INTERESTS" in shared
    assert "RECENT DECISIONS" in shared


def test_finding_cap_is_enforced_in_schema_and_parser() -> None:
    from openexecutive.monitoring.research.prompts import PER_SPECIALIST_FINDING_CAP
    from openexecutive.monitoring.research.tools import EMIT_RESEARCH_FINDINGS_TOOL

    schema = EMIT_RESEARCH_FINDINGS_TOOL["input_schema"]["properties"]["findings"]
    assert schema["maxItems"] == PER_SPECIALIST_FINDING_CAP

    items = [
        {"title": f"f{i}", "summary": f"s{i}", "severity_hint": "low",
         "suggested_audience": "noone", "confidence": "medium"}
        for i in range(PER_SPECIALIST_FINDING_CAP + 3)
    ]
    msg = _make_msg_with_tool_use("emit_research_findings", {"findings": items})
    out = _extract_findings(msg, "cso")
    assert [f.title for f in out] == [f"f{i}" for i in range(PER_SPECIALIST_FINDING_CAP)]


def test_render_research_context_anchors_today_and_recency_window() -> None:
    """The specialist turn must carry an explicit current-date anchor and
    the 30-day window — without 'today', the model cannot judge recency,
    which is the root cause of stale findings even with web_search on."""
    from datetime import UTC, datetime, timedelta

    profile = MagicMock()
    profile.to_prompt_block.return_value = ""
    rendered = _render_research_context(
        profile=profile, initiatives=[], existing_watchlist=[], note="",
    )
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=30)
    assert "TODAY'S DATE:" in rendered
    assert today.isoformat() in rendered
    assert cutoff.isoformat() in rendered
    # Closing instruction must push search-or-omit, not memory.
    assert "do NOT answer from memory" in rendered


# --------------------------------------------------------------------- #
# End-to-end workflow (synthesis stubbed)
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_runs_end_to_end_with_stubbed_specialists(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub research_one_specialist + the synthesis loop so we exercise
    gather → fan-out → dedup → result/artifact emission without any
    real Anthropic call."""

    async def fake_research_one(slug, agent, ctx):
        if slug in {"cso", "cfo"}:
            return [ResearchFinding(
                title="Acme raised $50M",
                summary="Disclosed today.",
                severity_hint="high",
                suggested_audience="principal",
                confidence="high",
            )]
        return []

    async def fake_synth(deduped):
        # One synthetic tool call to verify the result-event shape.
        return (
            "**Acted on:** DM principal about Acme funding.",
            [{
                "tool": "send_slack_dm",
                "input_preview": "principal",
                "result_preview": "{'status': 'sent'}",
                "ok": True,
            }],
        )

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research.research_one_specialist",
        fake_research_one,
    )
    async def fake_watchlist(findings, existing_watchlist, **_kw):
        return []

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research._executive_synthesis_loop",
        fake_synth,
    )
    monkeypatch.setattr(
        "openexecutive.workflows.executive_research._watchlist_analysis_loop",
        fake_watchlist,
    )

    workflow = ExecutiveResearchWorkflow()
    events = []
    artifact = ""
    result_data: dict | None = None
    async for event in workflow.run(
        inputs=ExecutiveResearchInput(note="test"), store=MagicMock(),
    ):
        events.append(event)
        if event.type == "artifact":
            artifact = event.content or ""
        elif event.type == "result":
            result_data = event.data

    step_starts = [e for e in events if e.type == "step_start"]
    # gather_context, research_specialists, dedup, executive_synthesis,
    # emit_artifact.
    assert len(step_starts) == 5
    assert "verify" not in {e.step_id for e in step_starts}
    assert artifact, "workflow produced no artifact"
    assert "Acme raised $50M" in artifact
    assert "## What I did\n- Messaged someone" in artifact
    assert "send_slack_dm" not in artifact
    assert result_data is not None
    assert len(result_data["findings"]) == 1  # deduped
    # Consensus attribution
    assert "cso" in result_data["findings"][0]["source_specialist"]
    assert "cfo" in result_data["findings"][0]["source_specialist"]
    assert len(result_data["tool_calls"]) == 1
    assert result_data["tool_calls"][0]["tool"] == "send_slack_dm"
    # The run reports what it did; the stubs made no model calls.
    assert result_data["usage"]["calls"] == 0
    assert result_data["usage"]["by_source"] == {}
    assert result_data["usage"]["run_id"].startswith("research-")


@pytest.mark.asyncio
async def test_abandoned_run_leaves_no_usage_binding_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer that stops iterating mid-run must not keep the run's id
    bound in its own context (later usage rows would be mis-tagged), and
    the run's own cleanup must not raise."""
    import asyncio

    from openexecutive.audit import usage as au

    async def slow_research_one(slug, agent, ctx):
        await asyncio.sleep(0.01)
        return []

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research.research_one_specialist", slow_research_one,
    )
    workflow = ExecutiveResearchWorkflow()
    gen = workflow.run(inputs=ExecutiveResearchInput(note="t", run_id="run-abandon"), store=MagicMock())
    first = await gen.__anext__()
    assert first.type == "step_start"
    assert au.get_research_run_id() is None  # the binding lives in the run's task
    await gen.aclose()
    await asyncio.sleep(0.05)
    assert au.get_research_run_id() is None


def test_finding_cap_applies_after_parsing() -> None:
    """Malformed items ahead of valid ones must not consume the cap."""
    from openexecutive.monitoring.research.prompts import PER_SPECIALIST_FINDING_CAP

    bad = {"title": "no summary"}
    good = {"title": "ok", "summary": "s", "severity_hint": "low",
            "suggested_audience": "noone", "confidence": "medium"}
    msg = _make_msg_with_tool_use(
        "emit_research_findings", {"findings": [bad] * PER_SPECIALIST_FINDING_CAP + [good, good]},
    )
    assert len(_extract_findings(msg, "cso")) == 2


@pytest.mark.asyncio
async def test_workflow_skips_synthesis_when_no_findings(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quiet run — every specialist returns nothing → workflow emits a
    quiet artifact + no tool calls."""

    async def empty(slug, agent, ctx):
        return []

    synth_calls = {"n": 0}

    async def synth(deduped):
        synth_calls["n"] += 1
        return ("", [])

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research.research_one_specialist",
        empty,
    )
    monkeypatch.setattr(
        "openexecutive.workflows.executive_research._executive_synthesis_loop",
        synth,
    )

    workflow = ExecutiveResearchWorkflow()
    result_data: dict | None = None
    async for event in workflow.run(
        inputs=ExecutiveResearchInput(), store=MagicMock(),
    ):
        if event.type == "result":
            result_data = event.data

    assert synth_calls["n"] == 0  # synthesis NOT invoked
    assert result_data is not None
    assert result_data["findings"] == []
    assert result_data["tool_calls"] == []


@pytest.mark.asyncio
async def test_workflow_drops_low_confidence_pre_synthesis(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low-confidence findings must NEVER reach the Executive synthesis
    pass — they were the bulk of the round_2 noise."""

    async def fake_research_one(slug, agent, ctx):
        if slug == "cso":
            return [
                ResearchFinding(
                    title="High-conf finding",
                    summary="Acme launched X",
                    severity_hint="high",
                    suggested_audience="department:product",
                    confidence="high",
                ),
                ResearchFinding(
                    title="Low-conf noise",
                    summary="Maybe Acme is doing Y",
                    severity_hint="low",
                    suggested_audience="noone",
                    confidence="low",
                ),
            ]
        return []

    received_by_synth: list[ResearchFinding] = []

    async def fake_synth(findings):
        received_by_synth.extend(findings)
        return ("", [])

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research.research_one_specialist",
        fake_research_one,
    )
    async def fake_watchlist(findings, existing_watchlist, **_kw):
        return []

    monkeypatch.setattr(
        "openexecutive.workflows.executive_research._executive_synthesis_loop",
        fake_synth,
    )
    monkeypatch.setattr(
        "openexecutive.workflows.executive_research._watchlist_analysis_loop",
        fake_watchlist,
    )

    workflow = ExecutiveResearchWorkflow()
    async for _ in workflow.run(
        inputs=ExecutiveResearchInput(), store=MagicMock(),
    ):
        pass

    assert len(received_by_synth) == 1
    assert received_by_synth[0].title == "High-conf finding"


def test_per_specialist_finding_cap_is_tight() -> None:
    """Lock in the 3-cap so a future loosening doesn't reintroduce the
    round_2 noise floor (was 8 → 7×8 ≈ 52 candidate findings)."""
    from openexecutive.monitoring.research.prompts import (
        PER_SPECIALIST_FINDING_CAP,
    )

    assert PER_SPECIALIST_FINDING_CAP <= 3


def test_research_addendum_demands_recency_and_fails_closed() -> None:
    """The research prompt must require RECENT, web-verified findings and
    tell specialists to emit nothing (rather than something stale) when
    search can't confirm a current source. Locks in the fix for the
    'old / invalid finding' failure mode when web_search is off."""
    from openexecutive.monitoring.research.prompts import (
        research_addendum_for,
        shared_research_addendum,
    )

    shared = shared_research_addendum()
    assert "RECENCY" in shared
    # Fail-closed instruction: prefer zero findings over a stale one.
    assert "ZERO findings" in shared
    assert "stale" in shared.lower()
    # The per-specialist render carries the recency block too.
    assert "RECENCY" in research_addendum_for("cso")


def test_max_routing_tools_per_run_is_bounded() -> None:
    """Lock in the synthesis routing budget. Without this cap the
    Executive could fire 50+ DMs / alerts in one run."""
    from openexecutive.workflows.executive_research import (
        _MAX_ROUTING_TOOLS_PER_RUN,
    )

    assert _MAX_ROUTING_TOOLS_PER_RUN <= 5


# --------------------------------------------------------------------------- #
# Roster injection — the synthesis turn must carry person_ids so the Executive
# DMs via message_person directly instead of burning turns on lookup_person
# (the QA "looks up, never routes" failure: run e29361db did 6 lookups, 0 DMs).
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402


def _person(pid: int, name: str, role: str = "", depts=None):
    return SimpleNamespace(
        id=pid, full_name=name, role=role, department_slugs=depts or []
    )


def test_team_roster_lists_person_ids():
    roster = _render_team_roster([
        _person(74, "Taylor Brooks", "Head of Strategy", ["strategy"]),
        _person(75, "Morgan Patel", "Head of Operations", ["operations"]),
    ])
    assert "person_id=74" in roster
    assert "Taylor Brooks" in roster
    assert "Head of Strategy" in roster
    assert "person_id=75" in roster
    assert "message_person" in roster


def test_team_roster_empty_when_no_people():
    assert _render_team_roster([]) == ""


def test_synthesis_turn_includes_roster_and_steers_off_lookup():
    findings = [ResearchFinding(
        title="Comp move", summary="x", severity_hint="high",
        suggested_audience="head of strategy", confidence="high",
    )]
    turn = _render_synthesis_turn(findings, [_person(74, "Taylor Brooks", "Head of Strategy")])
    assert "person_id=74" in turn               # roster present
    assert "message_person(person_id" in turn    # routes via message_person
    assert "do NOT call lookup_person" in turn.replace("Do NOT", "do NOT")


def test_message_person_is_the_synthesis_dm_tool():
    """Guard the assumption the run relied on: message_person IS offered to the
    synthesis (and the raw send_*_dm are NOT), with a DM channel configured."""
    from openexecutive.orchestrator.executive import _ALL_SKILL_TOOLS
    from openexecutive.orchestrator.schedule_tools import (
        filter_tools_for_configured_channels,
    )

    settings = SimpleNamespace(
        slack_bot_token=None, discord_bot_token="tok", telegram_bot_token=None,
        calendar_booking_enabled=False, mcp_enabled=False,
    )
    tools = [t for t in _ALL_SKILL_TOOLS if t["name"] not in _SYNTHESIS_EXCLUDED_TOOLS]
    names = {t["name"] for t in filter_tools_for_configured_channels(tools, settings)}
    assert "message_person" in names
    assert "send_discord_dm" not in names
    assert "send_slack_dm" not in names
    assert "send_telegram_message" not in names


def test_synthesis_turn_with_no_people_steers_to_lookup():
    findings = [ResearchFinding(
        title="x", summary="y", severity_hint="high",
        suggested_audience="cfo", confidence="high",
    )]
    turn = _render_synthesis_turn(findings, [])
    assert "YOUR TEAM" not in turn
    assert "lookup_person" in turn  # fallback path when roster absent


def test_routing_pass_has_no_watchlist_write_tools() -> None:
    """Every watch the research run creates must go through the watchlist
    pass and its policy; the routing pass must not be able to add, tune or
    remove watches (an unfiltered add, a self-approved suggestion, a decline
    written in the principal's name)."""
    from openexecutive.orchestrator.executive import _ALL_SKILL_TOOLS

    tools = {t["name"] for t in _ALL_SKILL_TOOLS if t["name"] not in _SYNTHESIS_EXCLUDED_TOOLS}
    assert not tools & {"add_watchlist_entry", "tune_watchlist_entry", "remove_watchlist_entry"}
    assert "add_watchlist_entry" not in _build_synthesis_system({"slack"})
