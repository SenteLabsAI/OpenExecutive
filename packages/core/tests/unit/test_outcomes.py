"""Attunement outcome ledger: recording, resolution, rates, and their uses."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from openexecutive.attunement import open_loops, outcomes
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.scheduler import nudge_engine


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    yield
    people_registry.invalidate()


@pytest.fixture
def team() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True,
                                           slack_user_id="U_PAT", preferred_channel="slack")
    sara = people_store.upsert_person(full_name="Sara Kim", slack_user_id="U_SARA",
                                      preferred_channel="slack")
    return SimpleNamespace(principal=principal, sara=sara)


def _outcome_rows() -> list[sqlite3.Row]:
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM proactive_outcomes ORDER BY id").fetchall()


def _send(channel_ref: str = "U_SARA", *, web: bool = False) -> None:
    """Drive the real send-side recording point for one Slack DM."""
    from openexecutive.orchestrator.schedule_tools import _record_outbound_context, current_session
    from openexecutive.orchestrator.session import Session

    token = current_session.set(Session(from_web_chat=web))
    try:
        _record_outbound_context(channel="slack_dm", channel_ref=channel_ref, text="ping")
    finally:
        current_session.reset(token)


def _seed(person_id: int, source: str, outcome: str | None, n: int, *, ref: str = "") -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        for _ in range(n):
            conn.execute(
                "INSERT INTO proactive_outcomes (created_at, person_id, source, ref, channel, "
                "channel_ref, outcome) VALUES (?, ?, ?, ?, 'slack_dm', 'x', ?)",
                (now, person_id, source, ref or None, outcome),
            )


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def test_untagged_send_records_nothing(team: SimpleNamespace) -> None:
    _send()
    assert _outcome_rows() == []


def test_tagged_send_records_one_row_linked_to_its_context(team: SimpleNamespace) -> None:
    with outcomes.tag_proactive(outcomes.SOURCE_REFLECTION, "r1"):
        _send()
    [row] = _outcome_rows()
    assert (row["person_id"], row["source"], row["ref"]) == (team.sara, "reflection", "r1")
    assert row["outbound_context_id"] and row["outcome"] is None
    # The tag does not outlive its block.
    assert outcomes.current_tag() is None


def test_send_to_unrostered_or_from_web_records_nothing(team: SimpleNamespace) -> None:
    with outcomes.tag_proactive(outcomes.SOURCE_REFLECTION):
        _send("U_STRANGER")
        _send(web=True)
    assert _outcome_rows() == []


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_matched_reply_resolves_replied(team: SimpleNamespace) -> None:
    with outcomes.tag_proactive(outcomes.SOURCE_FOLLOWUP):
        _send()
    ctx_id = _outcome_rows()[0]["outbound_context_id"]
    assert episodic.mark_outbound_context_consumed(ctx_id)
    assert _outcome_rows()[0]["outcome"] == "replied"
    # One-shot: a second consume changes nothing.
    assert not episodic.mark_outbound_context_consumed(ctx_id)


def test_loop_reported_done_resolves_its_chases_acted(team: SimpleNamespace) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                                   due_at=datetime.now(UTC))
    assert loop_id is not None
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1, ref=f"nudge:commitment:{loop_id}")
    open_loops.close_open_loop(loop_id, reason="reported_done", closed_by_person_id=team.sara)
    assert _outcome_rows()[0]["outcome"] == "acted"


def test_principal_reporting_someone_elses_loop_done_does_not_credit_them(
    team: SimpleNamespace,
) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                                   due_at=datetime.now(UTC))
    assert loop_id is not None
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1, ref=f"nudge:commitment:{loop_id}")
    assert open_loops.close_open_loop(loop_id, reason="reported_done",
                                      closed_by_person_id=team.principal)
    assert _outcome_rows()[0]["outcome"] is None


def test_acted_overrides_an_earlier_ignored_but_a_reply_does_not(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 1, ref="nudge:commitment:7")
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 1, ref="nudge:commitment:8")
    outcomes.resolve_by_ref("nudge:commitment:7", outcomes.OUTCOME_ACTED)
    outcomes.resolve_by_ref("nudge:commitment:8", outcomes.OUTCOME_REPLIED)
    assert [r["outcome"] for r in _outcome_rows()] == ["acted", "ignored"]


def test_loop_expiry_does_not_count_as_acted(team: SimpleNamespace) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                                   due_at=datetime.now(UTC))
    assert loop_id is not None
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1, ref=f"nudge:commitment:{loop_id}")
    open_loops.close_open_loop(loop_id, reason="expired")
    assert _outcome_rows()[0]["outcome"] is None


def _alert(alert_id: int, routed_to: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=alert_id, topic_tags=[], routed_to_person_id=routed_to)


def test_alert_ack_credits_only_its_owner_and_the_principal(team: SimpleNamespace) -> None:
    from openexecutive.alerts.lifecycle import record_ack_feedback

    ben = people_store.upsert_person(full_name="Ben Ortiz", slack_user_id="U_BEN")
    _seed(team.sara, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:7")
    _seed(ben, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:7")
    _seed(team.principal, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:7")
    record_ack_feedback(_alert(7, routed_to=team.sara), "ack")  # type: ignore[arg-type]
    # Ben was DM'd on an earlier pass but it isn't his any more.
    assert [r["outcome"] for r in _outcome_rows()] == ["acted", None, "acted"]


def test_expired_alert_voids_even_already_ignored_dms(team: SimpleNamespace) -> None:
    from openexecutive.alerts.lifecycle import resolve_alert_outreach

    _seed(team.sara, outcomes.SOURCE_ALERT_REVIEW, "ignored", 1, ref="alert:11")
    resolve_alert_outreach(_alert(11), "expired")  # type: ignore[arg-type]
    assert _outcome_rows()[0]["outcome"] == "void"


def test_dismissed_alert_voids_its_dms_and_never_counts_against_anyone(team: SimpleNamespace) -> None:
    from openexecutive.alerts.lifecycle import record_ack_feedback

    _seed(team.sara, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:7")
    record_ack_feedback(_alert(7, routed_to=team.sara), "dismissed")  # type: ignore[arg-type]
    assert _outcome_rows()[0]["outcome"] == "void"
    assert (team.sara, outcomes.SOURCE_ALERT_REVIEW) not in outcomes.acceptance()
    record_ack_feedback(_alert(8), "read")  # type: ignore[arg-type]


def test_review_closing_its_own_alert_resolves_its_dms(team: SimpleNamespace) -> None:
    from openexecutive.alerts.lifecycle import resolve_alert_outreach

    _seed(team.sara, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:9")
    _seed(team.sara, outcomes.SOURCE_ALERT_REVIEW, None, 1, ref="alert:10")
    resolve_alert_outreach(_alert(9, routed_to=team.sara), "resolved")  # type: ignore[arg-type]
    resolve_alert_outreach(_alert(10, routed_to=team.sara), "stale")  # type: ignore[arg-type]
    assert [r["outcome"] for r in _outcome_rows()] == ["acted", "void"]


async def test_resolved_approval_credits_its_nudges(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from openexecutive.workflows import persistence as wf_persistence
    from openexecutive.workflows.resumer import apply_resolution
    from openexecutive.workflows.wait_for_human import WaitForHumanResolution

    monkeypatch.setattr(wf_persistence, "DB_PATH", episodic.DB_PATH)
    wf_persistence.initialize_runs_db(episodic.DB_PATH)
    wf_persistence.create_run("run-1", "test_wf", "Test run", {})
    wf_persistence.save_checkpoint("run-1", json.dumps({"channel": "slack", "channel_ref": "U"}),
                                   team.sara, datetime.now(UTC) + timedelta(hours=1))
    ben = people_store.upsert_person(full_name="Ben Ortiz", slack_user_id="U_BEN")
    _seed(team.sara, outcomes.SOURCE_NUDGE_STALLED, None, 1, ref="nudge:stalled:run-1")
    _seed(ben, outcomes.SOURCE_NUDGE_STALLED, None, 1, ref="nudge:stalled:run-1")
    resolution = WaitForHumanResolution(
        run_id="run-1", reply_text="approved", source_channel="web", source_message_id="m",
        parsed_decision={"decision": "approve", "note": ""}, person_id=team.sara,
    )
    assert await apply_resolution("run-1", resolution)
    # Only the approver's nudge landed; Ben's is still open.
    assert [r["outcome"] for r in _outcome_rows()] == ["acted", None]


def test_initiative_update_credits_only_the_updaters_check_ins(team: SimpleNamespace) -> None:
    episodic.store_initiative("Launch", "active", db_path=episodic.DB_PATH)
    [init] = episodic.get_active_initiatives(db_path=episodic.DB_PATH)
    ref = f"nudge:initiative:{init.id}"
    _seed(team.sara, outcomes.SOURCE_NUDGE_INITIATIVE, None, 1, ref=ref)
    _seed(team.principal, outcomes.SOURCE_NUDGE_INITIATIVE, None, 1, ref=ref)
    # A same-status, same-summary re-mention is not an answer.
    episodic.store_initiative("Launch", "active", db_path=episodic.DB_PATH,
                              updated_by_person_id=team.sara)
    assert [r["outcome"] for r in _outcome_rows()] == [None, None]
    episodic.store_initiative("Launch", "active", summary="on track", db_path=episodic.DB_PATH,
                              updated_by_person_id=team.sara)
    assert [r["outcome"] for r in _outcome_rows()] == ["acted", None]


def test_anonymous_initiative_edit_credits_nobody(team: SimpleNamespace) -> None:
    """``PATCH /memories/initiatives/{id}`` has no caller identity and calls
    ``update_initiative`` without an updater, so an edit through it must not
    mark anyone's check-ins as answered."""
    episodic.store_initiative("Launch", "active", db_path=episodic.DB_PATH)
    [init] = episodic.get_active_initiatives(db_path=episodic.DB_PATH)
    _seed(team.sara, outcomes.SOURCE_NUDGE_INITIATIVE, None, 1, ref=f"nudge:initiative:{init.id}")
    assert episodic.update_initiative(init.id, summary="x", db_path=episodic.DB_PATH)
    assert _outcome_rows()[0]["outcome"] is None


def test_principal_tidying_a_loop_does_not_credit_the_owner(team: SimpleNamespace) -> None:
    ids = [open_loops.open_loop(owner_person_id=team.sara, description=f"deliver thing {i}",
                                due_at=datetime.now(UTC)) for i in range(2)]
    for loop_id in ids:
        _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1, ref=f"nudge:commitment:{loop_id}")
    open_loops.close_open_loop(ids[0], reason="done", closed_by_person_id=team.principal)
    open_loops.close_open_loop(ids[1], reason="done", closed_by_person_id=team.sara)
    assert [r["outcome"] for r in _outcome_rows()] == [None, "acted"]


def test_principal_self_reminders_are_not_outreach(team: SimpleNamespace) -> None:
    with outcomes.tag_proactive(outcomes.SOURCE_FOLLOWUP):
        _send("U_PAT")
    assert _outcome_rows() == []


def test_email_cc_is_not_counted_as_outreach(team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import mcp_gateway
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    people_store.update_person(team.sara, email="sara@acme.test")
    people_store.update_person(team.principal, email="pat@acme.test")
    token = current_session.set(Session())
    try:
        with outcomes.tag_proactive(outcomes.SOURCE_NUDGE_COMMITMENT, "nudge:commitment:1"):
            mcp_gateway._record_email_outbound_context(
                {"to": "sara@acme.test", "cc": ["pat@acme.test"], "body": "Any update?"}
            )
    finally:
        current_session.reset(token)
    assert [r["person_id"] for r in _outcome_rows()] == [team.sara]


def test_email_outcome_goes_to_the_first_rostered_to_address(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator import mcp_gateway
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    people_store.update_person(team.sara, email="sara@acme.test")
    token = current_session.set(Session())
    try:
        with outcomes.tag_proactive(outcomes.SOURCE_NUDGE_COMMITMENT, "nudge:commitment:1"):
            mcp_gateway._record_email_outbound_context(
                {"to": ["vendor@else.test", "sara@acme.test"], "body": "Any update?"}
            )
    finally:
        current_session.reset(token)
    assert [r["person_id"] for r in _outcome_rows()] == [team.sara]


def test_rates_sweep_on_read_even_without_a_nudge_scan(team: SimpleNamespace) -> None:
    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO proactive_outcomes (created_at, person_id, source, channel, channel_ref) "
            "VALUES (?, ?, 'reflection', 'slack_dm', 'x')", (old, team.sara))
    stats = outcomes.acceptance()[(team.sara, outcomes.SOURCE_REFLECTION)]
    assert (stats.resolved, stats.positive) == (1, 0)


def test_sweep_marks_only_stale_open_rows_ignored(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_REFLECTION, None, 1)
    _seed(team.sara, outcomes.SOURCE_REFLECTION, "replied", 1)
    assert outcomes.sweep_ignored(datetime.now(UTC) + timedelta(hours=73), after_hours=72) == 1
    assert [r["outcome"] for r in _outcome_rows()] == ["ignored", "replied"]
    assert outcomes.sweep_ignored(datetime.now(UTC), after_hours=72) == 0


# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #


def test_acceptance_counts(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 5)
    _seed(team.sara, outcomes.SOURCE_REFLECTION, "replied", 3)
    _seed(team.sara, outcomes.SOURCE_REFLECTION, None, 1)
    stats = outcomes.acceptance()
    loops = stats[(team.sara, outcomes.SOURCE_OPEN_LOOP)]
    assert (loops.sent, loops.resolved, loops.positive) == (5, 5, 0)
    refl = stats[(team.sara, outcomes.SOURCE_REFLECTION)]
    assert (refl.sent, refl.resolved, refl.pending) == (4, 3, 1)


def test_muted_reads_the_latest_sends_by_time_not_insert_order(team: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        # The reply is the newest send even though its row was written first.
        for age_h, outcome in [(0, "replied"), (5, "ignored"), (4, "ignored"), (3, "ignored"),
                               (2, "ignored"), (1, "ignored")]:
            conn.execute(
                "INSERT INTO proactive_outcomes (created_at, person_id, source, channel, "
                "channel_ref, outcome) VALUES (?, ?, ?, 'slack_dm', 'x', ?)",
                ((now - timedelta(hours=age_h)).isoformat(), team.sara,
                 outcomes.SOURCE_OPEN_LOOP, outcome),
            )
    assert outcomes.muted_pairs(min_sends=5) == set()


def test_muted_needs_the_last_n_all_unanswered(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 5)
    _seed(team.principal, outcomes.SOURCE_OPEN_LOOP, "ignored", 4)
    assert outcomes.muted_pairs(min_sends=5) == {(team.sara, outcomes.SOURCE_OPEN_LOOP)}


def test_reflection_block_needs_enough_history(team: SimpleNamespace) -> None:
    from openexecutive.workflows.executive_reflection import _render_reflection_context

    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "replied", 3)
    _seed(team.sara, outcomes.SOURCE_RESEARCH, "ignored", 2)
    [line] = outcomes.render_for_reflection()
    assert "Sara Kim" in line and "open-loop chases 3/3 answered" in line
    assert "research" not in line
    text = _render_reflection_context(period_label="p", today_data={}, activity=[],
                                      recent_alerts=[], external_signals=[], outreach=[line])
    assert "WHAT LANDS" in text


# --------------------------------------------------------------------------- #
# Uses: nudge ranking, source mapping, API
# --------------------------------------------------------------------------- #


def _candidate(scope: str, person_id: int, urgency: float, source: str) -> nudge_engine.NudgeCandidate:
    return nudge_engine.NudgeCandidate(
        scope_key=scope, intent_text="x", source="commitment", urgency_seconds=urgency,
        cooldown=timedelta(hours=48), person_id=person_id, outcome_source=source,
    )


def test_ignored_source_is_ranked_last_on_longer_cooldown(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 5)
    muted = _candidate("nudge:commitment:1", team.sara, -9999, outcomes.SOURCE_OPEN_LOOP)
    fresh = _candidate("nudge:commitment:2", team.principal, 0, outcomes.SOURCE_NUDGE_COMMITMENT)
    settings = SimpleNamespace(attunement_mute_min_sends=5, attunement_mute_cooldown_multiplier=3)
    cands, demoted = nudge_engine._apply_outcome_feedback([muted, fresh], settings)
    assert demoted == {"nudge:commitment:1"}
    assert next(c for c in cands if c.scope_key == "nudge:commitment:1").cooldown == timedelta(hours=144)
    ranked = nudge_engine._apply_caps(cands, max_total=10, max_per_person=2, demoted=demoted)
    assert [c.scope_key for c in ranked] == ["nudge:commitment:2", "nudge:commitment:1"]


def test_one_answer_lifts_the_demotion(team: SimpleNamespace) -> None:
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "ignored", 5)
    settings = SimpleNamespace(attunement_mute_min_sends=5, attunement_mute_cooldown_multiplier=3)
    cand = [_candidate("nudge:commitment:1", team.sara, 0, outcomes.SOURCE_OPEN_LOOP)]
    assert nudge_engine._apply_outcome_feedback(cand, settings)[1] == {"nudge:commitment:1"}
    # One reply, resolved after the ignores, is the most recent outcome.
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1, ref="nudge:commitment:9")
    outcomes.resolve_by_ref("nudge:commitment:9", outcomes.OUTCOME_REPLIED)
    assert nudge_engine._apply_outcome_feedback(cand, settings)[1] == set()


def test_commitment_selector_labels_open_loops(team: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                         due_at=now - timedelta(hours=1))
    [cand] = nudge_engine._select_stale_commitment_candidates(now, stale_days=3, cooldown_hours=48)
    assert cand.outcome_source == outcomes.SOURCE_OPEN_LOOP


def test_dispatch_source_mapping(team: SimpleNamespace) -> None:
    from openexecutive.scheduler.runner import _outreach_source

    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                                   due_at=datetime.now(UTC))

    def action(kind: str, scope: str | None) -> episodic.ScheduledAction:
        return episodic.ScheduledAction(id=5, created_at="", run_at="", channel="slack_dm",
                                        channel_ref="U", intent_text="x", kind=kind, scope_key=scope)

    assert _outreach_source(action("proactive_nudge", f"nudge:commitment:{loop_id}")) == (
        outcomes.SOURCE_OPEN_LOOP, f"nudge:commitment:{loop_id}")
    assert _outreach_source(action("proactive_nudge", "nudge:commitment:99999"))[0] == (
        outcomes.SOURCE_NUDGE_COMMITMENT)
    assert _outreach_source(action("proactive_nudge", "nudge:stalled:r1"))[0] == (
        outcomes.SOURCE_NUDGE_STALLED)
    assert _outreach_source(action("proactive_nudge", "nudge:initiative:3"))[0] == (
        outcomes.SOURCE_NUDGE_INITIATIVE)
    assert _outreach_source(action("ad_hoc", None)) == (outcomes.SOURCE_FOLLOWUP, "action:5")


def test_outreach_route_is_principal_or_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import people as route

    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, "replied", 2)
    _seed(team.sara, outcomes.SOURCE_OPEN_LOOP, None, 1)

    def call(caller: int | None) -> list[route.OutreachStat] | int:
        monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda req: caller)
        try:
            return route.get_person_outreach(team.sara, request=None)  # type: ignore[arg-type]
        except HTTPException as exc:
            return exc.status_code

    [row] = call(team.sara)  # type: ignore[misc]
    assert (row.source, row.sent, row.replied, row.pending) == ("open_loop", 3, 2, 1)
    assert isinstance(call(team.principal), list)
    assert call(None) == 403
    assert call(team.sara + 100) == 403  # no existence probe before auth


def test_reset_paths_wipe_the_ledger() -> None:
    from openexecutive.clients.slots import _BLANK_WIPE_TABLES

    assert "proactive_outcomes" in _BLANK_WIPE_TABLES
