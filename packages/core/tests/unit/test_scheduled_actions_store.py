"""Unit tests for the scheduled_actions table CRUD helpers in episodic memory."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory.episodic import (
    cancel_orphaned_talent_reminders,
    cancel_scheduled_action,
    claim_due_actions,
    count_pending_for_channel_ref,
    count_pending_global,
    get_scheduled_action,
    initialize_db,
    insert_scheduled_action,
    last_contact_at_by_person,
    list_awaiting_replies_by_person,
    list_pending_scheduled_actions,
    list_scheduled_actions,
    mark_action_done,
    mark_action_failed_or_retry,
    requeue_orphaned_running,
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    return db_path


def _future(seconds: int = 60) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past(seconds: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_list_order_asc_default_then_desc(db: Path) -> None:
    """`order` flips run_at sort: asc (default) soonest-first, desc recent-first.

    desc is what a terminal-status view wants so recent rows aren't pushed past
    the limit by old ones."""
    early = insert_scheduled_action(
        run_at=_past(300), channel="slack_dm", channel_ref="a",
        intent_text="early", db_path=db,
    )
    late = insert_scheduled_action(
        run_at=_past(60), channel="slack_dm", channel_ref="b",
        intent_text="late", db_path=db,
    )

    asc = [a.id for a in list_scheduled_actions(status="pending", db_path=db)]
    assert asc.index(early) < asc.index(late)  # default asc: earliest run_at first

    desc = [
        a.id
        for a in list_scheduled_actions(status="pending", order="desc", db_path=db)
    ]
    assert desc.index(late) < desc.index(early)  # desc: most recent run_at first


def test_list_order_desc_with_limit_keeps_recent(db: Path) -> None:
    """desc + limit returns the most-recent rows, not the oldest."""
    oldest = insert_scheduled_action(
        run_at=_past(900), channel="slack_dm", channel_ref="o",
        intent_text="oldest", db_path=db,
    )
    newest = insert_scheduled_action(
        run_at=_past(10), channel="slack_dm", channel_ref="n",
        intent_text="newest", db_path=db,
    )
    one = list_scheduled_actions(status="pending", limit=1, order="desc", db_path=db)
    assert [a.id for a in one] == [newest]
    assert oldest not in [a.id for a in one]


def test_insert_and_get_roundtrip(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="12345",
        intent_text="Follow up on the board memo",
        db_path=db,
    )
    assert action_id > 0
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert row.channel == "telegram"
    assert row.channel_ref == "12345"
    assert row.status == "pending"
    assert row.attempts == 0


def test_insert_rejects_unknown_channel(db: Path) -> None:
    with pytest.raises(ValueError):
        insert_scheduled_action(
            run_at=_future(),
            channel="carrier_pigeon",
            channel_ref="x",
            intent_text="nope",
            db_path=db,
        )


def test_claim_due_actions_only_returns_past_run_at(db: Path) -> None:
    due_id = insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="due now",
        db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(3600),
        channel="telegram",
        channel_ref="2",
        intent_text="far future",
        db_path=db,
    )

    claimed = claim_due_actions(datetime.now(UTC), db_path=db)
    assert [a.id for a in claimed] == [due_id]
    assert claimed[0].status == "running"
    assert claimed[0].attempts == 1


def test_claim_is_idempotent_running_rows_not_reclaimed(db: Path) -> None:
    insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="due",
        db_path=db,
    )
    first = claim_due_actions(datetime.now(UTC), db_path=db)
    second = claim_due_actions(datetime.now(UTC), db_path=db)
    assert len(first) == 1
    assert second == []  # status now 'running', no longer pending


def test_mark_done(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    claim_due_actions(datetime.now(UTC), db_path=db)
    assert mark_action_done(action_id, db_path=db) is True
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert row.status == "done"


def test_failed_or_retry_reschedules_then_gives_up(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    # Each claim bumps attempts; failure handler decides what to do based on attempts.
    claim_due_actions(datetime.now(UTC), db_path=db)  # attempts=1
    assert mark_action_failed_or_retry(action_id, "boom", db_path=db) == "pending"
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert row.status == "pending"
    assert row.last_error == "boom"

    # Manually flip back to pending+past to allow a second claim.
    from openexecutive.memory.episodic import _get_conn  # type: ignore[attr-defined]

    with _get_conn(db) as conn:
        conn.execute(
            "UPDATE scheduled_actions SET status='pending', run_at=? WHERE id=?",
            (_past(), action_id),
        )
    claim_due_actions(datetime.now(UTC), db_path=db)  # attempts=2
    assert mark_action_failed_or_retry(action_id, "boom2", db_path=db) == "pending"
    with _get_conn(db) as conn:
        conn.execute(
            "UPDATE scheduled_actions SET status='pending', run_at=? WHERE id=?",
            (_past(), action_id),
        )
    claim_due_actions(datetime.now(UTC), db_path=db)  # attempts=3
    assert mark_action_failed_or_retry(action_id, "boom3", db_path=db) == "failed"
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert row.status == "failed"


def test_cancel_pending_action(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    assert cancel_scheduled_action(action_id, db_path=db) == "cancelled"
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert row.status == "cancelled"


def test_cancel_running_action_refused(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    claim_due_actions(datetime.now(UTC), db_path=db)
    assert cancel_scheduled_action(action_id, db_path=db) == "not_cancellable"


def test_cancel_missing_returns_not_found(db: Path) -> None:
    assert cancel_scheduled_action(9999, db_path=db) == "not_found"


def test_count_pending_for_channel_ref(db: Path) -> None:
    insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="A",
        intent_text="x",
        db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="A",
        intent_text="y",
        db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="B",
        intent_text="z",
        db_path=db,
    )
    assert count_pending_for_channel_ref("telegram", "A", db_path=db) == 2
    assert count_pending_for_channel_ref("telegram", "B", db_path=db) == 1
    assert count_pending_for_channel_ref("email", "A", db_path=db) == 0


def test_requeue_orphaned_running(db: Path) -> None:
    action_id = insert_scheduled_action(
        run_at=_past(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    claim_due_actions(datetime.now(UTC), db_path=db)
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None and row.status == "running"

    assert requeue_orphaned_running(db_path=db) == 1
    row2 = get_scheduled_action(action_id, db_path=db)
    assert row2 is not None and row2.status == "pending"


def test_insert_truncates_overlong_intent(db: Path) -> None:
    huge = "x" * 5000
    action_id = insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="1",
        intent_text=huge,
        db_path=db,
    )
    row = get_scheduled_action(action_id, db_path=db)
    assert row is not None
    assert len(row.intent_text) == 2000


def test_count_pending_global(db: Path) -> None:
    for i in range(3):
        insert_scheduled_action(
            run_at=_future(60 + i),
            channel="telegram",
            channel_ref=str(i),
            intent_text="t",
            db_path=db,
        )
    assert count_pending_global(db_path=db) == 3


def test_list_filters_by_status(db: Path) -> None:
    a = insert_scheduled_action(
        run_at=_future(),
        channel="telegram",
        channel_ref="1",
        intent_text="t",
        db_path=db,
    )
    b = insert_scheduled_action(
        run_at=_future(120),
        channel="telegram",
        channel_ref="2",
        intent_text="u",
        db_path=db,
    )
    cancel_scheduled_action(b, db_path=db)
    pending = list_scheduled_actions(status="pending", db_path=db)
    cancelled = list_scheduled_actions(status="cancelled", db_path=db)
    assert {row.id for row in pending} == {a}
    assert {row.id for row in cancelled} == {b}


# --------------------------------------------------------------------------- #
# Per-person awaiting-reply + last-contact aggregations (brief enrichment)
# --------------------------------------------------------------------------- #

def test_list_awaiting_replies_by_person_counts_and_oldest(db: Path) -> None:
    older = datetime.now(UTC) - timedelta(hours=10)
    newer = datetime.now(UTC) - timedelta(hours=2)

    # Person 1: two open commitments (one pending, one done) awaiting reply.
    insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="ping 1", assigned_to_person_id=1,
        awaiting_response_since=older, db_path=db,
    )
    done_id = insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="ping 2", assigned_to_person_id=1,
        awaiting_response_since=newer, db_path=db,
    )
    assert mark_action_done(done_id, db_path=db)

    # Person 2: one awaiting reply.
    insert_scheduled_action(
        run_at=_past(), channel="telegram", channel_ref="U2",
        intent_text="ping", assigned_to_person_id=2,
        awaiting_response_since=newer, db_path=db,
    )
    # Excluded: a proactive_nudge follow-up (chases, doesn't count as awaited).
    insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="nudge", assigned_to_person_id=1, kind="proactive_nudge",
        awaiting_response_since=older, scope_key="nudge:x:1", db_path=db,
    )
    # Excluded: no awaiting_response_since.
    insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="fyi", assigned_to_person_id=1, db_path=db,
    )
    # Excluded: not assigned to any person.
    insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="x@co.com",
        intent_text="unassigned", awaiting_response_since=older, db_path=db,
    )

    result = list_awaiting_replies_by_person(db_path=db)
    assert result[1][0] == 2  # count
    assert result[1][1] == older.isoformat()  # oldest awaiting
    assert result[2][0] == 1
    assert result[2][1] == newer.isoformat()


def test_list_awaiting_replies_empty_db_returns_empty(tmp_path: Path) -> None:
    assert list_awaiting_replies_by_person(db_path=tmp_path / "missing.db") == {}


def test_last_contact_at_by_person_uses_max_done(db: Path) -> None:
    import time

    first = insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="first", assigned_to_person_id=1, db_path=db,
    )
    assert mark_action_done(first, db_path=db)
    second = insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="second", assigned_to_person_id=1, db_path=db,
    )
    assert mark_action_done(second, db_path=db)
    # A still-pending action is inserted LAST (so it has the most recent
    # created_at) but must NOT count as contact — contact = done outbound.
    time.sleep(0.01)
    pending = insert_scheduled_action(
        run_at=_past(), channel="email", channel_ref="a@co.com",
        intent_text="pending", assigned_to_person_id=1, db_path=db,
    )

    result = last_contact_at_by_person(db_path=db)
    rows = {a: get_scheduled_action(a, db_path=db) for a in (first, second, pending)}
    done_max = max(rows[first].created_at, rows[second].created_at)  # type: ignore[union-attr]
    assert result[1] == done_max
    assert result[1] != rows[pending].created_at  # pending excluded  # type: ignore[union-attr]


def test_last_contact_at_empty_db_returns_empty(tmp_path: Path) -> None:
    assert last_contact_at_by_person(db_path=tmp_path / "missing.db") == {}


def test_list_pending_scheduled_actions_excludes_internal(db: Path) -> None:
    """User-facing pending actions only — __internal__ channel and nudge_scan
    heartbeat are filtered; results ordered soonest-first."""
    insert_scheduled_action(
        run_at=_future(120), channel="email", channel_ref="x@y.com",
        intent_text="later follow-up", kind="ad_hoc", db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(30), channel="slack_dm", channel_ref="U1",
        intent_text="sooner nudge", kind="proactive_nudge", db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(10), channel="__internal__", channel_ref="scan",
        intent_text="internal cadence", kind="dept_cadence", db_path=db,
    )
    insert_scheduled_action(
        run_at=_future(5), channel="__internal__", channel_ref="hb",
        intent_text="heartbeat", kind="nudge_scan", db_path=db,
    )

    # nudge_scan on a NON-internal channel: must be dropped by the kind
    # filter alone (guards the kind filter independently of the channel one).
    insert_scheduled_action(
        run_at=_future(20), channel="slack_dm", channel_ref="U9",
        intent_text="heartbeat on a real channel", kind="nudge_scan", db_path=db,
    )

    rows = list_pending_scheduled_actions(db_path=db)
    intents = [r.intent_text for r in rows]
    assert intents == ["sooner nudge", "later follow-up"]  # soonest-first, internals dropped
    assert "heartbeat on a real channel" not in intents


def test_list_pending_scheduled_actions_excludes_done(db: Path) -> None:
    aid = insert_scheduled_action(
        run_at=_future(60), channel="email", channel_ref="x@y.com",
        intent_text="will complete", kind="ad_hoc", db_path=db,
    )
    mark_action_done(aid, db_path=db)
    assert list_pending_scheduled_actions(db_path=db) == []


# --------------------------------------------------------------------------- #
# One-shot sweep for reminders the removed talent / staff-onboarding
# workflows left on the principal's DM channel.
# --------------------------------------------------------------------------- #

# Literal intent texts as the deleted generators produced them (pre-removal
# commit d160d3d: workflows/candidate_outreach.py, interview_coordination.py,
# reference_check.py, new_hire_onboarding.py, talent/offers.py). The sweep
# must match THESE, not strings derived from its own patterns.
_ORPHAN_INTENTS = [
    "Outreach reminder 1/3 (Day 0) for the VP Operations search (Acme). Send the "
    "principal this ready-to-send draft for Jane Doe so they can review and send it "
    "themselves. Do NOT contact the candidate directly.\n\nHi Jane…",
    "Interview coordination for Jane Doe on the VP Operations search (Acme). Send the "
    "principal the proposed interview loop below and the ready-to-send availability "
    "request so they can line up the panel and send the request to the candidate.",
    "Reference checks (3) for Jane Doe on the VP Operations search (Acme). Send the "
    "principal the rubric below and the ready-to-send reference-outreach so they can "
    "line up and run the references themselves.",
    "Onboarding check-in (day 30) for Jane Doe, VP Operations at Acme. DM the "
    "principal the day-30 milestone questions from the onboarding plan so they can "
    "run the check-in themselves.",
    "Offer expiry reminder: the offer to Jane Doe for the VP Operations role "
    "(offer 12) expires in 3 days. DM the principal so they can check in with the "
    "candidate and either record a decision (accepted / declined / expired) or "
    "re-extend.",
]

# Principal-authored follow-ups that share the opening words — must survive.
_NEAR_MISS_INTENTS = [
    "Outreach reminder: chase the Series A investor list before Friday",
    "Interview coordination for the podcast guest — confirm the studio slot",
    "Reference checks (informal) on the new agency before we sign",
    "Onboarding check-in (day 1) with the new fractional CFO about access",
    "Offer expiry reminder: the vendor's discounted quote lapses Monday",
]


def _insert_reminder(
    db: Path, text: str, *, status: str = "pending", kind: str = "ad_hoc",
    department: str = "", awaiting: bool = False,
) -> int:
    aid = insert_scheduled_action(
        run_at=_future(3600), channel="telegram", channel_ref="123",
        intent_text=text, kind=kind, department=department, db_path=db,
    )
    if status == "running":
        # Simulate a crash mid-dispatch: claimed (running) but never finished.
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE scheduled_actions SET status='running' WHERE id=?", (aid,))
    elif status == "done":
        mark_action_done(aid, db_path=db)
    if awaiting:
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE scheduled_actions SET awaiting_response_since=? WHERE id=?",
                (_past(3600), aid),
            )
    return aid


def _status(db: Path, aid: int) -> str:
    row = get_scheduled_action(aid, db)
    assert row is not None
    return row.status


def test_orphan_sweep_cancels_the_five_generated_shapes_and_nothing_else(db: Path) -> None:
    orphans = [_insert_reminder(db, t) for t in _ORPHAN_INTENTS]
    near_misses = [_insert_reminder(db, t) for t in _NEAR_MISS_INTENTS]
    unrelated = _insert_reminder(db, "Follow up with the board candidate on the Q3 deck")
    delivered = _insert_reminder(db, _ORPHAN_INTENTS[0], status="done")
    # Same text, but a department-scoped or non-ad-hoc row is not a talent
    # reminder (those never set a department and were always ad_hoc).
    dept_scoped = _insert_reminder(db, _ORPHAN_INTENTS[0], department="hr")
    other_kind = _insert_reminder(db, _ORPHAN_INTENTS[0], kind="proactive_nudge")

    assert cancel_orphaned_talent_reminders(db) == len(_ORPHAN_INTENTS)

    for aid in orphans:
        row = get_scheduled_action(aid, db)
        assert row is not None and row.status == "cancelled"
        assert "feature removed" in row.last_error
    for aid in near_misses + [unrelated, dept_scoped, other_kind]:
        assert _status(db, aid) == "pending"
    assert _status(db, delivered) == "done"


def test_orphan_sweep_includes_running_rows(db: Path) -> None:
    """A row left `running` by a crash mid-dispatch is reclaimed to `pending`
    by the scheduler AFTER this sweep runs at boot — so the sweep must take it
    too, or it comes back from the dead."""
    stuck = _insert_reminder(db, _ORPHAN_INTENTS[1], status="running")
    assert cancel_orphaned_talent_reminders(db) == 1
    assert _status(db, stuck) == "cancelled"


def test_orphan_sweep_clears_awaiting_flag_on_delivered_reminders(db: Path) -> None:
    """Delivered talent reminders still flagged as awaiting a reply would keep
    feeding the nudge engine; the flag goes, the history row stays."""
    delivered = _insert_reminder(db, _ORPHAN_INTENTS[2], status="done", awaiting=True)
    keep = _insert_reminder(db, _NEAR_MISS_INTENTS[2], status="done", awaiting=True)
    assert cancel_orphaned_talent_reminders(db) == 0  # nothing pending to cancel
    swept = get_scheduled_action(delivered, db)
    assert swept is not None and swept.status == "done"
    assert swept.awaiting_response_since is None
    kept = get_scheduled_action(keep, db)
    assert kept is not None and kept.awaiting_response_since is not None


def test_orphan_sweep_runs_once_per_db(db: Path) -> None:
    """The marker row bounds the sweep: a matching row inserted AFTER the first
    run is left alone, proving later boots do no work."""
    first = _insert_reminder(db, _ORPHAN_INTENTS[0])
    assert cancel_orphaned_talent_reminders(db) == 1

    late = _insert_reminder(db, _ORPHAN_INTENTS[0])
    assert cancel_orphaned_talent_reminders(db) == 0
    assert _status(db, late) == "pending"
    assert _status(db, first) == "cancelled"


def test_orphan_sweep_marker_is_claimed_atomically(db: Path) -> None:
    """Two boots against one DB: the marker is claimed with INSERT OR IGNORE in
    the sweep's own transaction, so a pre-existing marker means a clean no-op —
    never an IntegrityError."""
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO app_migrations (name, applied_at) VALUES (?, ?)",
            ("2026-09-cancel-talent-reminders", "2026-09-17T00:00:00+00:00"),
        )
    orphan = _insert_reminder(db, _ORPHAN_INTENTS[0])
    assert cancel_orphaned_talent_reminders(db) == 0
    assert _status(db, orphan) == "pending"


def test_orphan_sweep_is_a_noop_on_a_clean_db(db: Path) -> None:
    assert cancel_orphaned_talent_reminders(db) == 0
    assert cancel_orphaned_talent_reminders(db) == 0
