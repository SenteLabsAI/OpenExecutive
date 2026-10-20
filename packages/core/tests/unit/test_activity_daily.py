"""Tests for the Pulse heartbeat data: episodic.count_activity_by_day and the
GET /today/activity/daily route that densifies it."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import today as today_route
from openexecutive.memory import decision_ledger, episodic
from openexecutive.workflows import persistence as wf_persistence


def _iso(days_ago: int) -> str:
    """ISO timestamp `days_ago` days before now (UTC)."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _seed_db(db: Path) -> None:
    episodic.initialize_db(db)


# --------------------------------------------------------------------------- #
# Store: count_activity_by_day
# --------------------------------------------------------------------------- #


def test_count_by_day_missing_db_returns_empty(tmp_path: Path) -> None:
    assert episodic.count_activity_by_day(30, db_path=tmp_path / "nope.db") == []


def test_count_by_day_buckets_across_sources(tmp_path: Path) -> None:
    db = tmp_path / "act.db"
    _seed_db(db)
    today = datetime.now(UTC).date().isoformat()

    # Two decisions + one advice today → 3 on today's bucket.
    episodic.store_decision("finance", "Cut burn", db_path=db)
    episodic.store_decision("strategy", "Enter EU", db_path=db)
    episodic.store_advice("hr", "How to hire?", "Hire slowly", db_path=db)

    # A fired (done) scheduled action today → +1 today.
    aid = episodic.insert_scheduled_action(
        run_at=_iso(0), channel="slack_dm", channel_ref="alice",
        intent_text="DM'd Alice", kind="ad_hoc", db_path=db,
    )
    assert episodic.mark_action_done(aid, db_path=db)

    by_day = dict(episodic.count_activity_by_day(30, db_path=db))
    assert by_day.get(today) == 4


def test_count_by_day_excludes_internal_and_nudge_scan(tmp_path: Path) -> None:
    db = tmp_path / "excl.db"
    _seed_db(db)
    today = datetime.now(UTC).date().isoformat()

    scan = episodic.insert_scheduled_action(
        run_at=_iso(0), channel="__internal__", channel_ref="-",
        intent_text="nudge scan tick", kind="nudge_scan", db_path=db,
    )
    internal = episodic.insert_scheduled_action(
        run_at=_iso(0), channel="__internal__", channel_ref="-",
        intent_text="internal cadence", kind="dept_cadence", db_path=db,
    )
    real = episodic.insert_scheduled_action(
        run_at=_iso(0), channel="slack_dm", channel_ref="alice",
        intent_text="Real DM", kind="ad_hoc", db_path=db,
    )
    for aid in (scan, internal, real):
        assert episodic.mark_action_done(aid, db_path=db)

    by_day = dict(episodic.count_activity_by_day(30, db_path=db))
    # Only the real DM counts — internal + nudge_scan are excluded.
    assert by_day.get(today) == 1


def test_count_by_day_includes_broadened_sources(tmp_path: Path) -> None:
    """The heatmap counts the same broadened set as the feed: a completed
    workflow run, an initiative, a resolved decision, and a raised alert each
    add to today's bucket (the workflow_runs/alerts tables are owned by other
    stores, so they must be initialised for their arms to count)."""
    db = tmp_path / "broad.db"
    _seed_db(db)
    wf_persistence.initialize_runs_db(db)
    alert_store.initialize_db(db)
    today = datetime.now(UTC).date().isoformat()

    wf_persistence.create_run("r1", "morning_brief", "Brief", {}, db_path=db)
    wf_persistence.complete_run("r1", artifact="body", db_path=db)
    episodic.store_initiative(title="New initiative", status="active", db_path=db)
    iid = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="ops",
        originating_session_id=None, proposed_payload={"summary": "x"},
        idempotency_key="k1", gate_mode="propose",
        approver_person_id=None, confidence=0.9, db_path=db,
    )
    decision_ledger.mark_resolved(iid, decision_ledger.STATUS_REJECTED, db_path=db)
    alert_store.insert_alert(
        source="triage", external_id="al1", severity="high",
        headline="Signal", body="x", db_path=db,
    )

    by_day = dict(episodic.count_activity_by_day(30, db_path=db))
    # workflow_done + initiative + decision_resolved + alert = 4 today.
    assert by_day.get(today) == 4


def test_count_by_day_excludes_decision_scheduling_alert(tmp_path: Path) -> None:
    """A gated booking's companion alert (source='decision_scheduling') must NOT
    add to the heatmap — the resolved decision_instance already counts it, so
    counting the alert too would double-count vs the feed (which excludes it)."""
    db = tmp_path / "dsalert.db"
    _seed_db(db)
    alert_store.initialize_db(db)
    today = datetime.now(UTC).date().isoformat()

    iid = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="ops",
        originating_session_id=None, proposed_payload={"summary": "Book sync"},
        idempotency_key="k1", gate_mode="propose",
        approver_person_id=None, confidence=0.9, db_path=db,
    )
    decision_ledger.mark_resolved(iid, decision_ledger.STATUS_APPROVED_UNCHANGED, db_path=db)
    alert_store.insert_alert(
        source=decision_ledger.DECISION_ALERT_SOURCE, external_id="decision:1",
        severity="medium", headline="Approve meeting", body="x",
        topic_tags=["decision_instance:1"], db_path=db,
    )

    by_day = dict(episodic.count_activity_by_day(30, db_path=db))
    # Only the resolved decision counts (1) — the companion alert is excluded.
    assert by_day.get(today) == 1


def test_count_by_day_leaves_out_private_rows(tmp_path: Path) -> None:
    """Rows private to the principal stay out of everyone's heatmap, as they
    stay out of the feed: a decision whose payload is private, an alert
    tagged private (case aside)."""
    db = tmp_path / "private.db"
    _seed_db(db)
    alert_store.initialize_db(db)
    today = datetime.now(UTC).date().isoformat()

    for key, payload in (
        ("shared", {"summary": "Book sync"}),
        ("private", {"summary": "Reply to a contact", "private": True}),
    ):
        iid = decision_ledger.create_decision_instance(
            decision_class="meeting_scheduling", department="ops",
            originating_session_id=None, proposed_payload=payload,
            idempotency_key=key, gate_mode="propose",
            approver_person_id=None, confidence=0.9, db_path=db,
        )
        decision_ledger.mark_resolved(iid, decision_ledger.STATUS_REJECTED, db_path=db)
    alert_store.insert_alert(
        source="triage", external_id="al1", severity="high",
        headline="Signal", body="x", db_path=db,
    )
    alert_store.insert_alert(
        source="triage", external_id="al2", severity="high",
        headline="A contact wrote", body="x", topic_tags=["Private:Principal"], db_path=db,
    )

    by_day = dict(episodic.count_activity_by_day(30, db_path=db))
    # The shared decision and the shared alert only.
    assert by_day.get(today) == 2


def test_count_by_day_excludes_pending_actions(tmp_path: Path) -> None:
    """Pending (not-yet-fired) actions are not activity — only status='done'."""
    db = tmp_path / "pending.db"
    _seed_db(db)
    episodic.insert_scheduled_action(
        run_at=_iso(0), channel="slack_dm", channel_ref="alice",
        intent_text="not fired yet", kind="ad_hoc", db_path=db,
    )
    assert episodic.count_activity_by_day(30, db_path=db) == []


def test_count_by_day_buckets_fired_actions_by_run_at(tmp_path: Path) -> None:
    """A cadence queued long ago but fired today counts TODAY, not at queue time.

    Guards the run_at (fire time) vs created_at (queue time) choice: bucketing
    by created_at would drop this row (queued 40d ago, outside a 7d window) and
    make "Beats today" read 0 on a day the Executive actually acted.
    """
    db = tmp_path / "runat.db"
    _seed_db(db)
    today = datetime.now(UTC).date().isoformat()
    # created_at = 40 days ago (outside the window), run_at = today, done.
    with episodic._get_conn(db) as conn:
        conn.execute(
            "INSERT INTO scheduled_actions "
            "(created_at, run_at, channel, channel_ref, intent_text, status, kind) "
            "VALUES (?, ?, 'slack_dm', 'alice', 'Morning brief', 'done', 'principal_brief_morning')",
            (_iso(40), _iso(0)),
        )
    by_day = dict(episodic.count_activity_by_day(7, db_path=db))
    assert by_day.get(today) == 1


def test_count_by_day_respects_window(tmp_path: Path) -> None:
    db = tmp_path / "window.db"
    _seed_db(db)
    # Decision 100 days ago must fall outside a 30-day window.
    db_conn_seed = episodic
    with db_conn_seed._get_conn(db) as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary) VALUES (?, ?, ?)",
            (_iso(100), "finance", "Old decision"),
        )
    assert episodic.count_activity_by_day(30, db_path=db) == []
    # But a 200-day window picks it up.
    assert len(episodic.count_activity_by_day(200, db_path=db)) == 1


# --------------------------------------------------------------------------- #
# Route: GET /today/activity/daily
# --------------------------------------------------------------------------- #


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(today_route.router)
    return TestClient(app)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "daily.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    return _make_client()


def test_daily_route_is_dense(client: TestClient) -> None:
    resp = client.get("/today/activity/daily?days=30")
    assert resp.status_code == 200
    days = resp.json()["days"]
    assert len(days) == 30
    # Oldest → newest, no gaps, last entry is today.
    dates = [d["date"] for d in days]
    assert dates == sorted(dates)
    assert dates[-1] == datetime.now(UTC).date().isoformat()
    # Empty store → every count is 0.
    assert all(d["count"] == 0 for d in days)


def test_daily_route_reflects_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "counts.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    episodic.store_decision("finance", "Cut burn", db_path=db)
    episodic.store_advice("hr", "Hire?", "Slowly", db_path=db)

    days = _make_client().get("/today/activity/daily?days=14").json()["days"]
    today = datetime.now(UTC).date().isoformat()
    by_date = {d["date"]: d["count"] for d in days}
    assert by_date[today] == 2


def test_daily_route_fills_gaps_with_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The densify claim: a single active day surrounded by zero-filled days."""
    db = tmp_path / "gaps.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    # One decision dated 5 days ago; everything else must be present at count 0.
    with episodic._get_conn(db) as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary) VALUES (?, ?, ?)",
            (_iso(5), "finance", "Five days ago"),
        )

    days = _make_client().get("/today/activity/daily?days=10").json()["days"]
    assert len(days) == 10
    by_date = {d["date"]: d["count"] for d in days}
    target = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()
    assert by_date[target] == 1
    # Every other day in the window is present and zero — a real gap fill, not
    # just "empty store → all zeros".
    assert sum(by_date.values()) == 1
    assert by_date[datetime.now(UTC).date().isoformat()] == 0


def test_daily_route_default_days(client: TestClient) -> None:
    assert len(client.get("/today/activity/daily").json()["days"]) == 90


def test_daily_route_clamps_out_of_range(client: TestClient) -> None:
    # Query(ge=1, le=365) → FastAPI returns 422 for out-of-range.
    assert client.get("/today/activity/daily?days=0").status_code == 422
    assert client.get("/today/activity/daily?days=999").status_code == 422
