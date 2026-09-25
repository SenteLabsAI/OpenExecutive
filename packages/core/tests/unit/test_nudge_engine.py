"""Tests for the proactive nudge engine."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.audit import logger as audit_logger_module
from openexecutive.config import Settings
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.scheduler import nudge_engine
from openexecutive.workflows import persistence as wf_persistence


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    # Initiative writes mirror to Honcho, which audits: keep those rows out
    # of the default ./episodic_memory.db.
    audit_log = audit_logger_module.AuditLogger(tmp_path / "audit.db")
    monkeypatch.setattr(audit_logger_module, "get_audit_logger", lambda: audit_log)

    dept_registry.invalidate()
    people_registry.invalidate()

    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    wf_persistence.initialize_runs_db(db)

    yield

    dept_registry.invalidate()
    people_registry.invalidate()


def _now() -> datetime:
    return datetime.now(UTC)


def _make_person(
    *,
    full_name: str = "Alice CFO",
    slack_user_id: str | None = "U_ALICE",
    preferred: str = "slack",
) -> int:
    return people_store.upsert_person(
        full_name=full_name,
        role="CFO",
        slack_user_id=slack_user_id,
        preferred_channel=preferred,  # type: ignore[arg-type]
    )


def _insert_workflow_run(
    *,
    run_id: str,
    awaiting_person_id: int | None,
    awaiting_until: datetime | None,
    updated_at: datetime,
    title: str = "Vendor renegotiation approval",
) -> None:
    """Insert directly via SQL so we can pin updated_at to whatever we like."""
    until_str = awaiting_until.isoformat() if awaiting_until else None
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO workflow_runs "
            "(run_id, workflow_name, title, status, inputs, created_at, "
            "updated_at, state_json, awaiting_person_id, awaiting_until) "
            "VALUES (?, ?, ?, 'awaiting_human', ?, ?, ?, ?, ?, ?)",
            (
                run_id, "vendor_neg", title, "{}", _now().isoformat(),
                updated_at.isoformat(), "{}", awaiting_person_id, until_str,
            ),
        )
        conn.commit()


def _settings(**overrides: object) -> Settings:
    base = dict(
        nudge_scan_enabled=True,
        nudge_scan_interval_minutes=15,
        nudge_stalled_lead_hours=24,
        nudge_stalled_min_quiet_hours=24,
        nudge_stalled_cooldown_hours=24,
        nudge_commitment_stale_days=3,
        nudge_commitment_cooldown_hours=48,
        nudge_initiative_idle_days=7,
        nudge_initiative_cooldown_days=7,
        nudge_max_defer_days=3,
        nudge_max_per_scan=10,
        nudge_max_per_person_per_scan=2,
        nudge_max_per_scope=3,
    )
    base.update(overrides)
    return base  # type: ignore[return-value]


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Make get_settings() return a dataclass-ish stub with our overrides."""
    s = _settings(**overrides)

    class _Stub:
        pass

    stub = _Stub()
    for k, v in s.items():
        setattr(stub, k, v)
    monkeypatch.setattr(nudge_engine, "get_settings", lambda: stub, raising=False)
    # The runner branch and bootstrap call get_settings inside the
    # nudge_engine module, so patching there is sufficient.
    import openexecutive.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: stub)


# ---------------------------------------------------------------------------
# Stalled workflow candidates
# ---------------------------------------------------------------------------

class TestStalledWorkflowCandidates:
    def test_within_lead_and_quiet_is_candidate(self) -> None:
        pid = _make_person()
        now = _now()
        _insert_workflow_run(
            run_id="run_a",
            awaiting_person_id=pid,
            awaiting_until=now + timedelta(hours=12),
            updated_at=now - timedelta(days=2),
        )
        out = nudge_engine._select_stalled_workflow_candidates(
            now, lead_hours=24, min_quiet_hours=24, cooldown_hours=24,
        )
        assert len(out) == 1
        assert out[0].scope_key == "nudge:stalled:run_a"
        assert out[0].source == "stalled"
        assert out[0].person_id == pid

    def test_recent_update_excluded(self) -> None:
        """A run touched in the last quiet-hours window is still moving."""
        pid = _make_person()
        now = _now()
        _insert_workflow_run(
            run_id="run_b",
            awaiting_person_id=pid,
            awaiting_until=now + timedelta(hours=12),
            updated_at=now - timedelta(hours=1),
        )
        out = nudge_engine._select_stalled_workflow_candidates(
            now, lead_hours=24, min_quiet_hours=24, cooldown_hours=24,
        )
        assert out == []

    def test_no_person_excluded(self) -> None:
        now = _now()
        _insert_workflow_run(
            run_id="run_c",
            awaiting_person_id=None,
            awaiting_until=now + timedelta(hours=12),
            updated_at=now - timedelta(days=2),
        )
        out = nudge_engine._select_stalled_workflow_candidates(
            now, lead_hours=24, min_quiet_hours=24, cooldown_hours=24,
        )
        assert out == []

    def test_already_timed_out_excluded(self) -> None:
        """Resumer handles timed-out runs; we never pile on."""
        pid = _make_person()
        now = _now()
        _insert_workflow_run(
            run_id="run_d",
            awaiting_person_id=pid,
            awaiting_until=now - timedelta(minutes=1),
            updated_at=now - timedelta(days=2),
        )
        out = nudge_engine._select_stalled_workflow_candidates(
            now, lead_hours=24, min_quiet_hours=24, cooldown_hours=24,
        )
        assert out == []

    def test_too_far_in_future_excluded(self) -> None:
        pid = _make_person()
        now = _now()
        _insert_workflow_run(
            run_id="run_e",
            awaiting_person_id=pid,
            awaiting_until=now + timedelta(days=5),
            updated_at=now - timedelta(days=2),
        )
        out = nudge_engine._select_stalled_workflow_candidates(
            now, lead_hours=24, min_quiet_hours=24, cooldown_hours=24,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Stale commitments
# ---------------------------------------------------------------------------

class TestStaleCommitmentCandidates:
    def test_old_awaiting_response_is_candidate(self) -> None:
        now = _now()
        pid = _make_person()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_BOB",
            intent_text="Please confirm Q3 close",
            assigned_to_person_id=pid,
            awaiting_response_since=now - timedelta(days=4),
        )
        out = nudge_engine._select_stale_commitment_candidates(
            now, stale_days=3, cooldown_hours=48,
        )
        assert len(out) == 1
        assert out[0].source == "commitment"
        assert out[0].person_id == pid

    def test_recent_awaiting_excluded(self) -> None:
        now = _now()
        pid = _make_person()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_BOB",
            intent_text="ping",
            assigned_to_person_id=pid,
            awaiting_response_since=now - timedelta(hours=12),
        )
        out = nudge_engine._select_stale_commitment_candidates(
            now, stale_days=3, cooldown_hours=48,
        )
        assert out == []

    def test_nudge_rows_themselves_are_skipped(self) -> None:
        """We never nudge a nudge — would cause an infinite chase loop."""
        now = _now()
        pid = _make_person()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_X",
            intent_text="nudge body",
            kind="proactive_nudge",
            assigned_to_person_id=pid,
            awaiting_response_since=now - timedelta(days=4),
        )
        out = nudge_engine._select_stale_commitment_candidates(
            now, stale_days=3, cooldown_hours=48,
        )
        assert out == []

    def test_null_person_keeps_fallback_channel(self) -> None:
        now = _now()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="telegram",
            channel_ref="123",
            intent_text="ping vendor",
            awaiting_response_since=now - timedelta(days=5),
        )
        out = nudge_engine._select_stale_commitment_candidates(
            now, stale_days=3, cooldown_hours=48,
        )
        assert len(out) == 1
        assert out[0].person_id is None
        assert out[0].fallback_channel == "telegram"
        assert out[0].fallback_channel_ref == "123"


# ---------------------------------------------------------------------------
# Idle initiatives
# ---------------------------------------------------------------------------

class TestIdleInitiativeCandidates:
    def test_idle_initiative_is_candidate(self) -> None:
        dept_store.seed_default_departments()
        pid = _make_person()
        dept_store.update_department("finance", head_person_id=pid)
        dept_registry.invalidate()

        now = _now()
        episodic.store_initiative(
            title="Refinance Series B notes", status="active",
            summary="Long-running treasury project", department="finance",
            db_path=episodic.DB_PATH,
        )
        # Pin updated_at older than the idle window
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            old = (now - timedelta(days=20)).isoformat()
            conn.execute("UPDATE initiatives SET updated_at = ?", (old,))
            conn.commit()

        out = nudge_engine._select_idle_initiative_candidates(
            now, idle_days=7, cooldown_days=7,
        )
        assert len(out) == 1
        assert out[0].source == "initiative"
        assert out[0].person_id == pid
        assert out[0].department == "finance"

    def test_recent_initiative_excluded(self) -> None:
        now = _now()
        episodic.store_initiative(
            title="Recent work", status="active", department="finance",
            db_path=episodic.DB_PATH,
        )
        out = nudge_engine._select_idle_initiative_candidates(
            now, idle_days=7, cooldown_days=7,
        )
        assert out == []

    def test_non_active_status_excluded(self) -> None:
        """Initiatives in paused/cancelled/planned status are NOT chased —
        nudging them with "what's the status?" misreads the user's intent
        (they paused it deliberately).
        """
        dept_store.seed_default_departments()
        pid = _make_person()
        dept_store.update_department("finance", head_person_id=pid)
        dept_registry.invalidate()
        now = _now()
        episodic.store_initiative(
            title="On hold", status="paused", department="finance",
            db_path=episodic.DB_PATH,
        )
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute(
                "UPDATE initiatives SET updated_at = ?",
                ((now - timedelta(days=20)).isoformat(),),
            )
            conn.commit()
        out = nudge_engine._select_idle_initiative_candidates(
            now, idle_days=7, cooldown_days=7,
        )
        assert out == []

    def test_skipped_when_dept_cadence_recent(self) -> None:
        dept_store.seed_default_departments()
        pid = _make_person()
        dept_store.update_department("finance", head_person_id=pid)
        dept_registry.invalidate()

        now = _now()
        episodic.store_initiative(
            title="Project X", status="active", department="finance",
            db_path=episodic.DB_PATH,
        )
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute(
                "UPDATE initiatives SET updated_at = ?",
                ((now - timedelta(days=20)).isoformat(),),
            )
            # Seed a dept_cadence row that fired 2 days ago (within idle window
            # of 7 days), in status 'done'.
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(created_at, run_at, channel, channel_ref, intent_text, "
                "kind, department, status, attempts) "
                "VALUES (?, ?, '__internal__', 'finance', 'check-in', "
                "'dept_cadence', 'finance', 'done', 0)",
                (
                    (now - timedelta(days=2)).isoformat(),
                    (now - timedelta(days=2)).isoformat(),
                ),
            )
            conn.commit()

        out = nudge_engine._select_idle_initiative_candidates(
            now, idle_days=7, cooldown_days=7,
        )
        assert out == []

    def test_unowned_initiative_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Initiative whose department has no head_person_id is skipped."""
        dept_store.seed_default_departments()
        # No head_person_id set on finance.
        now = _now()
        episodic.store_initiative(
            title="Orphan", status="active", department="finance",
            db_path=episodic.DB_PATH,
        )
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute(
                "UPDATE initiatives SET updated_at = ?",
                ((now - timedelta(days=20)).isoformat(),),
            )
            conn.commit()

        out = nudge_engine._select_idle_initiative_candidates(
            now, idle_days=7, cooldown_days=7,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Dedup via scope_key cooldown
# ---------------------------------------------------------------------------

class TestRecentNudgeForScope:
    def test_no_prior_row_returns_false(self) -> None:
        assert not episodic.recent_nudge_for_scope(
            "nudge:stalled:run_xyz", _now() - timedelta(hours=24),
        )

    def test_recent_done_row_returns_true(self) -> None:
        now = _now()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_X",
            intent_text="nudge",
            kind="proactive_nudge",
            scope_key="nudge:stalled:run_xyz",
        )
        # Mark done so we exercise the "done is still inside the cooldown" branch.
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute("UPDATE scheduled_actions SET status = 'done'")
            conn.commit()
        assert episodic.recent_nudge_for_scope(
            "nudge:stalled:run_xyz", now - timedelta(hours=24),
        )

    def test_old_row_outside_cooldown_returns_false(self) -> None:
        now = _now()
        # Insert and then back-date created_at to simulate an old row.
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_X",
            intent_text="nudge",
            kind="proactive_nudge",
            scope_key="nudge:stalled:run_xyz",
        )
        old = (now - timedelta(hours=48)).isoformat()
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute(
                "UPDATE scheduled_actions SET created_at = ?, status = 'done'",
                (old,),
            )
            conn.commit()
        # cooldown = 24h; the row was created 48h ago → outside cooldown.
        assert not episodic.recent_nudge_for_scope(
            "nudge:stalled:run_xyz", now - timedelta(hours=24),
        )

    def test_pending_row_blocks_even_outside_cooldown(self) -> None:
        """An undelivered nudge whose run_at is days away must still block
        re-emission of the same scope_key. This is the deferred-nudge
        case — without it, a scan 25h after a 3-day-deferred nudge would
        emit a duplicate (cooldown=24h, created_at < since)."""
        now = _now()
        episodic.insert_scheduled_action(
            run_at=(now + timedelta(days=3)).isoformat(),
            channel="slack_dm",
            channel_ref="U_X",
            intent_text="deferred nudge",
            kind="proactive_nudge",
            scope_key="nudge:stalled:run_xyz",
        )
        # Back-date created_at to be older than the cooldown window.
        old = (now - timedelta(hours=48)).isoformat()
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute("UPDATE scheduled_actions SET created_at = ?", (old,))
            conn.commit()
        # cooldown 24h → created_at is 48h ago → outside cooldown, BUT row
        # is still pending → must block.
        assert episodic.recent_nudge_for_scope(
            "nudge:stalled:run_xyz", now - timedelta(hours=24),
        )

    def test_cancelled_row_does_not_block_reemit(self) -> None:
        now = _now()
        episodic.insert_scheduled_action(
            run_at=now.isoformat(),
            channel="slack_dm",
            channel_ref="U_X",
            intent_text="nudge",
            kind="proactive_nudge",
            scope_key="nudge:stalled:run_xyz",
        )
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute("UPDATE scheduled_actions SET status = 'cancelled'")
            conn.commit()
        assert not episodic.recent_nudge_for_scope(
            "nudge:stalled:run_xyz", now - timedelta(hours=24),
        )


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------

class TestRouteCandidate:
    def test_immediate_when_reachable(self) -> None:
        pid = _make_person(slack_user_id="U_OK", preferred="slack")
        now = _now()
        cand = nudge_engine.NudgeCandidate(
            scope_key="x", intent_text="hi", source="stalled",
            urgency_seconds=0, cooldown=timedelta(hours=1), person_id=pid,
        )
        routed = nudge_engine._route_candidate(cand, now, max_defer_days=3)
        assert routed is not None
        assert routed.channel == "slack_dm"
        assert routed.channel_ref == "U_OK"
        assert routed.deliver_at == now

    def test_archived_person_returns_none(self) -> None:
        pid = _make_person(preferred="slack")
        with sqlite3.connect(str(people_store.DB_PATH)) as conn:
            conn.execute("UPDATE people SET archived = 1 WHERE id = ?", (pid,))
            conn.commit()
        people_registry.invalidate()
        now = _now()
        cand = nudge_engine.NudgeCandidate(
            scope_key="x", intent_text="hi", source="stalled",
            urgency_seconds=0, cooldown=timedelta(hours=1), person_id=pid,
        )
        assert nudge_engine._route_candidate(cand, now, max_defer_days=3) is None

    def test_no_person_falls_back_to_action_channel(self) -> None:
        now = _now()
        cand = nudge_engine.NudgeCandidate(
            scope_key="x", intent_text="hi", source="commitment",
            urgency_seconds=0, cooldown=timedelta(hours=1),
            fallback_channel="telegram", fallback_channel_ref="123",
        )
        routed = nudge_engine._route_candidate(cand, now, max_defer_days=3)
        assert routed is not None
        assert routed.channel == "telegram"
        assert routed.channel_ref == "123"

    def test_on_leave_with_windows_defers_past_leave_end(self) -> None:
        """On-leave person with availability windows must NOT be deferred
        into the middle of their leave — that creates an infinite
        reschedule loop. The deferred slot must fall after on_leave_until.
        """
        from datetime import date

        from openexecutive.people.models import AvailabilityWindow

        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)  # Wed noon UTC
        leave_until = date(2026, 5, 22)  # Friday — leave covers Wed/Thu/Fri
        pid = people_store.upsert_person(
            full_name="On Leave",
            slack_user_id="U_LEAVE",
            preferred_channel="slack",
            on_leave_until=leave_until,
        )
        people_store.set_availability(
            pid,
            [AvailabilityWindow(
                weekdays=[0, 1, 2, 3, 4],  # Mon-Fri
                start_local="09:00",
                end_local="17:00",
                timezone="UTC",
            )],
        )
        people_registry.invalidate()

        cand = nudge_engine.NudgeCandidate(
            scope_key="x", intent_text="hi", source="stalled",
            urgency_seconds=0, cooldown=timedelta(hours=1), person_id=pid,
        )
        # max_defer_days large enough to encompass leave + next window
        routed = nudge_engine._route_candidate(cand, now, max_defer_days=14)
        assert routed is not None
        # Must defer to STRICTLY AFTER leave_until midnight (so first
        # nudge lands on/after Saturday 00:00 UTC).
        assert routed.deliver_at.date() > leave_until

    def test_on_leave_with_long_leave_returns_none(self) -> None:
        """If leave extends past max_defer_days, drop the nudge entirely."""
        from datetime import date

        from openexecutive.people.models import AvailabilityWindow

        now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
        leave_until = date(2026, 7, 1)  # 6+ weeks out
        pid = people_store.upsert_person(
            full_name="Sabbatical",
            slack_user_id="U_SAB",
            preferred_channel="slack",
            on_leave_until=leave_until,
        )
        people_store.set_availability(
            pid,
            [AvailabilityWindow(
                weekdays=[0, 1, 2, 3, 4], start_local="09:00",
                end_local="17:00", timezone="UTC",
            )],
        )
        people_registry.invalidate()
        cand = nudge_engine.NudgeCandidate(
            scope_key="x", intent_text="hi", source="stalled",
            urgency_seconds=0, cooldown=timedelta(hours=1), person_id=pid,
        )
        # max_defer_days=3 → leave end is 40+ days out → drop.
        routed = nudge_engine._route_candidate(cand, now, max_defer_days=3)
        assert routed is None


# ---------------------------------------------------------------------------
# Per-scan caps
# ---------------------------------------------------------------------------

class TestApplyCaps:
    def test_global_cap_truncates_least_urgent(self) -> None:
        cands = [
            nudge_engine.NudgeCandidate(
                scope_key=f"x{i}", intent_text="", source="stalled",
                urgency_seconds=float(i), cooldown=timedelta(hours=1),
                person_id=100 + i,
            )
            for i in range(20)
        ]
        kept = nudge_engine._apply_caps(cands, max_total=5, max_per_person=2)
        assert len(kept) == 5
        # Most urgent first (urgency_seconds=0..4)
        assert [c.urgency_seconds for c in kept] == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_per_person_cap_honored(self) -> None:
        cands = [
            nudge_engine.NudgeCandidate(
                scope_key=f"x{i}", intent_text="", source="stalled",
                urgency_seconds=float(i), cooldown=timedelta(hours=1),
                person_id=42,  # all candidates target same person
            )
            for i in range(5)
        ]
        kept = nudge_engine._apply_caps(cands, max_total=10, max_per_person=2)
        # 5 candidates for one person, per-person cap 2 → only 2 emitted.
        assert len(kept) == 2

    def test_candidates_without_person_id_unaffected_by_per_person_cap(self) -> None:
        cands = [
            nudge_engine.NudgeCandidate(
                scope_key=f"x{i}", intent_text="", source="commitment",
                urgency_seconds=float(i), cooldown=timedelta(hours=1),
                person_id=None,
                fallback_channel="email", fallback_channel_ref=f"a{i}@x",
            )
            for i in range(5)
        ]
        kept = nudge_engine._apply_caps(cands, max_total=10, max_per_person=2)
        assert len(kept) == 5  # global cap allows all, per-person doesn't apply


# ---------------------------------------------------------------------------
# Full scan integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_nudge_scan_emits_one_proactive_nudge_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    pid = _make_person(slack_user_id="U_ALICE", preferred="slack")
    now = _now()
    _insert_workflow_run(
        run_id="run_main",
        awaiting_person_id=pid,
        awaiting_until=now + timedelta(hours=12),
        updated_at=now - timedelta(days=2),
    )
    emitted = await nudge_engine.run_nudge_scan(now=now)
    assert emitted == 1
    rows = episodic.list_scheduled_actions(status="pending")
    nudge_rows = [r for r in rows if r.kind == "proactive_nudge"]
    assert len(nudge_rows) == 1
    assert nudge_rows[0].channel == "slack_dm"
    assert nudge_rows[0].channel_ref == "U_ALICE"
    assert nudge_rows[0].scope_key == "nudge:stalled:run_main"
    assert nudge_rows[0].department == ""  # bypasses authority gate


@pytest.mark.asyncio
async def test_run_nudge_scan_dedupes_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch)
    pid = _make_person(slack_user_id="U_ALICE", preferred="slack")
    now = _now()
    _insert_workflow_run(
        run_id="run_dup",
        awaiting_person_id=pid,
        awaiting_until=now + timedelta(hours=12),
        updated_at=now - timedelta(days=2),
    )
    first = await nudge_engine.run_nudge_scan(now=now)
    second = await nudge_engine.run_nudge_scan(now=now + timedelta(minutes=15))
    assert first == 1
    assert second == 0


# ---------------------------------------------------------------------------
# Heartbeat bootstrap / chain
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_bootstrap_inserts_one_pending_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_settings(monkeypatch)
        action_id = nudge_engine.bootstrap_nudge_scan()
        assert action_id is not None
        rows = episodic.list_scheduled_actions(status="pending")
        assert len([r for r in rows if r.kind == "nudge_scan"]) == 1

    def test_bootstrap_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_settings(monkeypatch)
        first = nudge_engine.bootstrap_nudge_scan()
        second = nudge_engine.bootstrap_nudge_scan()
        assert first is not None
        assert second is None
        rows = episodic.list_scheduled_actions(status="pending")
        assert len([r for r in rows if r.kind == "nudge_scan"]) == 1

    def test_enqueue_next_chains_forward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_settings(monkeypatch, nudge_scan_interval_minutes=5)
        now = _now()
        action_id = nudge_engine.enqueue_next_scan(after=now)
        assert action_id is not None
        row = episodic.get_scheduled_action(action_id)
        assert row is not None
        run_at = datetime.fromisoformat(row.run_at)
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)
        # Should be ~5 minutes in the future
        delta = run_at - now
        assert timedelta(minutes=4, seconds=30) <= delta <= timedelta(minutes=5, seconds=30)


# ---------------------------------------------------------------------------
# Per-scope cap (alert lifecycle): stop re-chasing the same item forever
# ---------------------------------------------------------------------------

class TestPerScopeCap:
    def test_count_nudges_for_scope_counts_done_only(self) -> None:
        now = _now()
        ids = [
            episodic.insert_scheduled_action(
                run_at=now.isoformat(), channel="slack_dm", channel_ref="U_X",
                intent_text="nudge", kind="proactive_nudge",
                scope_key="nudge:stalled:run_cap", status="pending",
            )
            for _ in range(4)
        ]
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            for aid, status in zip(ids, ("done", "done", "cancelled", "pending"), strict=True):
                conn.execute("UPDATE scheduled_actions SET status = ? WHERE id = ?", (status, aid))
            # A non-nudge row sharing the scope_key (calendar / onboarding
            # namespaces also use scope_key) must not count.
            conn.execute(
                "UPDATE scheduled_actions SET kind = 'ad_hoc' WHERE id = ?", (ids[0],)
            )
            conn.commit()
        assert episodic.count_nudges_for_scope("nudge:stalled:run_cap") == 1
        assert episodic.count_nudges_for_scope("nudge:stalled:other") == 0

    @pytest.mark.asyncio
    async def test_scan_skips_scope_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_settings(monkeypatch, nudge_max_per_scope=2)
        pid = _make_person(slack_user_id="U_ALICE", preferred="slack")
        now = _now()
        _insert_workflow_run(
            run_id="run_capped",
            awaiting_person_id=pid,
            awaiting_until=now + timedelta(hours=12),
            updated_at=now - timedelta(days=2),
        )
        # Two nudges already delivered for this scope, both outside the cooldown.
        for days_ago in (5, 3):
            episodic.insert_scheduled_action(
                run_at=(now - timedelta(days=days_ago)).isoformat(), channel="slack_dm",
                channel_ref="U_ALICE", intent_text="nudge", kind="proactive_nudge",
                scope_key="nudge:stalled:run_capped", status="done",
            )
        with sqlite3.connect(str(episodic.DB_PATH)) as conn:
            conn.execute(
                "UPDATE scheduled_actions SET created_at = ? WHERE status = 'done'",
                ((now - timedelta(days=3)).isoformat(),),
            )
            conn.commit()
        assert await nudge_engine.run_nudge_scan(now=now) == 0

        # Cap disabled → the chase resumes.
        _patch_settings(monkeypatch, nudge_max_per_scope=0)
        assert await nudge_engine.run_nudge_scan(now=now) == 1


# ---------------------------------------------------------------------------
# Idle initiatives with no department head → the principal owns them
# ---------------------------------------------------------------------------


def _idle_initiative(title: str = "Hire a controller", department: str = "") -> int:
    episodic.store_initiative(
        title=title, status="active", summary="", department=department,
        db_path=episodic.DB_PATH,
    )
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute(
            "UPDATE initiatives SET updated_at = ? WHERE title = ?",
            ((_now() - timedelta(days=20)).isoformat(), title),
        )
        row = conn.execute("SELECT id FROM initiatives WHERE title = ?", (title,)).fetchone()
        conn.commit()
    return int(row[0])


def _make_principal(**channels: str) -> int:
    pid = people_store.upsert_person(full_name="Owner", is_principal=True, **channels)  # type: ignore[arg-type]
    people_registry.invalidate()
    return pid


class TestInitiativeOwnerFallback:
    def test_departmentless_initiative_goes_to_the_principal(self) -> None:
        principal = _make_principal(slack_user_id="U_OWNER", preferred_channel="slack")
        _idle_initiative()

        (cand,) = nudge_engine._select_idle_initiative_candidates(
            _now(), idle_days=7, cooldown_days=7,
        )
        assert cand.person_id == principal
        assert cand.department == ""
        # Self-addressed: the principal is reminded of their own initiative.
        assert cand.intent_text.startswith("Remind the principal")
        assert '"Hire a controller"' in cand.intent_text
        assert "still active, done, or dropped" in cand.intent_text
        assert "Do not call schedule_followup." in cand.intent_text
        assert cand.unreachable_card is not None

    def test_unknown_department_falls_back_to_the_principal(self) -> None:
        principal = _make_principal(slack_user_id="U_OWNER", preferred_channel="slack")
        _idle_initiative(department="ghost")

        (cand,) = nudge_engine._select_idle_initiative_candidates(
            _now(), idle_days=7, cooldown_days=7,
        )
        assert cand.person_id == principal

    def test_department_head_still_wins(self) -> None:
        dept_store.seed_default_departments()
        _make_principal(slack_user_id="U_OWNER", preferred_channel="slack")
        head = _make_person()
        dept_store.update_department("finance", head_person_id=head)
        dept_registry.invalidate()
        _idle_initiative(department="finance")

        (cand,) = nudge_engine._select_idle_initiative_candidates(
            _now(), idle_days=7, cooldown_days=7,
        )
        assert cand.person_id == head
        assert cand.intent_text.startswith("Check in on the active initiative")
        assert cand.unreachable_card is None  # only the principal gets a card

    def test_head_who_is_the_principal_gets_the_self_addressed_wording(self) -> None:
        dept_store.seed_default_departments()
        principal = _make_principal(slack_user_id="U_OWNER", preferred_channel="slack")
        dept_store.update_department("finance", head_person_id=principal)
        dept_registry.invalidate()
        _idle_initiative(department="finance")

        (cand,) = nudge_engine._select_idle_initiative_candidates(
            _now(), idle_days=7, cooldown_days=7,
        )
        assert cand.person_id == principal
        assert cand.intent_text.startswith("Remind the principal")

    def test_no_principal_skips(self) -> None:
        _idle_initiative()
        assert nudge_engine._select_idle_initiative_candidates(
            _now(), idle_days=7, cooldown_days=7,
        ) == []


class TestInitiativeNudgeToThePrincipal:
    @pytest.fixture(autouse=True)
    def _alerts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from openexecutive.alerts import store as alert_store

        monkeypatch.setattr(alert_store, "DB_PATH", episodic.DB_PATH)
        alert_store.initialize_db(episodic.DB_PATH)

    @pytest.mark.asyncio
    async def test_reachable_principal_gets_a_nudge_row(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_settings(monkeypatch)
        _make_principal(slack_user_id="U_OWNER", preferred_channel="slack")
        initiative_id = _idle_initiative()

        assert await nudge_engine.run_nudge_scan(now=_now()) == 1
        (row,) = [
            r for r in episodic.list_scheduled_actions(status="pending")
            if r.kind == "proactive_nudge"
        ]
        assert (row.channel, row.channel_ref) == ("slack_dm", "U_OWNER")
        assert row.scope_key == f"nudge:initiative:{initiative_id}"
        assert row.department == ""  # bypasses the authority gate

    @pytest.mark.asyncio
    async def test_unreachable_principal_gets_one_briefing_card_a_week(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from openexecutive.alerts import store as alert_store

        _patch_settings(monkeypatch)
        _make_principal()  # web-only: no chat id, no email
        _idle_initiative()
        now = _now()

        assert await nudge_engine.run_nudge_scan(now=now) == 0
        (card,) = alert_store.list_alerts()
        assert card.routed_to_person_id is None  # unrouted → the principal's queue
        assert "initiative" in card.topic_tags
        # No empty "department:" tag: "mute this topic" would pick it, and a
        # mute substring-matches every department card's tags.
        assert not any(t.startswith("department:") for t in card.topic_tags)
        assert '"Hire a controller"' in card.headline
        # Neutral: the principal may simply be on leave, not channel-less.
        assert "couldn't reach you" in card.body
        assert not episodic.list_scheduled_actions(status="pending")

        # The scan runs every 15 minutes: the open card is left alone, not
        # refreshed (a coalesce would bump its "seen ×N" each pass).
        await nudge_engine.run_nudge_scan(now=now + timedelta(minutes=15))
        (same,) = alert_store.list_alerts()
        assert same.id == card.id and same.occurrence_count == card.occurrence_count

        # Handled this week → nothing new until next week.
        assert card.id is not None
        alert_store.set_status(card.id, "ack")
        await nudge_engine.run_nudge_scan(now=now + timedelta(hours=1))
        assert len(alert_store.list_alerts()) == 1
        await nudge_engine.run_nudge_scan(now=now + timedelta(days=7))
        assert sorted(a.status for a in alert_store.list_alerts()) == ["ack", "unread"]


@pytest.mark.asyncio
async def test_a_skipped_check_in_does_not_suppress_the_departments_initiative_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeded departments have no Goals, so their daily check-in is skipped.
    A skipped check-in covered nothing: an idle initiative tagged to that
    department must still be chased (a done cadence row would suppress it)."""
    from openexecutive.scheduler import runner

    _patch_settings(monkeypatch)
    dept_store.seed_default_departments()
    head = _make_person(slack_user_id="U_HEAD", preferred="slack")
    dept_store.update_department("finance", head_person_id=head)
    dept_registry.invalidate()
    initiative_id = _idle_initiative(department="finance")

    episodic.insert_scheduled_action(
        run_at=(_now() - timedelta(seconds=10)).isoformat(), channel="__internal__",
        channel_ref="finance", intent_text="Department check-in: Finance",
        department="finance", kind="dept_cadence",
    )
    (cadence,) = episodic.claim_due_actions(_now())
    await runner._execute_action(cadence, None)
    assert cadence.id is not None
    fired = episodic.get_scheduled_action(cadence.id)
    assert fired is not None and fired.status == "cancelled"

    assert await nudge_engine.run_nudge_scan(now=_now()) == 1
    (nudge,) = [
        r for r in episodic.list_scheduled_actions(status="pending")
        if r.kind == "proactive_nudge"
    ]
    assert nudge.scope_key == f"nudge:initiative:{initiative_id}"
    assert nudge.channel_ref == "U_HEAD"


@pytest.mark.asyncio
async def test_unreachable_card_goes_to_the_scans_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_nudge_scan(db_path=...)` files the card in that database, not in
    the alerts store's default one."""
    from openexecutive.alerts import store as alert_store

    decoy = tmp_path / "decoy.db"
    monkeypatch.setattr(alert_store, "DB_PATH", decoy)
    alert_store.initialize_db(decoy)
    alert_store.initialize_db(episodic.DB_PATH)
    _patch_settings(monkeypatch)
    _make_principal()  # web-only
    _idle_initiative()

    await nudge_engine.run_nudge_scan(now=_now(), db_path=episodic.DB_PATH)

    assert alert_store.list_alerts(db_path=decoy) == []
    (card,) = alert_store.list_alerts(db_path=episodic.DB_PATH)
    assert "couldn't reach you" in card.body
