"""Solo mode beyond the chat turn: the unattended passes (reflection,
research), alert triage, the briefs and their caches, meeting autonomy,
follow-ups to the principal, the eval harness and the solo fixture.

Solo is one person using Open Executive for themselves. Every team path here
must come out exactly as before; the solo path must never address a team.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from openexecutive.alerts import store as alert_store
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.orchestrator.schedule_tools import (
    SOLO_WITHHELD_TOOLS,
    current_session,
    set_session,
)
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

REPO_ROOT = Path(__file__).resolve().parents[4]
SOLO_FIXTURE = REPO_ROOT / "fixtures" / "companies" / "solo_studio"
SCENARIOS_DIR = (
    Path(__file__).resolve().parents[2] / "openexecutive" / "evals" / "_scenarios"
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "solo_surfaces.db"
    for mod in (episodic, dept_store, people_store, alert_store):
        monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    # Modules that bound log_event at import still reach the default logger;
    # point it at the temp DB so nothing lands in ./episodic_memory.db.
    set_audit_logger(AuditLogger(db_path=db))
    dept_registry.invalidate()
    people_registry.invalidate()
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)
    yield db
    set_audit_logger(None)
    dept_registry.invalidate()
    people_registry.invalidate()


def _solo() -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))


def _principal(**kw: Any) -> int:
    return people_store.upsert_person(
        full_name="Maya Lindqvist", role="Principal Designer", is_principal=True,
        email="maya@example.com", telegram_chat_id="555", **kw,
    )


# --------------------------------------------------------------------------- #
# Reflection and research: filtered toolkit, solo framing
# --------------------------------------------------------------------------- #


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUse:
    type = "tool_use"

    def __init__(self, name: str, input_: dict[str, Any], id_: str = "tu_1") -> None:
        self.name = name
        self.input = input_
        self.id = id_


class _Resp:
    usage = None

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _CapturingProvider:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _run_reflection(provider: _CapturingProvider, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from openexecutive import providers
    from openexecutive.workflows.executive_reflection import (
        ExecutiveReflectionInput,
        ExecutiveReflectionWorkflow,
    )

    monkeypatch.setattr(providers, "get_provider", lambda _model: provider)

    async def _go() -> list[Any]:
        return [
            e async for e in ExecutiveReflectionWorkflow().run(
                inputs=ExecutiveReflectionInput(), store=None  # type: ignore[arg-type]
            )
        ]

    return asyncio.run(_go())


def _names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["name"] for t in tools}


def test_reflection_in_solo_offers_no_team_tools_and_frames_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _solo()
    pid = _principal()
    people_store.upsert_person(full_name="Client Contact", email="c@example.com", slack_user_id="U9")
    provider = _CapturingProvider([_Resp([_Text("**Quiet:** nothing.")], "end_turn")])
    events = _run_reflection(provider, monkeypatch)
    assert "error" not in [e.type for e in events]

    call = provider.calls[0]
    assert not SOLO_WITHHELD_TOOLS & _names(call["tools"])
    system = call["system"]
    assert "the principal's Executive" in system
    assert "send_department_message" not in system
    assert "send_company_broadcast" not in system
    assert "Choosing Who to Tell" not in system
    turn = call["messages"][0]["content"]
    assert "YOUR TEAM" not in turn
    assert f"YOUR PRINCIPAL: person_id={pid} — Maya Lindqvist" in turn
    assert "Client Contact" not in turn


def test_solo_passes_name_the_principal_by_the_shared_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reflection's principal line and research's principal roster use
    people.store.find_principal_person, like principal_only_handlers."""
    from openexecutive.workflows.executive_reflection import (
        _find_principal,
        _render_principal_line,
    )
    from openexecutive.workflows.executive_research import _principal_only

    _solo()
    pid = _principal()
    second = people_store.upsert_person(
        full_name="Aaron Second", is_principal=True, email="a@example.com"
    )
    people = people_store.list_people()
    assert _find_principal().id == pid
    assert f"person_id={pid}" in _render_principal_line(_find_principal())
    assert [p.id for p in _principal_only(people)] == [pid]

    other = people_store.get_person(second)
    monkeypatch.setattr(
        "openexecutive.people.store.find_principal_person", lambda db_path=None: other
    )
    assert [p.id for p in _principal_only(people)] == [second]
    assert _find_principal().id == second


def test_reflection_in_team_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator.schedule_tools import configured_integrations
    from openexecutive.workflows.executive_reflection import _build_reflection_system

    _principal()
    provider = _CapturingProvider([_Resp([_Text("**Quiet:** nothing.")], "end_turn")])
    _run_reflection(provider, monkeypatch)
    call = provider.calls[0]
    from openexecutive.config import get_settings

    assert call["system"] == _build_reflection_system(
        configured_integrations(get_settings()), True
    )
    assert "YOUR TEAM" in call["messages"][0]["content"]


def test_reflection_session_override_reaches_the_toolkit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eval binds a solo session; the reflection honours it while the
    workspace itself stays team."""
    _principal()
    provider = _CapturingProvider([_Resp([_Text("ok")], "end_turn")])
    with set_session(Session(workspace_mode="solo")):
        _run_reflection(provider, monkeypatch)
    assert not SOLO_WITHHELD_TOOLS & _names(provider.calls[0]["tools"])
    assert ws.get_workspace().mode == "team"


def test_reflection_refuses_a_withheld_tool_in_solo(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive

    _solo()
    _principal()
    ran: list[str] = []

    async def _broadcast(_payload: dict[str, Any]) -> str:
        ran.append("x")
        return "{}"

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "send_company_broadcast", _broadcast)
    provider = _CapturingProvider([
        _Resp([_ToolUse("send_company_broadcast", {"text": "hi"})], "tool_use"),
        _Resp([_Text("done")], "end_turn"),
    ])
    events = _run_reflection(provider, monkeypatch)
    assert ran == []
    artifact = next(e for e in events if e.type == "artifact").content
    assert "unknown tool — skipped" in artifact


def test_research_synthesis_in_solo_routes_only_to_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive import providers
    from openexecutive.monitoring.research.models import ResearchFinding
    from openexecutive.workflows import executive_research

    _solo()
    pid = _principal()
    people_store.upsert_person(full_name="Client Contact", email="c@example.com")
    provider = _CapturingProvider([_Resp([_Text("**Quiet:** 1 reviewed.")], "end_turn")])
    monkeypatch.setattr(providers, "get_provider", lambda _model: provider)
    monkeypatch.setattr(executive_research, "log_model_usage", lambda *a, **k: None)

    finding = ResearchFinding(
        title="Rival studio cut prices", summary="Announced today.",
        severity_hint="high", suggested_audience="principal", confidence="high",
    )
    asyncio.run(executive_research._executive_synthesis_loop([finding]))

    call = provider.calls[0]
    assert not SOLO_WITHHELD_TOOLS & _names(call["tools"])
    assert "send_department_message" not in call["system"]
    assert "the only person you route to" in call["system"]
    turn = call["messages"][0]["content"]
    assert "YOUR PRINCIPAL (DM with message_person" in turn
    assert f"person_id={pid}" in turn
    assert "Client Contact" not in turn
    assert "YOUR TEAM" not in turn


def test_research_synthesis_system_is_unchanged_in_team() -> None:
    from openexecutive.workflows.executive_research import _build_synthesis_system

    team = _build_synthesis_system({"slack"}, True)
    assert team == _build_synthesis_system({"slack"}, True, mode="team")
    assert "send_department_message" in team
    assert "send_department_message" not in _build_synthesis_system({"slack"}, True, mode="solo")


def test_principal_only_handlers_refuse_anyone_but_the_principal() -> None:
    from openexecutive.orchestrator.schedule_tools import principal_only_handlers

    pid = _principal()
    contact = people_store.upsert_person(full_name="Client Contact", email="c@example.com")
    sent: list[int] = []

    async def _send(tool_input: dict[str, Any]) -> str:
        sent.append(int(tool_input["person_id"]))
        return json.dumps({"status": "sent"})

    async def _other(_tool_input: dict[str, Any]) -> str:
        return "{}"

    original = {"message_person": _send, "create_alert": _other}
    wrapped = principal_only_handlers(original)
    assert wrapped["create_alert"] is _other
    assert original["message_person"] is _send  # the input map is not mutated

    refused = json.loads(asyncio.run(wrapped["message_person"]({"person_id": contact, "text": "hi"})))
    assert "only your principal" in refused["error"]
    bad = json.loads(asyncio.run(wrapped["message_person"]({"person_id": "nope", "text": "hi"})))
    assert "error" in bad
    ok = json.loads(asyncio.run(wrapped["message_person"]({"person_id": pid, "text": "hi"})))
    assert ok == {"status": "sent"}
    assert sent == [pid]


def test_solo_reflection_cannot_message_a_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive

    _solo()
    _principal()
    contact = people_store.upsert_person(full_name="Client Contact", email="c@example.com")
    sent: list[int] = []

    async def _send(tool_input: dict[str, Any]) -> str:
        sent.append(int(tool_input["person_id"]))
        return json.dumps({"status": "sent"})

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "message_person", _send)
    # A DM channel is configured, so message_person is offered — and still
    # reaches nobody but the principal.
    _configure(monkeypatch, "telegram")
    provider = _CapturingProvider([
        _Resp([_ToolUse("message_person", {"person_id": contact, "text": "hi"})], "tool_use"),
        _Resp([_Text("done")], "end_turn"),
    ])
    events = _run_reflection(provider, monkeypatch)
    assert "message_person" in _names(provider.calls[0]["tools"])
    assert sent == []
    assert "only your principal" in next(e for e in events if e.type == "artifact").content


def _configure(monkeypatch: pytest.MonkeyPatch, *integrations: str) -> None:
    """Pretend these channel integrations are configured (tokens set, and
    'calendar' = booking enabled with a running gateway)."""
    monkeypatch.setattr(
        "openexecutive.orchestrator.schedule_tools.configured_integrations",
        lambda _settings: set(integrations),
    )


_UNATTENDED_SOLO_ONLY = ("create_calendar_event", "create_instant_meeting", "run_workflow")


def _stub_handlers(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> list[str]:
    from openexecutive.orchestrator import executive

    ran: list[str] = []
    for name in names:
        async def _h(_payload: dict[str, Any], _name: str = name) -> str:
            ran.append(_name)
            return json.dumps({"status": "ok"})

        monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, name, _h)
    return ran


@pytest.mark.parametrize("mode", ["team", "solo"])
def test_reflection_runs_only_tools_it_offered(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool the pass does not offer never runs, in either mode. The raw
    DM tools (send_slack_dm has no roster check), ack_alert and
    close_open_loop used to run whenever the model emitted them."""
    if mode == "solo":
        _solo()
    _principal()
    _configure(monkeypatch, "slack", "telegram", "discord")
    withheld = ("send_slack_dm", "send_telegram_message", "ack_alert", "close_open_loop")
    ran = _stub_handlers(monkeypatch, withheld)
    provider = _CapturingProvider([
        _Resp(
            [_ToolUse(n, {"user_id": "U0XXXX", "text": "hi"}, f"tu-{n}") for n in withheld],
            "tool_use",
        ),
        _Resp([_Text("done")], "end_turn"),
    ])
    events = _run_reflection(provider, monkeypatch)
    offered = _names(provider.calls[0]["tools"])
    assert not set(withheld) & offered
    assert ran == []
    artifact = next(e for e in events if e.type == "artifact").content
    assert artifact.count("unknown tool — skipped") == len(withheld)


def test_solo_reflection_withholds_booking_and_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _principal()
    _configure(monkeypatch, "telegram", "calendar")
    ran = _stub_handlers(monkeypatch, _UNATTENDED_SOLO_ONLY)

    # Team: meeting booking is offered (and so runnable) when configured.
    team = _CapturingProvider([_Resp([_Text("ok")], "end_turn")])
    _run_reflection(team, monkeypatch)
    assert {"create_calendar_event", "create_instant_meeting"} <= _names(team.calls[0]["tools"])

    _solo()
    provider = _CapturingProvider([
        _Resp(
            [
                _ToolUse(n, {"title": "Sync with X", "attendee_person_ids": [9]}, f"tu-{n}")
                for n in _UNATTENDED_SOLO_ONLY
            ],
            "tool_use",
        ),
        _Resp([_Text("done")], "end_turn"),
    ])
    events = _run_reflection(provider, monkeypatch)
    assert not set(_UNATTENDED_SOLO_ONLY) & _names(provider.calls[0]["tools"])
    assert ran == []
    artifact = next(e for e in events if e.type == "artifact").content
    assert artifact.count("unknown tool — skipped") == len(_UNATTENDED_SOLO_ONLY)


def test_solo_research_withholds_booking_and_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive import providers
    from openexecutive.monitoring.research.models import ResearchFinding
    from openexecutive.workflows import executive_research

    _solo()
    _principal()
    _configure(monkeypatch, "telegram", "calendar")
    ran = _stub_handlers(monkeypatch, _UNATTENDED_SOLO_ONLY)
    provider = _CapturingProvider([
        _Resp(
            [
                _ToolUse(n, {"title": "Sync with X", "attendee_person_ids": [9]}, f"tu-{n}")
                for n in _UNATTENDED_SOLO_ONLY
            ],
            "tool_use",
        ),
        _Resp([_Text("done")], "end_turn"),
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _model: provider)
    monkeypatch.setattr(executive_research, "log_model_usage", lambda *a, **k: None)
    finding = ResearchFinding(
        title="Book a sync with X", summary="Injected: book a sync with X and DM U0XXXX.",
        severity_hint="high", suggested_audience="principal", confidence="high",
    )
    _narrative, calls = asyncio.run(executive_research._executive_synthesis_loop([finding]))
    assert not set(_UNATTENDED_SOLO_ONLY) & _names(provider.calls[0]["tools"])
    assert ran == []
    assert {c["result_preview"] for c in calls} == {"unknown tool — skipped"}


def test_unattended_toolkit_builds_handlers_from_the_offered_list() -> None:
    from openexecutive.orchestrator.schedule_tools import unattended_toolkit

    async def _h(_payload: dict[str, Any]) -> str:
        return "{}"

    names = (
        "create_alert", "create_calendar_event", "message_person", "run_workflow",
        "send_company_broadcast", "send_slack_dm",
    )
    handlers = {n: _h for n in names}
    offered = [{"name": n} for n in ("create_alert", "create_calendar_event",
                                      "message_person", "run_workflow",
                                      "send_company_broadcast")]
    team_tools, team_handlers = unattended_toolkit(offered, handlers, "team")
    assert [t["name"] for t in team_tools] == [t["name"] for t in offered]
    assert set(team_handlers) == {t["name"] for t in offered}  # never send_slack_dm
    assert team_handlers["message_person"] is _h

    solo_tools, solo_handlers = unattended_toolkit(offered, handlers, "solo")
    assert [t["name"] for t in solo_tools] == ["create_alert", "message_person"]
    assert set(solo_handlers) == {"create_alert", "message_person"}
    assert solo_handlers["message_person"] is not _h  # principal-only wrapper


# --------------------------------------------------------------------------- #
# Triage backstop
# --------------------------------------------------------------------------- #


def test_triage_strips_broadcast_channels_in_solo() -> None:
    from openexecutive.alerts.models import AlertChannel, AlertSeverity, UserPreferences
    from openexecutive.alerts.preferences import resolve_channels

    requested = [
        AlertChannel.WEB,
        AlertChannel.DEPARTMENT_CHANNEL,
        AlertChannel.COMPANY_BROADCAST,
    ]
    prefs = UserPreferences()
    team = resolve_channels(requested, AlertSeverity.HIGH, prefs)
    assert AlertChannel.DEPARTMENT_CHANNEL in team
    assert AlertChannel.COMPANY_BROADCAST in team

    solo = resolve_channels(requested, AlertSeverity.HIGH, prefs, workspace_mode="solo")
    assert AlertChannel.DEPARTMENT_CHANNEL not in solo
    assert AlertChannel.COMPANY_BROADCAST not in solo
    assert AlertChannel.WEB in solo
    assert AlertChannel.PERSISTED in solo

    # Unset → the workspace setting decides.
    _solo()
    from_workspace = resolve_channels(requested, AlertSeverity.HIGH, prefs)
    assert AlertChannel.COMPANY_BROADCAST not in from_workspace


# --------------------------------------------------------------------------- #
# Briefs: context, prompts, caches
# --------------------------------------------------------------------------- #


def _today_data() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "departments": [
            {"title": "Finance", "slug": "finance", "at_risk_count": 1, "off_track_count": 0,
             "awaiting_count": 1, "authority_level": "propose_only"},
        ],
        "people": [
            {"full_name": "Maya Lindqvist", "id": 1, "role": "Principal Designer", "awaiting_count": 2,
             "soonest_sla_at": "soon"},
        ],
        "proposals": [
            {"alert_id": 7, "headline": "Approve the Northwind hourly exception",
             "created_at": (now - timedelta(hours=2)).isoformat()},
        ],
    }


def test_brief_context_in_solo_has_no_department_or_people_block() -> None:
    from openexecutive.briefing.narrative import render_briefing_context

    for since in (None, datetime.now(UTC) - timedelta(days=1)):
        solo = render_briefing_context(
            period_label="p", today_data=_today_data(), activity=[], since=since, mode="solo"
        )
        assert "GOALS AT RISK BY AREA:" in solo
        assert "- Finance: at_risk=1 off_track=0" in solo
        assert "DEPARTMENTS" not in solo
        assert "PEOPLE WAITING" not in solo
        assert "awaiting=" not in solo
        assert "Northwind" in solo

        team = render_briefing_context(
            period_label="p", today_data=_today_data(), activity=[], since=since
        )
        assert "DEPARTMENTS WITH RISK:" in team
        assert "PEOPLE WAITING ON YOU:" in team


def test_eod_context_in_solo_has_no_department_or_people_block() -> None:
    from openexecutive.workflows.end_of_day_digest import _render_eod_context

    since = datetime.now(UTC) - timedelta(days=1)
    solo = _render_eod_context(
        period_label="p", today_data=_today_data(), activity=[], since=since, mode="solo"
    )
    assert "GOALS AT RISK BY AREA, CARRIED FORWARD:" in solo
    assert "DEPARTMENTS" not in solo
    assert "PEOPLE STILL WAITING" not in solo
    team = _render_eod_context(period_label="p", today_data=_today_data(), activity=[], since=since)
    assert "DEPARTMENTS WITH RISK CARRIED FORWARD:" in team
    assert "PEOPLE STILL WAITING ON YOU:" in team


def test_solo_brief_prompts_drop_the_team_sections() -> None:
    from openexecutive.briefing.narrative import (
        BRIEFING_NARRATIVE_SOLO_SYSTEM,
        QUIET_PRINCIPAL,
        STANDALONE_BRIEF_SOLO_SYSTEM,
        STANDALONE_BRIEF_SYSTEM,
    )
    from openexecutive.workflows.end_of_day_digest import _EOD_DIGEST_SOLO_SYSTEM

    assert "**Waiting on**" in STANDALONE_BRIEF_SYSTEM
    for prompt in (STANDALONE_BRIEF_SOLO_SYSTEM, BRIEFING_NARRATIVE_SOLO_SYSTEM):
        assert QUIET_PRINCIPAL in prompt
        assert "**Waiting on**" not in prompt
        assert "teams are stuck" not in prompt
        assert "departments / goals" not in prompt
    for section in ("**Top call**", "**What changed**", "**Needs you**", "**Goals at risk**"):
        assert section in STANDALONE_BRIEF_SOLO_SYSTEM
    assert "**Open decisions**" in _EOD_DIGEST_SOLO_SYSTEM
    assert "name the person and what's blocking" not in _EOD_DIGEST_SOLO_SYSTEM


def _capture_synth(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _P:
        async def messages_create(self, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="brief")])

    monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: _P())
    monkeypatch.setattr(
        "openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test"
    )
    return calls


def test_synthesizer_picks_the_solo_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.briefing.narrative import (
        BRIEFING_NARRATIVE_SOLO_SYSTEM,
        BRIEFING_NARRATIVE_SYSTEM,
        STANDALONE_BRIEF_SOLO_SYSTEM,
        STANDALONE_BRIEF_SYSTEM,
        synthesize_briefing_narrative,
    )

    calls = _capture_synth(monkeypatch)
    for standalone, mode in ((True, "solo"), (False, "solo"), (True, "team"), (False, "team")):
        asyncio.run(synthesize_briefing_narrative(
            today_data=_today_data(), activity=[], period_label="p",
            standalone=standalone, mode=mode,
        ))
    assert [c["system"] for c in calls] == [
        STANDALONE_BRIEF_SOLO_SYSTEM,
        BRIEFING_NARRATIVE_SOLO_SYSTEM,
        STANDALONE_BRIEF_SYSTEM,
        BRIEFING_NARRATIVE_SYSTEM,
    ]
    assert "GOALS AT RISK BY AREA:" in calls[0]["messages"][0]["content"]
    assert "PEOPLE WAITING" not in calls[0]["messages"][0]["content"]


def test_morning_brief_and_eod_follow_the_workspace_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.api.routes import today as today_route
    from openexecutive.api.routes.today import ActivityResponse, TodayResponse
    from openexecutive.briefing import narrative as briefing_narrative
    from openexecutive.workflows.end_of_day_digest import (
        _EOD_DIGEST_SOLO_SYSTEM,
        EndOfDayDigestInput,
        EndOfDayDigestWorkflow,
    )
    from openexecutive.workflows.morning_brief import MorningBriefInput, MorningBriefWorkflow

    _solo()
    captured: dict[str, Any] = {}

    async def _synth(**kw: Any) -> str:
        captured.update(kw)
        return "BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    monkeypatch.setattr(
        today_route, "_build_today",
        lambda: TodayResponse(departments=[], people=[], proposals=[]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None: ActivityResponse(items=[])
    )

    async def _drain(wf: Any, inputs: Any) -> list[Any]:
        return [e async for e in wf.run(inputs, MagicMock())]

    asyncio.run(_drain(MorningBriefWorkflow(), MorningBriefInput(force_full=True)))
    assert captured["mode"] == "solo"

    calls = _capture_synth(monkeypatch)
    asyncio.run(_drain(EndOfDayDigestWorkflow(), EndOfDayDigestInput(force_full=True)))
    assert calls[0]["system"] == _EOD_DIGEST_SOLO_SYSTEM


def test_narrative_hash_differs_by_mode_and_team_is_unchanged() -> None:
    from openexecutive.briefing.narrative_cache import (
        NARRATIVE_PROMPT_VERSION,
        build_narrative_input_hash,
    )

    ctx = "PERIOD: 2026-09-25\n\nNEEDS YOU: x"
    team = build_narrative_input_hash(ctx, "principal")
    # The team key is exactly the pre-solo formula — no team cache invalidated.
    legacy = hashlib.sha256(json.dumps({
        "scope": "principal",
        "prompt_version": NARRATIVE_PROMPT_VERSION,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "context": ctx,
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    assert team == legacy
    assert build_narrative_input_hash(ctx, "principal", mode="team") == team
    assert build_narrative_input_hash(ctx, "principal", mode="solo") != team


def test_brief_fingerprint_team_unchanged_solo_ignores_awaiting() -> None:
    from openexecutive.briefing.brief_state import build_brief_fingerprint

    data = _today_data()
    kw: dict[str, Any] = dict(activity=[], handled=[], since=None)
    team = build_brief_fingerprint(today_data=data, **kw)
    assert build_brief_fingerprint(today_data=data, mode="team", **kw) == team
    solo = build_brief_fingerprint(today_data=data, mode="solo", **kw)
    assert solo != team
    no_awaiting = {**data, "people": []}
    assert build_brief_fingerprint(today_data=no_awaiting, mode="solo", **kw) == solo
    assert build_brief_fingerprint(today_data=no_awaiting, **kw) != team


def test_today_narrative_quiet_check_ignores_awaiting_in_solo() -> None:
    from openexecutive.api.routes.today import _nothing_needs_attention

    only_awaiting = {"departments": [], "proposals": [], "people": [{"awaiting_count": 3}]}
    assert _nothing_needs_attention(only_awaiting) is False
    assert _nothing_needs_attention(only_awaiting, "solo") is True


# --------------------------------------------------------------------------- #
# Meeting autonomy: the class API and the solo gate
# --------------------------------------------------------------------------- #


@pytest.fixture()
def decisions_client() -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes.decisions import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_meeting_class_mode_defaults_to_propose(decisions_client: Any) -> None:
    res = decisions_client.get("/decisions/classes/meeting_scheduling")
    assert res.status_code == 200
    assert res.json() == {"decision_class": "meeting_scheduling", "mode": "propose"}


def test_principal_sets_the_meeting_class_mode(
    decisions_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: events.append((event_type, kw)),
    )
    _principal()
    res = decisions_client.put(
        "/decisions/classes/meeting_scheduling", json={"mode": "auto_execute"}
    )
    assert res.status_code == 200
    assert res.json() == {"decision_class": "meeting_scheduling", "mode": "auto_execute"}
    assert decisions_client.get("/decisions/classes/meeting_scheduling").json()["mode"] == (
        "auto_execute"
    )
    assert [e[0] for e in events] == ["decision_class_mode_changed"]
    assert events[0][1]["details"]["mode"] == {"from": "propose", "to": "auto_execute"}

    # Setting the same mode again changes nothing and audits nothing.
    decisions_client.put("/decisions/classes/meeting_scheduling", json={"mode": "auto_execute"})
    assert len(events) == 1


def test_non_principal_cannot_set_the_meeting_class_mode(decisions_client: Any) -> None:
    _principal()
    people_store.upsert_person(full_name="Client Contact", email="client@example.com")
    res = decisions_client.put(
        "/decisions/classes/meeting_scheduling",
        json={"mode": "auto_execute"},
        headers={"x-caller-email": "client@example.com"},
    )
    assert res.status_code == 403
    assert decisions_client.get("/decisions/classes/meeting_scheduling").json()["mode"] == (
        "propose"
    )


def test_bad_meeting_class_mode_is_422(decisions_client: Any) -> None:
    for body in ({"mode": "sometimes"}, {}, {"mode": None}):
        res = decisions_client.put("/decisions/classes/meeting_scheduling", json=body)
        assert res.status_code == 422, body


def test_auto_execute_needs_an_active_principal(decisions_client: Any) -> None:
    """With no principal the PUT is open to anyone (first-run setup), so
    switching meetings to auto-booking is refused until one exists; propose
    stays allowed. An archived principal does not count."""
    url = "/decisions/classes/meeting_scheduling"
    res = decisions_client.put(url, json={"mode": "auto_execute"})
    assert res.status_code == 409
    assert "principal" in res.json()["detail"]
    assert decisions_client.get(url).json()["mode"] == "propose"
    assert decisions_client.put(url, json={"mode": "propose"}).status_code == 200

    pid = _principal()
    people_store.archive_person(pid)
    assert decisions_client.put(url, json={"mode": "auto_execute"}).status_code == 409

    # Back on the roster (there is no un-archive API): now it can be set.
    with sqlite3.connect(str(people_store.DB_PATH)) as conn:
        conn.execute("UPDATE people SET archived = 0 WHERE id = ?", (pid,))
    res = decisions_client.put(url, json={"mode": "auto_execute"})
    assert res.status_code == 200
    assert res.json()["mode"] == "auto_execute"


def _next_weekday_at_10() -> datetime:
    dt = (datetime.now(UTC) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt


def _calendar_settings() -> Any:
    return SimpleNamespace(
        calendar_booking_enabled=True, mcp_enabled=True,
        calendar_business_hours_start="09:00", calendar_business_hours_end="18:00",
        calendar_horizon_days=30, calendar_max_events_per_day=10,
        calendar_max_attendees=8, calendar_meet_links_enabled=True,
        calendar_instant_meeting_minutes=30, calendar_post_meeting_followup_enabled=True,
        slack_bot_token=None, discord_bot_token=None, telegram_bot_token=None,
        telegram_webhook_secret_valid=False,
    )


def _book(class_mode: str, session: Session | None = None) -> tuple[dict[str, Any], Any]:
    """Run create_calendar_event with ``session`` bound as the turn's session
    (None = no session, like an unattended caller)."""
    from openexecutive.orchestrator.calendar_tools import handle_create_calendar_event

    guest = people_store.find_person_by_email("client@example.com")
    guest_id = guest.id if guest is not None else people_store.upsert_person(
        full_name="Client Contact", email="client@example.com"
    )
    start = _next_weekday_at_10()
    gw = MagicMock()
    gw.call_tool = AsyncMock(return_value=json.dumps({"id": "evt-1"}))

    def _no_gate(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("solo must not consult a department gate")

    with (
        patch("openexecutive.config.get_settings", return_value=_calendar_settings()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=gw),
        patch("openexecutive.departments.authority.gate_action", new=_no_gate),
        patch("openexecutive.memory.decision_ledger.get_class_mode", return_value=class_mode),
        set_session(session),
    ):
        raw = asyncio.run(handle_create_calendar_event({
            "title": "Kickoff", "start": start.isoformat(),
            "end": start.replace(hour=11).isoformat(),
            "attendee_person_ids": [guest_id], "confidence": 0.9,
        }))
    return json.loads(raw), gw


def test_solo_meeting_auto_executes_for_the_principal_on_a_verified_surface() -> None:
    _solo()
    pid = _principal()
    # No departments exist at all — nothing named "operations" to gate on.
    result, gw = _book("auto_execute", Session(from_web_chat=True, caller_person_id=pid))
    assert result["status"] == "created"
    gw.call_tool.assert_awaited()


@pytest.mark.parametrize("who", ["email_poller", "contact_on_web", "no_session", "unverified_telegram"])
def test_solo_auto_execute_proposes_unless_the_principal_asked_on_a_verified_surface(
    who: str,
) -> None:
    """auto_execute books only for the principal on a surface that verified
    it is them. An inbound email (the poller's session: no channel, not web),
    a contact's web turn, an unattended caller or a Telegram chat without a
    webhook secret gets a proposal to the principal instead of a booking."""
    from openexecutive.memory.decision_ledger import get_decision_instance

    _solo()
    pid = _principal()
    contact = people_store.upsert_person(full_name="Client Contact", email="client@example.com")
    session = {
        "email_poller": Session(caller_person_id=pid),
        "contact_on_web": Session(from_web_chat=True, caller_person_id=contact),
        "no_session": None,
        "unverified_telegram": Session(
            origin_channel="telegram", origin_channel_ref="555", caller_person_id=pid
        ),
    }[who]
    result, gw = _book("auto_execute", session)
    assert result["status"] == "proposed"
    gw.call_tool.assert_not_awaited()
    instance = get_decision_instance(result["decision_instance_id"])
    assert instance is not None
    assert instance.gate_mode == "propose"
    assert instance.approver_person_id == pid


def test_solo_meeting_proposes_to_the_principal_otherwise(_isolated: Path) -> None:
    from openexecutive.alerts.store import get_alert_by_external
    from openexecutive.memory.decision_ledger import get_decision_instance

    _solo()
    pid = _principal()
    result, gw = _book("propose")
    assert result["status"] == "proposed"
    gw.call_tool.assert_not_awaited()
    instance = get_decision_instance(result["decision_instance_id"])
    assert instance is not None
    assert instance.gate_mode == "propose"
    assert instance.approver_person_id == pid
    alert = get_alert_by_external(
        "decision_scheduling", f"decision:{result['decision_instance_id']}", db_path=_isolated
    )
    assert alert is not None
    assert alert.routed_to_person_id == pid


# --------------------------------------------------------------------------- #
# schedule_followup to the principal
# --------------------------------------------------------------------------- #


def _schedule(channel: str, ref: str, *, session: Session, **extra: Any) -> Any:
    from openexecutive.orchestrator.schedule_tools import handle_schedule_followup

    session.seen_channel_refs.add((channel, ref))
    token = current_session.set(session)
    try:
        raw = asyncio.run(handle_schedule_followup({
            "run_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "channel": channel, "channel_ref": ref,
            "intent": "Check the pricing page went live.",
            "department": "strategy", "required_scope": "wildcard", **extra,
        }))
    finally:
        current_session.reset(token)
    payload = json.loads(raw)
    assert payload["status"] == "scheduled", payload
    row = next(a for a in episodic.list_scheduled_actions(limit=50) if a.id == payload["id"])
    return row


def test_solo_followup_to_the_principal_files_no_proposal_card() -> None:
    _solo()
    pid = _principal()
    row = _schedule("telegram", "555", session=Session())
    assert row.department == ""
    assert row.required_scope is None
    row = _schedule("email", "MAYA@example.com", session=Session(), assigned_to_person_id=pid)
    assert row.department == ""


def test_solo_followup_to_someone_else_keeps_the_gate() -> None:
    _solo()
    _principal()
    other = people_store.upsert_person(full_name="Client Contact", telegram_chat_id="777")
    row = _schedule("telegram", "777", session=Session(), assigned_to_person_id=other)
    assert row.department == "strategy"
    assert row.required_scope == "wildcard"


def test_team_followup_to_the_principal_keeps_the_gate() -> None:
    _principal()
    row = _schedule("telegram", "555", session=Session())
    assert row.department == "strategy"
    assert row.required_scope == "wildcard"


def test_followup_uses_the_turns_pinned_mode_not_a_mid_turn_flip() -> None:
    from openexecutive.memory.workspace_settings import pin_turn_workspace_mode

    _solo()
    _principal()
    session = Session()
    pin_turn_workspace_mode(session)
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="team"))
    row = _schedule("telegram", "555", session=session)
    assert row.department == ""  # still the solo rule, as the turn began


def test_calendar_uses_the_turns_pinned_mode_not_a_mid_turn_flip() -> None:
    from openexecutive.memory.workspace_settings import pin_turn_workspace_mode

    _solo()
    pid = _principal()
    session = Session(from_web_chat=True, caller_person_id=pid)
    pin_turn_workspace_mode(session)
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="team"))
    # _book fails the test if the team department gate is consulted.
    result, _gw = _book("auto_execute", session)
    assert result["status"] == "created"


def test_reflection_pins_its_mode_for_its_tool_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory.workspace_settings import effective_workspace_mode
    from openexecutive.orchestrator import executive

    _solo()
    _principal()
    seen: list[str] = []

    async def _alert(_payload: dict[str, Any]) -> str:
        ws.restore_workspace_settings(ws.WorkspaceSettings(mode="team"))
        seen.append(effective_workspace_mode(current_session.get()))
        return json.dumps({"alert_id": 1})

    monkeypatch.setitem(executive._ALL_SKILL_HANDLERS, "create_alert", _alert)
    provider = _CapturingProvider([
        _Resp([_ToolUse("create_alert", {"headline": "x"})], "tool_use"),
        _Resp([_Text("done")], "end_turn"),
    ])
    _run_reflection(provider, monkeypatch)
    assert seen == ["solo"]


def test_session_override_drives_the_followup_rule() -> None:
    _principal()
    row = _schedule("telegram", "555", session=Session(workspace_mode="solo"))
    assert row.department == ""


# --------------------------------------------------------------------------- #
# Evals
# --------------------------------------------------------------------------- #


def _drive_suite(kind: str, scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from openexecutive.evals import runner

    monkeypatch.setattr(runner, "load_scenarios", lambda kind, scenario_id=None: [scenario])

    async def _go() -> list[Any]:
        return [e async for e in runner.run_scenarios(kind=kind)]

    return asyncio.run(_go())


def test_eval_chat_scenario_runs_in_its_workspace_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import runner
    from openexecutive.orchestrator.executive import Executive

    seen: list[str | None] = []

    async def _chat(self: Any, *, user_message: str, session: Session, **_kw: Any) -> str:
        seen.append(session.workspace_mode)
        return "answer"

    async def _judge(_scenario: dict[str, Any], _response: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setattr(Executive, "chat", _chat)
    monkeypatch.setattr(runner, "judge_chat", _judge)
    for mode in ("solo", None):
        scenario = {"id": f"s-{mode}", "query": "q", "_kind": "chat"}
        if mode:
            scenario["workspace_mode"] = mode
        events = _drive_suite("chat", scenario, monkeypatch)
        assert events[-1] == {"type": "suite_done", "kind": "chat", "passed": 1, "total": 1}
    assert seen == ["solo", None]

    bad = _drive_suite("chat", {"id": "s-bad", "query": "q", "workspace_mode": "duo"}, monkeypatch)
    assert any(e["type"] == "scenario_error" and "workspace_mode" in e["error"] for e in bad)


def test_eval_workflow_scenario_binds_its_workspace_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.evals import runner
    from openexecutive.memory.workspace_settings import effective_workspace_mode
    from openexecutive.workflows import WORKFLOW_REGISTRY
    from openexecutive.workflows.base import WorkflowEvent

    seen: list[str] = []

    class _WF:
        def input_model(self) -> Any:
            return dict

        async def run(self, _inputs: Any, _store: Any) -> Any:
            seen.append(effective_workspace_mode(current_session.get()))
            yield WorkflowEvent(type="artifact", content="x")

    async def _judge(_scenario: dict[str, Any], _artifact: str) -> dict[str, Any]:
        return {"overall": 5}

    monkeypatch.setitem(WORKFLOW_REGISTRY, "solo_probe", _WF())
    monkeypatch.setattr(runner, "judge_workflow", _judge)
    _drive_suite(
        "workflow",
        {"id": "w1", "type": "workflow", "workflow": "solo_probe", "workspace_mode": "solo"},
        monkeypatch,
    )
    _drive_suite("workflow", {"id": "w2", "type": "workflow", "workflow": "solo_probe"}, monkeypatch)
    assert seen == ["solo", "team"]
    assert current_session.get() is None


def test_solo_scenarios_are_shipped_and_valid() -> None:
    from openexecutive.evals.scenarios import validate_scenario_yaml

    files = sorted(SCENARIOS_DIR.glob("solo_*.yaml"))
    assert [f.name for f in files] == [
        "solo_001.yaml", "solo_002.yaml", "solo_003.yaml", "solo_004.yaml",
    ]
    for f in files:
        text = f.read_text(encoding="utf-8")
        s = validate_scenario_yaml(text)
        assert s["workspace_mode"] == "solo"
        assert s["query"]
        # Scenarios describe the principal neutrally; "founder" appears only
        # where it is about owning a business (the studio's clients).
        assert "the founder" not in text.lower()
    # solo_004 is an in-house executive, not a business owner.
    in_house = validate_scenario_yaml((SCENARIOS_DIR / "solo_004.yaml").read_text(encoding="utf-8"))
    assert "VP of Marketing" in in_house["query"]
    assert in_house["company_context"]["headcount"] >= 500
    assert "does_not_assume_the_principal_owns_the_business" in in_house["quality_criteria"]
    assert {"send_company_broadcast", "send_department_message"} <= set(
        in_house["forbidden_tool_calls"]
    )
    with pytest.raises(ValueError, match="workspace_mode"):
        validate_scenario_yaml("id: x\nquery: q\nworkspace_mode: duo\n")


def test_judge_scores_solo_criteria_only_for_solo_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.evals import judges

    prompts: list[str] = []

    class _P:
        async def messages_create(self, **kw: Any) -> Any:
            prompts.append(kw["messages"][0]["content"])
            return SimpleNamespace(content=[SimpleNamespace(text='{"overall": 4}')])

    monkeypatch.setattr(judges, "get_provider", lambda _m: _P())
    base = {"query": "q", "quality_criteria": {"no_team_language": True}}
    asyncio.run(judges.judge_chat({**base, "workspace_mode": "solo"}, "r"))
    asyncio.run(judges.judge_chat(base, "r"))
    assert "solo mode" in prompts[0]
    assert "no team language" in prompts[0]
    assert "solo mode" not in prompts[1]
    assert "no team language" not in prompts[1]
    # The judge's solo framing is role-neutral: it names every kind of
    # principal and tells the judge not to reward assuming one.
    for text in ("run their own business", "lead a function inside a larger",
                 "work independently", "do not reward assuming which"):
        assert text in prompts[0]
    assert "founder" not in prompts[0].lower()
    assert "runs this business" not in prompts[0]


# --------------------------------------------------------------------------- #
# The solo_studio fixture
# --------------------------------------------------------------------------- #


def test_solo_studio_fixture_is_one_principal_with_areas() -> None:
    from openexecutive.memory.company_profile import CompanyProfile

    profile = CompanyProfile.load_from_yaml(SOLO_FIXTURE / "profile.yaml")
    assert profile.name == "Tallgrass Studio"
    assert profile.headcount == 1

    people = yaml.safe_load((SOLO_FIXTURE / "people.yaml").read_text())["people"]
    assert len(people) == 1
    principal = people[0]
    assert principal["is_principal"] is True
    assert principal["authority_scope"] == ["wildcard"]

    departments = yaml.safe_load((SOLO_FIXTURE / "departments.yaml").read_text())["departments"]
    assert 3 <= len(departments) <= 4
    for d in departments:
        assert d["head_person_name"] == principal["full_name"]
        assert 1 <= len(d["goals"]) <= 2
        assert not d.get("cadences")

    assert yaml.safe_load((SOLO_FIXTURE / "workspace.yaml").read_text())["mode"] == "solo"
    memory = json.loads((SOLO_FIXTURE / "memory.json").read_text())
    assert memory["decisions"] and memory["initiatives"]
    assert memory["scheduled_actions"] == []
    assert list((SOLO_FIXTURE / "docs").glob("*.md"))


def test_solo_studio_fixture_seeds_a_solo_workspace() -> None:
    from openexecutive.cli import fixture_loader

    # The same two steps _apply_state_from_source runs on a fixture load:
    # workspace.yaml is read and validated up front, then applied after the
    # people and departments are seeded (a fixture, so not keep_when_missing).
    wanted = fixture_loader._read_workspace_file(SOLO_FIXTURE / "workspace.yaml")
    assert wanted == ws.WorkspaceSettings(mode="solo", timezone="Europe/Stockholm")
    assert fixture_loader._seed_people(SOLO_FIXTURE / "people.yaml") == 1
    assert fixture_loader._seed_departments(SOLO_FIXTURE / "departments.yaml") == 4
    applied = fixture_loader._apply_workspace(wanted, keep_when_missing=False)
    dept_registry.invalidate()
    people_registry.invalidate()

    assert applied == {"mode": "solo", "timezone": "Europe/Stockholm"}
    assert ws.get_workspace() == ws.WorkspaceSettings(mode="solo", timezone="Europe/Stockholm")
    principal = people_store.find_principal_person()
    assert principal is not None and principal.full_name == "Maya Lindqvist"
    states = dept_store.list_departments()
    assert {s.config.slug for s in states} == {"strategy", "finance", "marketing", "product"}
    assert all(s.config.head_person_id == principal.id for s in states)

    from openexecutive.departments.prompt_block import render_org_block

    block = render_org_block(mode="solo")
    assert "### Finance (area slug: finance)" in block
    assert "Collect every invoice within 30 days" in block

    listed = {f["name"]: f for f in fixture_loader.list_fixtures()}
    assert len(listed["solo_studio"]["people"]) == 1
    assert len(listed["solo_studio"]["departments"]) == 4


def _solo_prompt_texts() -> dict[str, str]:
    from openexecutive.briefing.narrative import (
        BRIEFING_NARRATIVE_SOLO_SYSTEM,
        STANDALONE_BRIEF_SOLO_SYSTEM,
    )
    from openexecutive.orchestrator.schedule_tools import withheld_tool_error
    from openexecutive.prompts.executive_persona import EXECUTIVE_PERSONA_SOLO_PROMPT
    from openexecutive.workflows.end_of_day_digest import _EOD_DIGEST_SOLO_SYSTEM
    from openexecutive.workflows.executive_reflection import _build_reflection_system_solo
    from openexecutive.workflows.executive_research import _build_synthesis_system
    from openexecutive.workflows.weekly_review import WEEKLY_TOP_THREE_SYSTEM

    return {
        "persona": EXECUTIVE_PERSONA_SOLO_PROMPT,
        "standalone_brief": STANDALONE_BRIEF_SOLO_SYSTEM,
        "today_header": BRIEFING_NARRATIVE_SOLO_SYSTEM,
        "eod_digest": _EOD_DIGEST_SOLO_SYSTEM,
        "reflection": _build_reflection_system_solo(),
        "research_dm": _build_synthesis_system({"telegram"}, True, mode="solo"),
        "research_no_dm": _build_synthesis_system(set(), True, mode="solo"),
        "withheld_tool_error": withheld_tool_error("send_company_broadcast", "solo"),
        "weekly_review_top_three": WEEKLY_TOP_THREE_SYSTEM,
    }


@pytest.mark.parametrize("name", sorted(_solo_prompt_texts()))
def test_solo_prompts_are_role_neutral(name: str) -> None:
    """Solo principals can be owners, in-house executives or independents:
    no solo prompt assumes a founder running a business alone."""
    text = _solo_prompt_texts()[name]
    assert "founder" not in text.lower()
    for assumption in ("runs this business", "on their own:", "no team to route to"):
        assert assumption not in text
    if name in {
        "persona", "standalone_brief", "today_header", "eod_digest", "reflection",
        "weekly_review_top_three",
    }:
        assert "lead a function inside a larger organisation" in text


def test_scheduled_solo_brief_is_written_for_and_delivered_to_the_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the scheduler (#226's delivery path): in solo the
    morning brief is written with the solo prompt, goes to the principal and
    its outcome is recorded for the Briefing notice and Setup status."""
    from openexecutive.api.routes import today as today_route
    from openexecutive.api.routes.today import ActivityResponse, TodayResponse
    from openexecutive.briefing import brief_state, narrative_cache
    from openexecutive.briefing.narrative import STANDALONE_BRIEF_SOLO_SYSTEM
    from openexecutive.scheduler import runner
    from openexecutive.workflows import persistence as wf_persistence

    _solo()
    _principal()
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(wf_persistence, "DB_PATH", tmp_path / "runs.db")
    wf_persistence.initialize_runs_db(tmp_path / "runs.db")
    monkeypatch.setattr(
        today_route, "_build_today",
        lambda: TodayResponse(departments=[], people=[], proposals=[]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity",
        lambda limit, since=None, **_kw: ActivityResponse(items=[]),
    )
    calls = _capture_synth(monkeypatch)
    sent: list[str] = []

    async def _deliver(text: str, **_kw: object) -> Any:
        sent.append(text)
        return runner.PrincipalDelivery(True, "telegram → 555", "delivered", "telegram")

    async def _no_review(**_kw: object) -> None:
        return None

    monkeypatch.setattr(runner, "_deliver_to_principal", _deliver)
    monkeypatch.setattr(runner, "_enqueue_next_principal_brief", lambda kind, after: None)
    monkeypatch.setattr("openexecutive.alerts.review.run_alert_review", _no_review)

    class _Store:
        def __init__(self, **_kw: object) -> None: ...

    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", _Store)

    action_id = episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="__internal__", channel_ref="principal",
        intent_text="brief", kind="principal_brief_morning",
    )
    action = episodic.get_scheduled_action(action_id)
    assert action is not None
    brief_state_before = brief_state.last_delivery_outcome()
    asyncio.run(runner._run_principal_brief(action, datetime.now(UTC)))

    assert brief_state_before is None
    assert [c["system"] for c in calls] == [STANDALONE_BRIEF_SOLO_SYSTEM]
    assert sent == ["brief"]
    outcome = brief_state.last_delivery_outcome()
    assert outcome is not None
    assert (outcome.kind, outcome.reason, outcome.channel) == (
        "principal_brief_morning", "delivered", "telegram",
    )
