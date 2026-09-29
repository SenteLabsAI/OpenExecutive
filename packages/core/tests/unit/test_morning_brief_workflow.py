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
