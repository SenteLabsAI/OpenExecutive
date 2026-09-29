"""MorningBriefWorkflow contract: it must use the STANDALONE brief prompt.

The morning brief is delivered as a DM with no cards beside it, so it must
enumerate actionables (standalone=True) rather than the /today header synthesis
that assumes a card list renders below it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openexecutive.api.routes import today as today_route
from openexecutive.api.routes.today import ActivityResponse, TodayResponse
from openexecutive.briefing import brief_state, narrative_cache
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.workflows.morning_brief import (
    MorningBriefInput,
    MorningBriefWorkflow,
)


@pytest.fixture(autouse=True)
def _isolated_brief_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")
    # The "handled overnight" block reads the audit log; keep it empty and
    # deterministic here regardless of what other modules audited.
    monkeypatch.setattr(brief_state, "handled_since", lambda since, limit=20: [])
    # The live blocks read the audit log, the principal's chats and the
    # calendar; point them at empty, isolated stores.
    from openexecutive.audit import logger as audit_logger
    from openexecutive.briefing import live_signals
    from openexecutive.memory import episodic

    monkeypatch.setattr(
        audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=tmp_path / "audit.db")
    )
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "episodic.db")
    episodic.initialize_db()

    async def _no_calendar(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(live_signals, "refresh_calendar", _no_calendar)


@pytest.mark.asyncio
async def test_morning_brief_uses_standalone_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _synth(**kw: object) -> str:
        captured.update(kw)
        return "MORNING BRIEF BODY"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    # Avoid touching the DB — stub the aggregators.
    monkeypatch.setattr(
        today_route, "_build_today",
        lambda **_kw: TodayResponse(departments=[], people=[], proposals=[]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None, **_kw: ActivityResponse(items=[])
    )

    wf = MorningBriefWorkflow()
    events = [
        e async for e in wf.run(MorningBriefInput(period_label="2026-05-29"), MagicMock())
    ]

    # The morning brief must request the standalone (enumerated) prompt.
    assert captured.get("standalone") is True
    assert captured.get("viewer") is None
    # And the synthesized body becomes the artifact.
    artifacts = [e for e in events if getattr(e, "type", "") == "artifact"]
    assert artifacts and "MORNING BRIEF BODY" in artifacts[0].content


def _stub_aggregators(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes.today import ProposalItem

    monkeypatch.setattr(
        today_route, "_build_today",
        lambda **_kw: TodayResponse(departments=[], people=[], proposals=[
            ProposalItem(
                alert_id=1, headline="Renew Acme", body="b", routed_to_person_id=None,
                suggested_action="", created_at="2026-01-01T00:00:00+00:00", topic_tags=[],
            ),
        ]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None, **_kw: ActivityResponse(items=[])
    )


@pytest.mark.asyncio
async def test_morning_brief_passes_window_and_emits_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _synth(**kw: object) -> str:
        captured.update(kw)
        return "FULL BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    _stub_aggregators(monkeypatch)

    events = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    result = next(e for e in events if e.type == "result")
    assert result.data["suppressed"] is False
    assert len(result.data["brief_fingerprint"]) == 64
    assert captured["since"] is not None
    assert captured["handled"] == []
    assert any(e.type == "artifact" and e.content == "FULL BRIEF" for e in events)


@pytest.mark.asyncio
async def test_morning_brief_suppressed_when_fingerprint_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _synth(**kw: object) -> str:
        calls["n"] += 1
        return "FULL BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    _stub_aggregators(monkeypatch)

    first = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    fp = next(e for e in first if e.type == "result").data["brief_fingerprint"]
    assert calls["n"] == 1
    # The scheduler records a delivery; the next run sees an identical fingerprint.
    brief_state.record_delivered("principal_brief_morning", fp, "FULL BRIEF")

    second = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    result = next(e for e in second if e.type == "result")
    artifact = next(e for e in second if e.type == "artifact")
    assert result.data["suppressed"] is True
    assert calls["n"] == 1  # no model call
    assert artifact.content == "Nothing new since yesterday's brief — 1 item still waiting on you."

    # force_full bypasses the suppression.
    third = [
        e async for e in MorningBriefWorkflow().run(MorningBriefInput(force_full=True), MagicMock())
    ]
    assert next(e for e in third if e.type == "result").data["suppressed"] is False
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# The principal's live world (briefing.live_signals) and what stays private
# --------------------------------------------------------------------------- #

def _log_inbound(subject: str, *, private: bool = False) -> None:
    from openexecutive.audit import logger as audit_logger

    audit_logger.get_audit_logger().log(
        "integration_inbound", f"Inbound email from sam@x.com: {subject}", actor="email",
        details={"channel": "email", "from": "sam@x.com", "subject": subject},
        private=private,
    )


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def _synth(**kw: object) -> str:
        calls.append(kw)
        return "FULL BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    return calls


@pytest.mark.asyncio
async def test_inbound_since_the_last_brief_reaches_the_brief_and_unsuppresses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture(monkeypatch)
    _stub_aggregators(monkeypatch)

    first = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    fp = next(e for e in first if e.type == "result").data["brief_fingerprint"]
    brief_state.record_delivered("principal_brief_morning", fp, "FULL BRIEF")
    _log_inbound("vendor renewal terms")

    second = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    assert next(e for e in second if e.type == "result").data["suppressed"] is False
    live = calls[-1]["live"]
    assert "vendor renewal terms" in "\n".join(live.inbound)  # type: ignore[attr-defined]
    assert calls[-1]["live_window"] == "since the last brief"


@pytest.mark.asyncio
async def test_a_teammates_correction_reaches_the_brief_and_unsuppresses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import facts

    calls = _capture(monkeypatch)
    _stub_aggregators(monkeypatch)
    first = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    fp = next(e for e in first if e.type == "result").data["brief_fingerprint"]
    brief_state.record_delivered("principal_brief_morning", fp, "FULL BRIEF")
    facts.record_fact(subject="Cedar Court unit count", statement="Cedar Court has 38 units.",
                      source_quote="q", recorded_by_role="teammate", recorded_by_name="Sam Lee")

    second = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    assert next(e for e in second if e.type == "result").data["suppressed"] is False
    rendered = str(calls[-1]["rendered_context"])
    assert "TEAMMATE CORRECTIONS SINCE LAST BRIEF" in rendered
    assert "Sam Lee recorded Cedar Court unit count: Cedar Court has 38 units." in rendered
    # Once delivered, the same correction is not news the next time.
    fp2 = next(e for e in second if e.type == "result").data["brief_fingerprint"]
    brief_state.record_delivered("principal_brief_morning", fp2, "FULL BRIEF")
    n_calls = len(calls)
    _ = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    assert all("TEAMMATE CORRECTIONS" not in str(c["rendered_context"]) for c in calls[n_calls:])


@pytest.mark.asyncio
async def test_a_teammates_proposal_is_only_in_the_principals_private_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import facts
    from openexecutive.workflows import morning_brief

    calls = _capture(monkeypatch)
    _stub_aggregators(monkeypatch)
    facts.record_fact(subject="Oak Row", statement="Oak Row has 12 units.", source_quote="q",
                      recorded_by_role="teammate", recorded_by_name="Sam Lee", proposed=True)
    # Anyone may run the brief from chat; its run history is shared.
    shared = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    assert "Oak Row" not in str(calls[-1]["rendered_context"])
    assert next(e for e in shared if e.type == "result").data["private_to_principal"] is False
    token = morning_brief.PRINCIPAL_DELIVERY.set(True)
    try:
        delivered = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(force_full=True), MagicMock())]
    finally:
        morning_brief.PRINCIPAL_DELIVERY.reset(token)
    assert "Sam Lee proposed Oak Row" in str(calls[-1]["rendered_context"])
    assert next(e for e in delivered if e.type == "result").data["private_to_principal"] is True


def test_teammate_changes_count_against_a_quiet_day() -> None:
    block = "TEAMMATE CORRECTIONS SINCE LAST BRIEF (…):\n- Sam Lee recorded X: Y."
    quiet = briefing_narrative.render_briefing_context(
        period_label="Today", today_data={}, activity=[], standing_facts="",
    )
    news = briefing_narrative.render_briefing_context(
        period_label="Today", today_data={}, activity=[], standing_facts="", teammate_changes=block,
    )
    assert "No org activity" in quiet and "No org activity" not in news and block in news
    base = {"today_data": {}, "activity": [], "handled": [], "since": None}
    assert brief_state.build_brief_fingerprint(**base) != brief_state.build_brief_fingerprint(
        **base, teammate_changes=block,
    )
    assert brief_state.build_brief_fingerprint(**base) == brief_state.build_brief_fingerprint(
        **base, teammate_changes="",
    )


@pytest.mark.asyncio
async def test_private_rows_only_on_a_run_for_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows.morning_brief import PRINCIPAL_DELIVERY

    calls = _capture(monkeypatch)
    seen: list[bool] = []

    def _build_today(*, include_private: bool = False, **_kw: object) -> TodayResponse:
        seen.append(include_private)
        return TodayResponse(departments=[], people=[], proposals=[])

    monkeypatch.setattr(today_route, "_build_today", _build_today)
    monkeypatch.setattr(
        today_route, "_build_activity",
        lambda limit, since=None, **_kw: ActivityResponse(items=[]),
    )
    _log_inbound("a private matter", private=True)

    anyone = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    token = PRINCIPAL_DELIVERY.set(True)
    try:
        own = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    finally:
        PRINCIPAL_DELIVERY.reset(token)

    assert seen == [False, True]
    assert calls[0]["live"].inbound == ()  # type: ignore[attr-defined]
    assert "a private matter" in "\n".join(calls[1]["live"].inbound)  # type: ignore[attr-defined]
    # The run that used a private row says so, so its text stays out of the
    # shared run history; the one that did not keeps it.
    assert next(e for e in anyone if e.type == "result").data["private_to_principal"] is False
    assert next(e for e in own if e.type == "result").data["private_to_principal"] is True


@pytest.mark.asyncio
async def test_reflection_flags_reach_the_brief(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows import persistence

    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "runs.db")
    persistence.initialize_runs_db()
    persistence.create_run("r1", "executive_reflection", "Executive Reflection", {})
    persistence.complete_run(
        "r1",
        "**Acted on:**\n- DM'd Sam\n\n**Flagged for the brief:**\n- Audit deadline "
        "is Friday\n\n**Quiet:** nothing else.",
    )
    calls = _capture(monkeypatch)
    _stub_aggregators(monkeypatch)

    _ = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    assert calls[0]["reflection_flags"] == "- Audit deadline is Friday"


def test_stored_artifact_withholds_a_private_brief() -> None:
    from openexecutive.workflows.persistence import PRIVATE_RUN_ARTIFACT, stored_artifact

    assert stored_artifact("text", private_to_principal=False) == "text"
    assert stored_artifact("text", private_to_principal=True) == PRIVATE_RUN_ARTIFACT
    assert stored_artifact("", private_to_principal=True) == ""


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [("slack:dm:U1", True), ("slack:channel:C1:U1", False), ("discord:guild:1:2", False)],
)
def test_a_chat_run_reads_private_rows_only_in_a_private_conversation(
    monkeypatch: pytest.MonkeyPatch, session_id: str, expected: bool,
) -> None:
    """The principal asking in a shared channel gets the reply posted there,
    so their private rows stay out of it."""
    from types import SimpleNamespace

    from openexecutive.orchestrator import people_tools
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.workflows import morning_brief

    monkeypatch.setattr(people_tools, "is_principal_on_verified_surface", lambda s: True)
    session = SimpleNamespace(
        session_id=session_id, origin_channel=session_id.split(":", 1)[0], from_web_chat=False,
    )
    token = current_session.set(session)  # type: ignore[arg-type]
    try:
        assert morning_brief._private_ok() is expected
    finally:
        current_session.reset(token)


def test_a_private_row_past_the_top_groups_still_counts() -> None:
    from openexecutive.briefing.live_signals import LiveSignals
    from openexecutive.workflows.morning_brief import _differs

    keys = {"inbound": ["a|b|1"], "stuck": [], "drafts": 0, "conversations": [], "calendar": ""}
    own = LiveSignals(inbound_total=12, keys=keys)
    shared = LiveSignals(inbound_total=11, keys=dict(keys))
    assert _differs(own, shared) is True
    assert _differs(shared, LiveSignals(inbound_total=11, keys=dict(keys))) is False


# --------------------------------------------------------------------------- #
# Grounding (briefing/grounding.py): what ships names only what the context holds
# --------------------------------------------------------------------------- #


def _ground_in_isolation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    from openexecutive.briefing import grounding

    monkeypatch.setattr(grounding, "profile_sources", lambda: [])
    monkeypatch.setattr(grounding, "org_sources", lambda: [])
    monkeypatch.setattr(grounding, "roster", lambda: [])
    monkeypatch.setattr(grounding, "grounding_mode", lambda: "enforce")
    monkeypatch.setattr(grounding, "citations_enabled", lambda: True)
    rows: list[dict[str, object]] = []

    def _log(event_type: str, summary: str, **kw: object) -> None:
        if event_type == "grounding":
            rows.append({"summary": summary, **kw})

    monkeypatch.setattr("openexecutive.audit.log_event", _log)
    return rows


def _stub_lease_board(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.routes.today import ProposalItem

    monkeypatch.setattr(
        today_route, "_build_today",
        lambda **_kw: TodayResponse(departments=[], people=[], proposals=[
            ProposalItem(
                alert_id=1, headline="Renew Acme lease for 48 units", body="b",
                routed_to_person_id=None, suggested_action="",
                created_at="2099-01-01T00:00:00+00:00", topic_tags=[],
            ),
        ]),
    )
    monkeypatch.setattr(
        today_route, "_build_activity", lambda limit, since=None, **_kw: ActivityResponse(items=[])
    )


class _SeqProvider:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, object]] = []

    async def messages_create(self, **kw: object) -> object:
        from types import SimpleNamespace

        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.texts.pop(0))])


@pytest.mark.asyncio
async def test_morning_brief_holds_back_a_fabricated_colleague_and_cites_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive import providers

    rows = _ground_in_isolation(monkeypatch)
    _stub_lease_board(monkeypatch)
    captured: dict[str, object] = {}
    draft = (
        "**Needs you**\n- Renew Acme lease for 48 units\n"
        "- Marcus Lee wants a call about 52 units"
    )

    async def _synth(**kw: object) -> str:
        captured.update(kw)
        return draft

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    # The one repair attempt changes nothing, so the line is dropped.
    repair = _SeqProvider([draft])
    monkeypatch.setattr(providers, "get_provider", lambda _m: repair)
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")

    events = [e async for e in MorningBriefWorkflow().run(MorningBriefInput(), MagicMock())]
    artifact = next(e for e in events if e.type == "artifact").content

    # The synthesizer got the context the grounding pass checked against.
    assert "Renew Acme lease for 48 units" in str(captured["rendered_context"])
    assert "'Marcus Lee', '52'" in repair.calls[0]["messages"][0]["content"]  # type: ignore[index]
    assert "Marcus" not in artifact
    assert "- Renew Acme lease for 48 [1] units" in artifact
    assert "_Held back 1 line " in artifact
    assert "- [1] Needs you — Renew Acme lease for 48 units" in artifact
    step = next(e for e in events if e.type == "step_done" and e.step_id == "synthesize")
    assert "1 line(s) held back" in step.summary
    [row] = rows
    assert row["private"] is False
    assert row["details"]["names_bad"] == ["Marcus Lee"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_eod_digest_keeps_a_repair_that_grounds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive import providers
    from openexecutive.workflows.end_of_day_digest import (
        EndOfDayDigestInput,
        EndOfDayDigestWorkflow,
    )

    rows = _ground_in_isolation(monkeypatch)
    _stub_lease_board(monkeypatch)
    provider = _SeqProvider([
        "**Still pending**\n- Acme lease, 48 units — Marcus Lee is blocking",
        "**Still pending**\n- Acme lease, 48 units",
    ])
    monkeypatch.setattr(providers, "get_provider", lambda _m: provider)
    monkeypatch.setattr("openexecutive.agents.utility_fast.get_fast_model", lambda: "claude-test")

    events = [e async for e in EndOfDayDigestWorkflow().run(EndOfDayDigestInput(), MagicMock())]
    artifact = next(e for e in events if e.type == "artifact").content

    assert len(provider.calls) == 2
    assert provider.calls[1]["system"] == provider.calls[0]["system"]
    assert artifact.startswith("**Still pending**\n- Acme lease, 48 [1] units")
    assert "Marcus" not in artifact and "Held back" not in artifact
    [row] = rows
    assert row["details"]["repaired"] is True  # type: ignore[index]
    assert row["details"]["held_back"] == []  # type: ignore[index]
