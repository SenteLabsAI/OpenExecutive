"""Tests for GET /today and its deprecated alias GET /morning-brief."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import today as today_route
from openexecutive.briefing import narrative as briefing_narrative
from openexecutive.briefing import narrative_cache
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import decision_ledger, episodic
from openexecutive.people import insights_cache
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence as wf_persistence


def _setup_isolated_db(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch all store DB_PATHs to `db` and initialise schemas."""
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(dept_store, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(alert_store, "DB_PATH", db)
    monkeypatch.setattr(wf_persistence, "DB_PATH", db)
    monkeypatch.setattr(insights_cache, "DB_PATH", db)
    monkeypatch.setattr(narrative_cache, "DB_PATH", db)
    dept_registry.invalidate()
    # The channel reachability helpers read the people registry; drop its
    # cache so cross-test rosters don't leak through the 60s TTL.
    people_registry.invalidate()

    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    alert_store.initialize_db(db)
    wf_persistence.initialize_runs_db(db)
    insights_cache.initialize_db(db)
    narrative_cache.initialize_db(db)
    dept_store.seed_default_departments(db_path=db)


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(today_route.router)
    return TestClient(app)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _setup_isolated_db(tmp_path / "today.db", monkeypatch)
    return _make_client()


@pytest.fixture(autouse=True)
def _stub_narrative_synth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests network-free. Every /today GET schedules the narrative
    regen BackgroundTask, which TestClient executes; default the synthesizer
    to a no-op so it never calls the provider. Module-scoped (autouse) so it
    covers every test here; regen-specific tests override it with their own
    stub after this one runs.
    """
    async def _fake(**_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _fake)


# --------------------------------------------------------------------------- #
# Empty state
# --------------------------------------------------------------------------- #

def test_today_empty(client: TestClient) -> None:
    resp = client.get("/today")
    assert resp.status_code == 200
    data = resp.json()
    assert "departments" in data
    assert "people" in data
    assert "proposals" in data
    assert len(data["departments"]) == 8


def test_today_departments_have_fields(client: TestClient) -> None:
    resp = client.get("/today")
    dept = next(d for d in resp.json()["departments"] if d["slug"] == "finance")
    assert dept["title"]
    assert "authority_level" in dept
    assert "goal_count" in dept
    assert "at_risk_count" in dept
    assert "awaiting_count" in dept


# --------------------------------------------------------------------------- #
# Deprecated alias /morning-brief
# --------------------------------------------------------------------------- #

def test_morning_brief_alias_returns_same_body(client: TestClient) -> None:
    today = client.get("/today").json()
    legacy = client.get("/morning-brief").json()
    assert today == legacy


# --------------------------------------------------------------------------- #
# caller_person_id — resolved from x-caller-email so the briefing UI can
# split proposals into "routed to me" vs "across the team."
# --------------------------------------------------------------------------- #

def test_today_caller_person_id_resolves_from_header(client: TestClient) -> None:
    pid = people_store.upsert_person(
        full_name="Alice Example",
        role="Co-founder",
        email="alice@example.com",
    )
    resp = client.get("/today", headers={"x-caller-email": "alice@example.com"})
    assert resp.status_code == 200
    assert resp.json()["caller_person_id"] == pid


def test_today_caller_person_id_unmatched_header_is_null(client: TestClient) -> None:
    # Signed-in user with no matching Person row must not be silently fused
    # with the principal — caller_person_id must be None.
    resp = client.get("/today", headers={"x-caller-email": "stranger@example.com"})
    assert resp.status_code == 200
    assert resp.json()["caller_person_id"] is None


def test_morning_brief_alias_sets_deprecation_headers(client: TestClient) -> None:
    resp = client.get("/morning-brief")
    assert resp.status_code == 200
    assert resp.headers.get("deprecation") == "true"
    assert resp.headers.get("sunset", "").endswith("GMT")
    link = resp.headers.get("link", "")
    assert "/today" in link
    assert 'rel="successor-version"' in link


# --------------------------------------------------------------------------- #
# Goal counts reflected
# --------------------------------------------------------------------------- #

def test_goal_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "goals.db"
    _setup_isolated_db(db, monkeypatch)

    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Test", target="T", current="C", status="on_track", db_path=db)
    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Burn", target="<$550K", current="$612K", status="at_risk", db_path=db)

    resp = _make_client().get("/today")
    assert resp.status_code == 200
    fin = next(d for d in resp.json()["departments"] if d["slug"] == "finance")
    assert fin["goal_count"] == 2
    assert fin["at_risk_count"] == 1


def test_attention_goals_surface_problem_goals_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Department cards carry the actual off_track/at_risk goals (worst first,
    capped) so the briefing is insightful at rest; healthy goals are excluded."""
    db = tmp_path / "attn.db"
    _setup_isolated_db(db, monkeypatch)

    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Healthy", target="T", current="C", status="on_track", db_path=db)
    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Burn", target="<$550K", current="$612K", status="at_risk", db_path=db)
    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Runway", target=">12mo", current="7mo", status="off_track", db_path=db)

    resp = _make_client().get("/today")
    assert resp.status_code == 200
    fin = next(d for d in resp.json()["departments"] if d["slug"] == "finance")

    # Only the two problem goals (healthy excluded), off_track before at_risk.
    # List equality is order-sensitive, so this also pins the worst-first order.
    assert len(fin["attention_goals"]) == 2
    krs = [g["key_result"] for g in fin["attention_goals"]]
    assert krs == ["Runway", "Burn"]
    runway, burn = fin["attention_goals"]
    assert runway["status"] == "off_track"
    assert runway["current"] == "7mo"
    assert runway["target"] == ">12mo"
    assert burn["status"] == "at_risk"


def test_attention_goals_empty_for_healthy_department(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department with only on_track goals carries no attention_goals."""
    db = tmp_path / "healthy.db"
    _setup_isolated_db(db, monkeypatch)

    dept_store.insert_goal("finance", period_value="Q2 2026", key_result="Fine", target="T", current="C", status="on_track", db_path=db)

    resp = _make_client().get("/today")
    fin = next(d for d in resp.json()["departments"] if d["slug"] == "finance")
    assert fin["attention_goals"] == []


# --------------------------------------------------------------------------- #
# Proposals surface routed alerts
# --------------------------------------------------------------------------- #

def test_proposals_surface_all_unread_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both routed and unrouted unread alerts surface as action items.
    The UI handles the split (caller-match + principal-owns-unrouted),
    not the backend. Previously unrouted alerts were silently dropped
    here and never appeared in the briefing.
    """
    db = tmp_path / "prop.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="department_check_in",
        external_id="proposal-1",
        severity="medium",
        headline="Approve vendor renegotiation",
        body="Please approve.",
        suggested_action="Reply approve/reject",
        topic_tags=["department:finance"],
        routed_to_person_id=99,
        db_path=db,
    )
    alert_store.insert_alert(
        source="system",
        external_id="general-1",
        severity="low",
        headline="General alert",
        body="No routing.",
        db_path=db,
    )

    resp = _make_client().get("/today")
    proposals = resp.json()["proposals"]
    assert len(proposals) == 2
    by_headline = {p["headline"]: p for p in proposals}
    assert by_headline["Approve vendor renegotiation"]["routed_to_person_id"] == 99
    assert by_headline["General alert"]["routed_to_person_id"] is None


def test_artifact_alert_surfaces_as_action_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft_artifact alert (source='artifact', tagged 'artifact') must
    surface in /today as an action-category proposal carrying the tag, so
    the UI can render it as a document card in the 'Needs you' queue."""
    db = tmp_path / "artifact.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="artifact",
        external_id="artifact-1",
        severity="medium",
        headline="Competitor X Series B teardown",
        body="## Summary\n\nFull document body.",
        suggested_action="Reframes our Q3 fundraising window.",
        topic_tags=["artifact"],
        routed_to_person_id=7,
        db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    artifact = next(p for p in proposals if p["headline"] == "Competitor X Series B teardown")
    assert "artifact" in artifact["topic_tags"]
    assert artifact["category"] == "action"
    assert artifact["body"] == "## Summary\n\nFull document body."
    assert artifact["routed_to_person_id"] == 7


def test_proposals_include_unrouted_alerts_for_principal_inbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrouted alerts must come back from /today so the UI can place
    them in the principal's 'Needs you' bucket. The backend doesn't do
    the principal-owns-unrouted routing itself — it just surfaces the
    full set so the client can split it correctly.
    """
    db = tmp_path / "unrouted.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="triage",
        external_id="cold-inbound",
        severity="medium",
        headline="Cold inbound from Dana Reilly",
        body="Dana wants a 30-min walkthrough",
        suggested_action="Classify and propose next step",
        db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["routed_to_person_id"] is None
    assert proposals[0]["headline"] == "Cold inbound from Dana Reilly"


# --------------------------------------------------------------------------- #
# Decision-backed proposals (gated calendar bookings) carry decision_instance_id
# --------------------------------------------------------------------------- #

def test_today_surfaces_decision_instance_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alert tagged decision_instance:{id} exposes that id on the proposal,
    so the UI can route Approve/Reject to the /decisions endpoints. It must
    also land in the 'action' lane (it's routed)."""
    db = tmp_path / "decision.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="decision_scheduling",
        external_id="decision:42",
        severity="medium",
        headline="Approve meeting: Weekly sync",
        body="Meeting: Weekly sync\nWhen: ... → ...",
        suggested_action='Book "Weekly sync".',
        topic_tags=["decision_instance:42", "decision_class:meeting_scheduling"],
        routed_to_person_id=5,
        db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["decision_instance_id"] == 42
    assert proposals[0]["category"] == "action"
    assert proposals[0]["routed_to_person_id"] == 5


def test_today_decision_id_skips_malformed_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed decision_instance tag must not shadow a valid one later in
    the list — the parser keeps scanning."""
    db = tmp_path / "malformed.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="decision_scheduling",
        external_id="decision:7",
        severity="medium",
        headline="Approve meeting: Sync",
        body="...",
        topic_tags=["decision_instance:oops", "decision_instance:7"],
        routed_to_person_id=5,
        db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    assert proposals[0]["decision_instance_id"] == 7


def test_today_non_decision_alert_has_null_decision_instance_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain alert (no decision_instance tag) reports a null id."""
    db = tmp_path / "plain.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="system",
        external_id="plain-1",
        severity="low",
        headline="General alert",
        body="No routing.",
        db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["decision_instance_id"] is None


# --------------------------------------------------------------------------- #
# Awaiting workflow count
# --------------------------------------------------------------------------- #

def test_awaiting_count_in_people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "await.db"
    _setup_isolated_db(db, monkeypatch)

    person_id = people_store.upsert_person(full_name="Alex", role="Ops", db_path=db)

    wf_persistence.create_run("run-alex", "test_wf", "Test", {}, db_path=db)
    state = json.dumps({"on_timeout": "escalate", "channel": "slack", "department": "finance"})
    until = datetime.now(UTC) + timedelta(hours=4)
    wf_persistence.save_checkpoint("run-alex", state, person_id, until, db_path=db)

    resp = _make_client().get("/today")
    person_entry = next((p for p in resp.json()["people"] if p["full_name"] == "Alex"), None)
    assert person_entry is not None
    assert person_entry["awaiting_count"] == 1
    assert person_entry["soonest_sla_at"] is not None
    assert person_entry["status"] == "awaiting"


# --------------------------------------------------------------------------- #
# People-section enrichment: status, ranking, reachability, insight
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_real_insight_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise background insight generation so today tests stay hermetic
    (no Anthropic / Honcho calls). Tests that exercise the cache seed it
    directly instead."""
    from openexecutive.people import insights as insights_mod

    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(insights_mod, "generate_person_insight", _none)


def test_person_has_enrichment_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "enrich.db"
    _setup_isolated_db(db, monkeypatch)
    people_store.upsert_person(
        full_name="Dana", role="Ops", email="dana@co.com", preferred_channel="email", db_path=db,
    )

    person = next(p for p in _make_client().get("/today").json()["people"] if p["full_name"] == "Dana")
    assert person["status"] == "clear"
    assert person["reachable_now"] is True  # has email, no windows = always on
    assert person["awaiting_reply_count"] == 0
    assert person["overdue"] is False
    assert person["insight"] is None  # cold cache
    assert person["authority_scope"] == []


def test_awaiting_reply_status_and_overdue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "reply.db"
    _setup_isolated_db(db, monkeypatch)
    pid = people_store.upsert_person(full_name="Reed", role="Vendor", email="r@co.com", db_path=db)

    # Awaited 30h ago, default SLA 24h → overdue.
    episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="email", channel_ref="r@co.com",
        intent_text="Awaiting the signed SOW", assigned_to_person_id=pid,
        awaiting_response_since=datetime.now(UTC) - timedelta(hours=30), db_path=db,
    )

    person = next(p for p in _make_client().get("/today").json()["people"] if p["full_name"] == "Reed")
    assert person["status"] == "needs_reply"
    assert person["awaiting_reply_count"] == 1
    assert person["oldest_awaiting_reply_at"] is not None
    assert person["overdue"] is True


def test_on_leave_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "leave.db"
    _setup_isolated_db(db, monkeypatch)
    from datetime import date
    people_store.upsert_person(
        full_name="Lee", role="CFO", email="lee@co.com",
        on_leave_until=date(2099, 12, 31), db_path=db,
    )

    person = next(p for p in _make_client().get("/today").json()["people"] if p["full_name"] == "Lee")
    assert person["status"] == "on_leave"
    assert person["reachable_now"] is False
    assert person["on_leave_until"] == "2099-12-31"


def test_roster_ranked_attention_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "rank.db"
    _setup_isolated_db(db, monkeypatch)
    # Clear person (no pending work) and a person we're awaiting a reply from.
    people_store.upsert_person(full_name="Aaron Clear", role="Ops", email="a@co.com", db_path=db)
    pid_busy = people_store.upsert_person(full_name="Zoe Busy", role="Sales", email="z@co.com", db_path=db)
    episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="email", channel_ref="z@co.com",
        intent_text="Awaiting reply", assigned_to_person_id=pid_busy,
        awaiting_response_since=datetime.now(UTC) - timedelta(hours=2), db_path=db,
    )

    names = [p["full_name"] for p in _make_client().get("/today").json()["people"]]
    # Despite the alphabetical disadvantage, Zoe (needs_reply) ranks above Aaron (clear).
    assert names.index("Zoe Busy") < names.index("Aaron Clear")


def test_insight_served_from_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "insight.db"
    _setup_isolated_db(db, monkeypatch)
    pid = people_store.upsert_person(full_name="Cara", role="Ops", email="c@co.com", db_path=db)

    from openexecutive.people import insights as insights_mod
    monkeypatch.setattr(insights_mod, "build_insight_input_hash", lambda signals: "FIXEDHASH")
    insights_cache.put(
        insights_cache.PersonInsight(
            person_id=pid, input_hash="FIXEDHASH",
            insight_text="Available and clear; last contacted last week",
            generated_at=insights_cache.utc_now_iso(),
        ),
        db_path=db,
    )

    person = next(p for p in _make_client().get("/today").json()["people"] if p["full_name"] == "Cara")
    assert person["insight"] == "Available and clear; last contacted last week"


def test_morning_brief_alias_unaffected_by_async_today(client: TestClient) -> None:
    """The sync /morning-brief alias must keep returning the same body as
    the now-async /today (both serve insight=None on a cold cache)."""
    assert client.get("/today").json() == client.get("/morning-brief").json()


def test_naive_awaiting_timestamp_does_not_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a timezone-NAIVE awaiting_response_since on disk must not
    crash /today. Comparing it against datetime.now(UTC) used to raise an
    uncaught TypeError and 500 the whole brief."""
    from datetime import datetime as _dt
    db = tmp_path / "naive.db"
    _setup_isolated_db(db, monkeypatch)
    pid = people_store.upsert_person(full_name="Nina", role="Vendor", email="n@co.com", db_path=db)

    # Bare naive datetime (no tzinfo), clearly past → should read as overdue.
    naive_past = _dt(2020, 1, 1, 0, 0, 0)
    assert naive_past.tzinfo is None
    episodic.insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(), channel="email", channel_ref="n@co.com",
        intent_text="Awaiting since forever", assigned_to_person_id=pid,
        awaiting_response_since=naive_past, db_path=db,
    )

    resp = _make_client().get("/today")
    assert resp.status_code == 200
    person = next(p for p in resp.json()["people"] if p["full_name"] == "Nina")
    assert person["status"] == "needs_reply"
    assert person["overdue"] is True


def test_parse_aware_helpers_tolerate_naive() -> None:
    """Unit-level guard for the datetime helpers behind the regression above."""
    now = datetime.now(UTC)
    # Naive past timestamp is treated as UTC, not a crash.
    assert today_route._is_past("2020-01-01T00:00:00", now) is True
    assert today_route._reply_overdue("2020-01-01T00:00:00", 24, now) is True
    # Aware timestamps still work; malformed/empty → safe False.
    assert today_route._is_past(now.isoformat(), now) is False
    assert today_route._is_past("not-a-date", now) is False
    assert today_route._is_past(None, now) is False


# --------------------------------------------------------------------------- #
# GET /today/activity
# --------------------------------------------------------------------------- #

def test_activity_empty_store_returns_empty(client: TestClient) -> None:
    resp = client.get("/today/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": []}


def test_activity_includes_fired_scheduled_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "act.db"
    _setup_isolated_db(db, monkeypatch)

    # The default departments seed at authority_level=propose_only, but the
    # rows below are simulating dispatched actions (the test only marks
    # them done — it doesn't actually run the gate). Flip the depts under
    # test to auto_execute so the activity-rail reclassifier doesn't label
    # them as `proposal_routed` (which is the right call for real
    # propose_only-gated actions, see test_activity_reclassifies_propose_only).
    from openexecutive.departments.models import AuthorityLevel
    for slug in ("marketing", "strategy", "finance"):
        dept_store.update_department(slug, authority_level=AuthorityLevel.AUTO_EXECUTE, db_path=db)

    # Three fired actions across different kinds. We seed each as 'pending'
    # then flip to 'done' so the row goes through the normal lifecycle.
    now = datetime.now(UTC).isoformat()
    aid_dm = episodic.insert_scheduled_action(
        run_at=now, channel="slack_dm", channel_ref="alice",
        intent_text="Followed up on Q3 CAC", department="marketing",
        kind="ad_hoc", db_path=db,
    )
    aid_nudge = episodic.insert_scheduled_action(
        run_at=now, channel="email", channel_ref="bob@example.com",
        intent_text="Reminded Bob about board prep", department="strategy",
        kind="proactive_nudge", scope_key="nudge:test:1", db_path=db,
    )
    aid_cad = episodic.insert_scheduled_action(
        run_at=now, channel="discord_dm", channel_ref="charlie",
        intent_text="Monthly finance check-in", department="finance",
        kind="dept_cadence", db_path=db,
    )
    for aid in (aid_dm, aid_nudge, aid_cad):
        assert episodic.mark_action_done(aid, db_path=db)

    resp = _make_client().get("/today/activity")
    assert resp.status_code == 200
    items = resp.json()["items"]
    kinds = {item["kind"] for item in items}
    assert "dm_sent" in kinds
    assert "nudge_sent" in kinds
    assert "cadence_sent" in kinds
    # Every fired action carries actor=Executive and a non-empty summary.
    for item in items:
        assert item["actor"] == "Executive"
        assert item["summary"]


def test_activity_excludes_internal_and_nudge_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "act_excl.db"
    _setup_isolated_db(db, monkeypatch)

    now = datetime.now(UTC).isoformat()
    # nudge_scan = internal heartbeat, must NOT appear in user-visible feed.
    aid_scan = episodic.insert_scheduled_action(
        run_at=now, channel="__internal__", channel_ref="-",
        intent_text="internal nudge scan tick", kind="nudge_scan", db_path=db,
    )
    # __internal__ channel on any kind also stays out — no outbound side effect.
    aid_internal = episodic.insert_scheduled_action(
        run_at=now, channel="__internal__", channel_ref="-",
        intent_text="internal cadence trigger", kind="dept_cadence", db_path=db,
    )
    # And a real, visible one to prove the filter isn't dropping everything.
    aid_real = episodic.insert_scheduled_action(
        run_at=now, channel="slack_dm", channel_ref="alice",
        intent_text="Real DM", kind="ad_hoc", db_path=db,
    )
    for aid in (aid_scan, aid_internal, aid_real):
        assert episodic.mark_action_done(aid, db_path=db)

    items = _make_client().get("/today/activity").json()["items"]
    summaries = [i["summary"] for i in items]
    assert "Real DM" in summaries
    assert "internal nudge scan tick" not in summaries
    assert "internal cadence trigger" not in summaries


def test_activity_reclassifies_propose_only_to_proposal_routed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For propose_only departments the runner marks actions done WITHOUT
    dispatching (scheduler/runner.py — the propose branch returns before
    the send tools fire). The activity rail must surface those as
    'proposal_routed' so the UI says 'proposed to <approver>' instead of
    'DM'd <person>', which would lie about a message actually going out.
    """
    db = tmp_path / "propose_kind.db"
    _setup_isolated_db(db, monkeypatch)
    # Default-seeded depts are already propose_only — finance is one.

    now = datetime.now(UTC).isoformat()
    aid = episodic.insert_scheduled_action(
        run_at=now, channel="discord_dm", channel_ref="banuid",
        intent_text="Proposal: hire a contract designer",
        department="finance", kind="ad_hoc", db_path=db,
    )
    assert episodic.mark_action_done(aid, db_path=db)

    items = _make_client().get("/today/activity").json()["items"]
    rows = [i for i in items if i["summary"].startswith("Proposal: hire")]
    assert len(rows) == 1
    assert rows[0]["kind"] == "proposal_routed"


def test_activity_includes_decisions_and_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "act_da.db"
    _setup_isolated_db(db, monkeypatch)

    episodic.store_decision(
        domain="finance", summary="Approve Q3 budget", department="finance", db_path=db,
    )
    episodic.store_advice(
        domain="strategy",
        query_summary="Pricing?",
        advice_summary="Hold the line until December.",
        department="strategy",
        db_path=db,
    )

    items = _make_client().get("/today/activity").json()["items"]
    kinds = {i["kind"] for i in items}
    assert "decision_logged" in kinds
    assert "advice_given" in kinds


def test_activity_includes_workflow_initiative_decision_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feed merges beyond messaging: completed workflow runs, initiatives,
    resolved gated decisions, and raised alerts each surface with their kind."""
    db = tmp_path / "act_broad.db"
    _setup_isolated_db(db, monkeypatch)

    # Completed workflow run → workflow_done.
    wf_persistence.create_run("run-1", "morning_brief", "Morning brief", {}, db_path=db)
    wf_persistence.complete_run("run-1", artifact="## Brief\n...", db_path=db)
    # A still-running run must NOT surface (no completion).
    wf_persistence.create_run("run-2", "research", "Open research run", {}, db_path=db)

    # Initiative → initiative_started.
    episodic.store_initiative(
        title="AI Opportunity Assessment", status="active",
        department="strategy", db_path=db,
    )

    # Resolved gated decision → decision_resolved (pending one stays out).
    iid = decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="operations",
        originating_session_id=None,
        proposed_payload={"summary": "Book weekly sync"},
        idempotency_key="m1", gate_mode="propose",
        approver_person_id=None, confidence=0.9, db_path=db,
    )
    decision_ledger.mark_resolved(
        iid, decision_ledger.STATUS_APPROVED_UNCHANGED, db_path=db,
    )
    decision_ledger.create_decision_instance(
        decision_class="meeting_scheduling", department="operations",
        originating_session_id=None, proposed_payload={"summary": "Pending one"},
        idempotency_key="m2", gate_mode="propose",
        approver_person_id=None, confidence=0.5, db_path=db,
    )

    # Raised alert → alert_raised; a decision_scheduling alert is excluded.
    alert_store.insert_alert(
        source="triage", external_id="a1", severity="high",
        headline="Cold inbound from Dana", body="x", db_path=db,
    )
    alert_store.insert_alert(
        source=decision_ledger.DECISION_ALERT_SOURCE, external_id="decision:1",
        severity="medium", headline="Approve meeting: Sync", body="y",
        topic_tags=["decision_instance:1"], db_path=db,
    )

    items = _make_client().get("/today/activity?limit=100").json()["items"]
    by_kind: dict[str, list[dict]] = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)

    assert [i["summary"] for i in by_kind.get("workflow_done", [])] == ["Morning brief"]
    assert [i["summary"] for i in by_kind.get("initiative_started", [])] == ["AI Opportunity Assessment"]
    # Decision summary derives the verb from status + a human payload key.
    assert by_kind["decision_resolved"][0]["summary"] == "Approved: Book weekly sync"
    assert len(by_kind["decision_resolved"]) == 1  # pending one excluded
    alert_summaries = [i["summary"] for i in by_kind.get("alert_raised", [])]
    assert "Cold inbound from Dana" in alert_summaries
    assert "Approve meeting: Sync" not in alert_summaries  # decision_scheduling excluded


def test_activity_orders_by_timestamp_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "act_order.db"
    _setup_isolated_db(db, monkeypatch)

    # Older first, then newer. The feed should return newer first.
    episodic.store_decision(domain="finance", summary="OLD decision", db_path=db)
    # Force a small advance so timestamps are distinct.
    import time
    time.sleep(0.01)
    episodic.store_decision(domain="finance", summary="NEW decision", db_path=db)

    items = _make_client().get("/today/activity").json()["items"]
    # Find the two decisions in the order returned.
    decision_items = [i for i in items if i["kind"] == "decision_logged"]
    assert decision_items[0]["summary"] == "NEW decision"
    assert decision_items[1]["summary"] == "OLD decision"


def test_activity_respects_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "act_limit.db"
    _setup_isolated_db(db, monkeypatch)

    # Seed 5 decisions, ask for 3.
    for i in range(5):
        episodic.store_decision(
            domain="finance", summary=f"Decision {i}", db_path=db,
        )

    items = _make_client().get("/today/activity?limit=3").json()["items"]
    assert len(items) == 3


def test_activity_limit_below_one_rejected(client: TestClient) -> None:
    resp = client.get("/today/activity?limit=0")
    assert resp.status_code == 422  # FastAPI Query(ge=1) validation


def test_activity_limit_above_max_rejected(client: TestClient) -> None:
    resp = client.get("/today/activity?limit=500")
    assert resp.status_code == 422  # FastAPI Query(le=100) validation


# --------------------------------------------------------------------------- #
# Briefing narrative + proposal ranking (Phase 1: "tell a better story")
# --------------------------------------------------------------------------- #


def test_proposals_carry_score_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrouted low-severity watchlist signal is demoted to 'monitoring';
    a routed alert and a high-severity stock move stay 'action'."""
    db = tmp_path / "rank.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="stock", external_id="stk-low", severity="low",
        headline="LCID moved 3.2%", body="no obvious driver",
        topic_tags=["external:stock-lcid"], db_path=db,
    )
    alert_store.insert_alert(
        source="department_check_in", external_id="routed-1", severity="medium",
        headline="Approve vendor renegotiation", body="please approve",
        topic_tags=["department:finance"], routed_to_person_id=99, db_path=db,
    )
    alert_store.insert_alert(
        source="stock", external_id="stk-urgent", severity="urgent",
        headline="TSLA -18%", body="major move",
        topic_tags=["external:stock-tsla"], db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    by_headline = {p["headline"]: p for p in proposals}
    assert by_headline["LCID moved 3.2%"]["category"] == "monitoring"
    assert by_headline["Approve vendor renegotiation"]["category"] == "action"
    assert by_headline["TSLA -18%"]["category"] == "action"  # high severity stays
    # routed medium (40+15) outscores the unrouted urgent? no — urgent=100.
    assert by_headline["TSLA -18%"]["score"] == 100
    assert by_headline["Approve vendor renegotiation"]["score"] == 55


def test_proposals_sorted_action_before_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monitoring noise sorts after action items regardless of insert order."""
    db = tmp_path / "sort.db"
    _setup_isolated_db(db, monkeypatch)

    alert_store.insert_alert(
        source="stock", external_id="m1", severity="low",
        headline="monitoring item", body="x",
        topic_tags=["external:stock-x"], db_path=db,
    )
    alert_store.insert_alert(
        source="triage", external_id="a1", severity="high",
        headline="action item", body="y", db_path=db,
    )

    proposals = _make_client().get("/today").json()["proposals"]
    categories = [p["category"] for p in proposals]
    # All 'action' entries precede the first 'monitoring' entry.
    assert categories == sorted(categories, key=lambda c: 0 if c == "action" else 1)
    assert proposals[0]["headline"] == "action item"


def test_narrative_served_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached narrative is served verbatim on the response (the fresh-vs-
    stale distinction is covered by test_fresh_cache_does_not_trigger_regen)."""
    db = tmp_path / "narr.db"
    _setup_isolated_db(db, monkeypatch)

    snapshot = today_route._build_today()
    nhash = narrative_cache.build_narrative_input_hash(snapshot.model_dump())
    narrative_cache.put(
        narrative_cache.BriefingNarrative(
            scope=narrative_cache.DEFAULT_SCOPE,
            input_hash=nhash,
            narrative_text="**Top call:** ship the C2 decision.",
            generated_at=narrative_cache.utc_now_iso(),
        ),
        db_path=db,
    )

    data = _make_client().get("/today").json()
    assert data["narrative"] == "**Top call:** ship the C2 decision."


def test_narrative_null_on_cold_cache(client: TestClient) -> None:
    """With no cached narrative, the field is null (the regen runs in the
    background; the synthesizer is stubbed to return '')."""
    data = client.get("/today").json()
    assert data["narrative"] is None


def test_narrative_regenerated_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold/stale cache schedules regen; after the request the cache is
    populated and the next request serves it."""
    db = tmp_path / "regen.db"
    _setup_isolated_db(db, monkeypatch)

    async def _synth(**_kwargs: object) -> str:
        return "**What changed:** nothing dramatic."

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    first = c.get("/today").json()
    assert first["narrative"] is None  # cold at response-build time
    # Background task ran during the TestClient call → cache now populated.
    cached = narrative_cache.get(db_path=db)
    assert cached is not None and cached.narrative_text == "**What changed:** nothing dramatic."
    # Second request serves it.
    assert c.get("/today").json()["narrative"] == "**What changed:** nothing dramatic."


def test_fresh_cache_does_not_trigger_regen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the cached narrative hash matches current state, the request
    serves it and does NOT schedule a (costly) regeneration."""
    db = tmp_path / "fresh.db"
    _setup_isolated_db(db, monkeypatch)

    snapshot = today_route._build_today()
    nhash = narrative_cache.build_narrative_input_hash(snapshot.model_dump())
    narrative_cache.put(
        narrative_cache.BriefingNarrative(
            scope=narrative_cache.DEFAULT_SCOPE, input_hash=nhash,
            narrative_text="cached text", generated_at=narrative_cache.utc_now_iso(),
        ),
        db_path=db,
    )

    calls = {"n": 0}

    async def _synth(**_kwargs: object) -> str:
        calls["n"] += 1
        return "regenerated"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    data = _make_client().get("/today").json()
    assert data["narrative"] == "cached text"
    assert calls["n"] == 0  # fresh cache → no background regen scheduled


def test_proposals_recency_tiebreak_within_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within the same category+score band, the more-recent alert leads.
    Regression guard for the two-pass stable sort."""
    import sqlite3

    db = tmp_path / "recency.db"
    _setup_isolated_db(db, monkeypatch)

    # Two action items, identical severity (→ identical score), different ages.
    # created_at is auto-set on insert, so we backdate them via direct SQL.
    alert_store.insert_alert(
        source="triage", external_id="older", severity="high",
        headline="older action", body="x", db_path=db,
    )
    alert_store.insert_alert(
        source="triage", external_id="newer", severity="high",
        headline="newer action", body="y", db_path=db,
    )
    # Both inside the action TTL (alerts/lifecycle.py) — a backdate past the
    # TTL would drop the row from the live queue rather than sort it.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE alerts SET created_at=? WHERE external_id=?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), "older"),
        )
        conn.execute(
            "UPDATE alerts SET created_at=? WHERE external_id=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), "newer"),
        )
        conn.commit()

    proposals = _make_client().get("/today").json()["proposals"]
    headlines = [p["headline"] for p in proposals]
    assert headlines.index("newer action") < headlines.index("older action")


# --------------------------------------------------------------------------- #
# In flight & awaiting (Phase 2)
# --------------------------------------------------------------------------- #


def test_in_flight_lists_user_facing_pending_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending user-facing actions surface in `in_flight`; internal plumbing
    (the __internal__ channel and nudge_scan heartbeat) is excluded."""
    db = tmp_path / "inflight.db"
    _setup_isolated_db(db, monkeypatch)

    pid = people_store.upsert_person(
        full_name="Sam Rivera", role="CFO", email="dan@example.com",
    )
    future = (datetime.now(UTC) + timedelta(hours=8)).isoformat()
    episodic.insert_scheduled_action(
        run_at=future, channel="email", channel_ref="dan@example.com",
        intent_text="Check Dan's C2 finance review", department="finance",
        kind="ad_hoc", assigned_to_person_id=pid, db_path=db,
    )
    episodic.insert_scheduled_action(
        run_at=future, channel="__internal__", channel_ref="watchlist",
        intent_text="internal scan", kind="dept_cadence", db_path=db,
    )
    episodic.insert_scheduled_action(
        run_at=future, channel="__internal__", channel_ref="hb",
        intent_text="heartbeat", kind="nudge_scan", db_path=db,
    )

    data = _make_client().get("/today").json()
    in_flight = data["in_flight"]
    assert len(in_flight) == 1
    item = in_flight[0]
    assert item["intent"] == "Check Dan's C2 finance review"
    assert item["target"] == "Sam Rivera"  # resolved from assigned person
    assert item["department"] == "finance"
    assert item["overdue"] is False  # run_at is in the future


def test_in_flight_overdue_when_run_at_past(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "overdue.db"
    _setup_isolated_db(db, monkeypatch)
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    episodic.insert_scheduled_action(
        run_at=past, channel="slack_dm", channel_ref="U123",
        intent_text="overdue follow-up", kind="ad_hoc", db_path=db,
    )
    in_flight = _make_client().get("/today").json()["in_flight"]
    assert len(in_flight) == 1
    assert in_flight[0]["overdue"] is True
    # No assigned person and no roster match → target falls back to the raw
    # channel ref (regression guard for _resolve_channel_target's fallback).
    assert in_flight[0]["target"] == "U123"


def test_awaiting_lists_people_with_open_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person with an open awaited reply appears in `awaiting`."""
    db = tmp_path / "awaiting.db"
    _setup_isolated_db(db, monkeypatch)

    pid = people_store.upsert_person(
        full_name="Sam Rivera", role="CFO", email="dan2@example.com",
    )
    # An open commitment we're awaiting THEIR reply on (not a nudge).
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channel="email", channel_ref="dan2@example.com",
        intent_text="awaiting C2 sign-off", kind="ad_hoc",
        assigned_to_person_id=pid,
        awaiting_response_since=datetime.now(UTC) - timedelta(days=1),
        db_path=db,
    )

    awaiting = _make_client().get("/today").json()["awaiting"]
    by_pid = {a["person_id"]: a for a in awaiting}
    assert pid in by_pid
    assert by_pid[pid]["full_name"] == "Sam Rivera"
    # Exactly one open commitment seeded — guards against double-counting.
    assert by_pid[pid]["awaiting_count"] == 1


def test_in_flight_target_falls_back_when_assigned_person_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An action assigned to a person id not in the roster (e.g. deleted)
    falls back to the channel-ref resolution rather than returning None."""
    db = tmp_path / "orphan.db"
    _setup_isolated_db(db, monkeypatch)
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channel="email", channel_ref="ghost@example.com",
        intent_text="follow up with ghost", kind="ad_hoc",
        assigned_to_person_id=99999,  # no such person
        db_path=db,
    )
    in_flight = _make_client().get("/today").json()["in_flight"]
    assert len(in_flight) == 1
    # pid_to_name miss → channel lookup miss → raw channel ref.
    assert in_flight[0]["target"] == "ghost@example.com"


def test_in_flight_and_awaiting_empty_by_default(client: TestClient) -> None:
    data = client.get("/today").json()
    assert data["in_flight"] == []
    assert data["awaiting"] == []


# --------------------------------------------------------------------------- #
# Per-viewer personalized narrative
# --------------------------------------------------------------------------- #


def test_narrative_personalized_per_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each viewer gets their own narrative: the principal sees the whole
    company; a teammate sees only what's routed to them. Crucially, the two
    are cached under distinct scopes — no cross-user leakage."""
    db = tmp_path / "perviewer.db"
    _setup_isolated_db(db, monkeypatch)

    people_store.upsert_person(
        full_name="Jordan", role="CEO", email="rufus@x.com",
        is_principal=True, db_path=db,
    )
    dan = people_store.upsert_person(
        full_name="Sam Rivera", role="CFO", email="dan@x.com",
        department_slugs=["finance"], db_path=db,
    )
    # One proposal routed to Dan, one unrouted (the principal's catch-all).
    alert_store.insert_alert(
        source="department_check_in", external_id="dan-1", severity="high",
        headline="Dan approval needed", body="x",
        routed_to_person_id=dan, db_path=db,
    )
    alert_store.insert_alert(
        source="system", external_id="gen-1", severity="medium",
        headline="Company-wide thing", body="y", db_path=db,
    )

    seen: list[dict[str, object]] = []

    async def _synth(**kw: object) -> str:
        viewer = kw.get("viewer")
        today_data = kw.get("today_data") or {}
        seen.append({
            "viewer": viewer,
            "proposals": [
                p["headline"] for p in today_data.get("proposals", [])  # type: ignore[union-attr]
            ],
        })
        return f"brief-for-{viewer['name']}" if viewer else "brief-for-principal"  # type: ignore[index]

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    # Cold caches → regen runs in the background during each call.
    c.get("/today", headers={"x-caller-email": "rufus@x.com"})
    c.get("/today", headers={"x-caller-email": "dan@x.com"})
    # Now served from each viewer's own cache scope.
    p_narr = c.get("/today", headers={"x-caller-email": "rufus@x.com"}).json()["narrative"]
    d_narr = c.get("/today", headers={"x-caller-email": "dan@x.com"}).json()["narrative"]

    assert p_narr == "brief-for-principal"
    assert d_narr == "brief-for-Sam Rivera"
    assert p_narr != d_narr  # no cross-user leak

    # Dan's synthesis input was scoped to HIS routed proposal only.
    dan_call = next(
        s for s in seen if s["viewer"] and s["viewer"]["name"] == "Sam Rivera"  # type: ignore[index]
    )
    assert dan_call["proposals"] == ["Dan approval needed"]
    # The principal's synthesis saw the whole company.
    principal_call = next(s for s in seen if s["viewer"] is None)
    assert set(principal_call["proposals"]) == {"Dan approval needed", "Company-wide thing"}  # type: ignore[arg-type]


def test_unrostered_viewer_gets_whole_company_narrative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller whose email isn't on a Person row falls back to the
    whole-company (principal-scope) narrative."""
    db = tmp_path / "unrostered.db"
    _setup_isolated_db(db, monkeypatch)

    async def _synth(**kw: object) -> str:
        return "principal-brief" if kw.get("viewer") is None else "teammate-brief"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    c.get("/today", headers={"x-caller-email": "stranger@x.com"})
    n = c.get("/today", headers={"x-caller-email": "stranger@x.com"}).json()["narrative"]
    assert n == "principal-brief"
    # Structurally: the fallback writes the DEFAULT_SCOPE key, never "person:None".
    assert narrative_cache.get("principal", db_path=db) is not None
    assert narrative_cache.get("person:None", db_path=db) is None


def test_two_teammates_get_separate_narratives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two distinct non-principal teammates are cached under separate scopes
    and each is served only their own narrative — the A↔B half of the no-leak
    guarantee, not just principal↔teammate."""
    db = tmp_path / "twoteam.db"
    _setup_isolated_db(db, monkeypatch)

    dan = people_store.upsert_person(
        full_name="Dan", role="CFO", email="dan@x.com",
        department_slugs=["finance"], db_path=db,
    )
    eve = people_store.upsert_person(
        full_name="Eve", role="COO", email="eve@x.com",
        department_slugs=["operations"], db_path=db,
    )
    alert_store.insert_alert(
        source="department_check_in", external_id="d", severity="high",
        headline="Dan item", body="x", routed_to_person_id=dan, db_path=db,
    )
    alert_store.insert_alert(
        source="department_check_in", external_id="e", severity="high",
        headline="Eve item", body="y", routed_to_person_id=eve, db_path=db,
    )

    captured: dict[str, list[str]] = {}

    async def _synth(**kw: object) -> str:
        viewer = kw.get("viewer")
        today_data = kw.get("today_data") or {}
        if viewer:
            captured[viewer["name"]] = [  # type: ignore[index]
                p["headline"] for p in today_data.get("proposals", [])  # type: ignore[union-attr]
            ]
            return f"brief-for-{viewer['name']}"  # type: ignore[index]
        return "principal"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    c.get("/today", headers={"x-caller-email": "dan@x.com"})
    c.get("/today", headers={"x-caller-email": "eve@x.com"})
    dan_narr = c.get("/today", headers={"x-caller-email": "dan@x.com"}).json()["narrative"]
    eve_narr = c.get("/today", headers={"x-caller-email": "eve@x.com"}).json()["narrative"]

    assert dan_narr == "brief-for-Dan"
    assert eve_narr == "brief-for-Eve"
    assert dan_narr != eve_narr  # A never served B's narrative
    assert captured["Dan"] == ["Dan item"]
    assert captured["Eve"] == ["Eve item"]
    # Distinct cache scopes, each holding only its owner's text.
    assert narrative_cache.get(f"person:{dan}", db_path=db).narrative_text == "brief-for-Dan"  # type: ignore[union-attr]
    assert narrative_cache.get(f"person:{eve}", db_path=db).narrative_text == "brief-for-Eve"  # type: ignore[union-attr]


def test_teammate_with_no_routed_proposals_quiet_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teammate with nothing routed to them gets an empty slice and a
    quiet-day narrative — the regen completes without error."""
    db = tmp_path / "quiet.db"
    _setup_isolated_db(db, monkeypatch)

    people_store.upsert_person(
        full_name="Dan", role="CFO", email="dan@x.com",
        department_slugs=["finance"], db_path=db,
    )
    captured: dict[str, list[str]] = {}

    async def _synth(**kw: object) -> str:
        viewer = kw.get("viewer")
        today_data = kw.get("today_data") or {}
        if viewer:
            captured["proposals"] = [
                p["headline"] for p in today_data.get("proposals", [])  # type: ignore[union-attr]
            ]
            return "Quiet right now — nothing needs you."
        return "principal"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    c.get("/today", headers={"x-caller-email": "dan@x.com"})
    narr = c.get("/today", headers={"x-caller-email": "dan@x.com"}).json()["narrative"]
    assert captured["proposals"] == []  # empty slice — nothing routed to them
    assert narr == "Quiet right now — nothing needs you."


def test_narrative_input_is_company_action_no_monitoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The principal narrative synthesizes over the whole company's ACTION
    items (mine + across the team) but never the monitoring/watchlist noise
    the UI demotes. (It's a synthesis, not a re-list of the cards.)"""
    db = tmp_path / "narrinput.db"
    _setup_isolated_db(db, monkeypatch)

    people_store.upsert_person(
        full_name="Jordan", role="CEO", email="rufus@x.com",
        is_principal=True, db_path=db,
    )
    dan = people_store.upsert_person(
        full_name="Dan", role="CFO", email="dan@x.com",
        department_slugs=["finance"], db_path=db,
    )
    # Unrouted action item (principal owns).
    alert_store.insert_alert(
        source="department_check_in", external_id="a1", severity="high",
        headline="Approve budget", body="x", db_path=db,
    )
    # Monitoring/watchlist (stock) → excluded from the synthesis.
    alert_store.insert_alert(
        source="stock", external_id="m1", severity="low",
        headline="LCID up 3%", body="no driver",
        topic_tags=["external:stock-lcid"], db_path=db,
    )
    # Action routed to a teammate → still part of the whole-company picture.
    alert_store.insert_alert(
        source="department_check_in", external_id="d1", severity="high",
        headline="Dan approval", body="y", routed_to_person_id=dan, db_path=db,
    )

    captured: dict[str, list[str]] = {}

    async def _synth(**kw: object) -> str:
        captured["proposals"] = sorted(
            p["headline"]
            for p in (kw.get("today_data") or {}).get("proposals", [])  # type: ignore[union-attr]
        )
        return "narrative"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    c.get("/today", headers={"x-caller-email": "rufus@x.com"})
    # Both action items, across-team included; monitoring "LCID up 3%" excluded.
    assert captured["proposals"] == ["Approve budget", "Dan approval"]


def test_teammate_slice_is_only_their_routed_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teammate's narrative input is only the ACTION items routed to them —
    not the principal's unrouted items, and not unrouted monitoring noise."""
    db = tmp_path / "teamslice.db"
    _setup_isolated_db(db, monkeypatch)
    dan = people_store.upsert_person(
        full_name="Dan", role="CFO", email="dan@x.com",
        department_slugs=["finance"], db_path=db,
    )
    # Routed to Dan → his.
    alert_store.insert_alert(
        source="department_check_in", external_id="a", severity="high",
        headline="Dan action", body="x", routed_to_person_id=dan, db_path=db,
    )
    # Unrouted action (the principal's) → not Dan's.
    alert_store.insert_alert(
        source="department_check_in", external_id="p", severity="high",
        headline="Principal action", body="x", db_path=db,
    )
    # Unrouted monitoring (watchlist) → excluded everywhere but the principal's
    # Monitoring section; definitely not Dan's.
    alert_store.insert_alert(
        source="stock", external_id="m", severity="low",
        headline="ticker move", body="z",
        topic_tags=["external:stock-x"], db_path=db,
    )

    captured: dict[str, list[str]] = {}

    async def _synth(**kw: object) -> str:
        if kw.get("viewer"):
            captured["proposals"] = [
                p["headline"]
                for p in (kw.get("today_data") or {}).get("proposals", [])  # type: ignore[union-attr]
            ]
        return "n"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)

    c = _make_client()
    c.get("/today", headers={"x-caller-email": "dan@x.com"})
    assert captured["proposals"] == ["Dan action"]


def test_today_has_no_talent_or_onboarding_fields(client: TestClient) -> None:
    """The talent and staff-onboarding verticals were removed, so /today must
    no longer carry their rollups. Pins the response-shape change so a revert
    or a stray re-add is caught here rather than by the UI."""
    resp = client.get("/today")
    assert resp.status_code == 200
    body = resp.json()
    assert "talent" not in body
    assert "onboarding" not in body


# --------------------------------------------------------------------------- #
# Alert lifecycle on the read side (alerts/lifecycle.py)
# --------------------------------------------------------------------------- #


def test_proposals_exclude_expired_resolved_and_past_ttl_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only live unread rows surface: swept (`expired`), Executive-closed
    (`resolved`) and unswept-but-past-TTL rows are all absent."""
    import sqlite3

    db = tmp_path / "lifecycle.db"
    _setup_isolated_db(db, monkeypatch)
    from openexecutive.alerts import lifecycle

    monkeypatch.setattr(lifecycle, "_ttl_settings", lambda: (3, 14))

    alert_store.insert_alert(
        source="triage", external_id="live", severity="high",
        headline="live item", body="x", db_path=db,
    )
    swept = alert_store.insert_alert(
        source="triage", external_id="swept", severity="high",
        headline="swept item", body="x", db_path=db,
    )
    resolved = alert_store.insert_alert(
        source="triage", external_id="resolved", severity="high",
        headline="resolved item", body="x", db_path=db,
    )
    alert_store.insert_alert(
        source="triage", external_id="old", severity="high",
        headline="past ttl item", body="x", db_path=db,
    )
    assert swept is not None and resolved is not None
    alert_store.set_status(swept, "expired", db_path=db)
    alert_store.set_status(resolved, "resolved", db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE alerts SET created_at=? WHERE external_id='old'",
            ((datetime.now(UTC) - timedelta(days=20)).isoformat(),),
        )
        conn.commit()

    headlines = [p["headline"] for p in _make_client().get("/today").json()["proposals"]]
    assert headlines == ["live item"]


def test_proposals_carry_lifecycle_fields_and_demote_likely_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fields.db"
    _setup_isolated_db(db, monkeypatch)

    stale = alert_store.insert_alert(
        source="triage", external_id="s", severity="high",
        headline="stale one", body="x", db_path=db,
    )
    fresh = alert_store.insert_alert(
        source="triage", external_id="f", severity="high",
        headline="fresh one", body="x", db_path=db,
    )
    folded = alert_store.insert_alert(
        source="triage", external_id="dup", severity="high",
        headline="dup", body="x", db_path=db,
    )
    assert stale is not None and fresh is not None and folded is not None
    alert_store.set_review(
        stale, verdict="likely_stale", note="no activity in 9 days",
        recommended_move="close", db_path=db,
    )
    alert_store.set_review(
        fresh, verdict="relevant", note="Dana replied overnight",
        recommended_move="nudge", why_now="SLA ends today",
        due_at=(datetime.now(UTC) + timedelta(hours=6)).isoformat(), db_path=db,
    )
    alert_store.mark_superseded(folded, fresh, db_path=db)

    proposals = _make_client().get("/today").json()["proposals"]
    by_headline = {p["headline"]: p for p in proposals}
    assert set(by_headline) == {"stale one", "fresh one"}
    assert proposals[0]["headline"] == "fresh one"  # due-soon bonus + stale penalty
    f = by_headline["fresh one"]
    assert f["review_verdict"] == "relevant"
    assert f["review_note"] == "Dana replied overnight"
    assert f["recommended_move"] == "nudge"
    assert f["why_now"] == "SLA ends today"
    assert f["due_at"] is not None
    assert f["superseded_count"] == 1
    assert f["occurrence_count"] == 2
    assert f["last_reviewed_at"] is not None
    s = by_headline["stale one"]
    assert s["review_verdict"] == "likely_stale"
    assert s["score"] < f["score"]


def test_handled_overnight_rows_are_structured_and_track_current_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rail is rebuilt from audit rows: each carries the who / what / why the
    audit row recorded, plus the alert's status NOW so an undone close renders
    as reopened instead of offering a second Undo."""
    from openexecutive.audit import logger as audit_logger
    from openexecutive.briefing import brief_state

    db = tmp_path / "today.db"
    _setup_isolated_db(db, monkeypatch)
    al = audit_logger.AuditLogger(db_path=tmp_path / "audit.db")
    al.initialize_db()
    monkeypatch.setattr(audit_logger, "get_audit_logger", lambda: al)
    monkeypatch.setattr(brief_state, "since_for", lambda kind, now=None: datetime.now(UTC) - timedelta(hours=1))

    def _alert(headline: str) -> int:
        aid = alert_store.insert_alert(
            source="triage", external_id=f"ext-{headline}", severity="medium",
            headline=headline, body=headline,
        )
        assert aid is not None
        return aid

    closed_id = _alert("Stripe payouts delayed")
    undone_id = _alert("Old webinar follow-up")
    routed_id = _alert("Q3 pricing page copy stale")
    survivor_id = _alert("Payout delay")
    dup_id = _alert("Payout delay (duplicate)")
    acked_id = _alert("SOC2 evidence request")
    alert_store.set_status(closed_id, "resolved")
    alert_store.set_status(acked_id, "ack")
    alert_store.set_status(undone_id, "dismissed")
    alert_store.mark_superseded(dup_id, survivor_id)

    al.log("alert_review_closed", "Resolved 'Stripe payouts delayed' — vendor confirmed", actor="executive",
           details={"alert_id": closed_id, "headline": "Stripe payouts delayed", "new_status": "resolved",
                    "evidence": "vendor status page confirmed the fix", "evidence_ref": "R2"})
    al.log("alert_review_closed", "Dismissed as stale 'Old webinar follow-up' — no activity", actor="executive",
           details={"alert_id": undone_id, "headline": "Old webinar follow-up", "new_status": "dismissed",
                    "evidence": "no activity in 30 days", "evidence_ref": "A1"})
    al.log("alert_review_routed", "Routed 'Q3 pricing page copy stale' to Dana Kim (proposal 9)",
           actor="executive",
           details={"alert_id": routed_id, "headline": "Q3 pricing page copy stale",
                    "target_person_id": 3, "target_person_name": "Dana Kim", "proposed": True})
    al.log("alert_review_merged", "Merged 'Payout delay (duplicate)' into 'Payout delay'", actor="executive",
           details={"alert_id": dup_id, "headline": "Payout delay (duplicate)",
                    "superseded_by_alert_id": survivor_id, "superseded_by_headline": "Payout delay"})
    al.log("alert_review_escalated", "Escalated 'SOC2 evidence request' to high (sent)", actor="executive",
           details={"alert_id": acked_id, "headline": "SOC2 evidence request", "new_severity": "high",
                    "evidence": "auditor deadline Friday"})
    al.log("watchlist_research_added", "Started watching stock-acme — competitor ticker", actor="executive",
           details={"slug": "stock-acme", "rationale": "competitor ticker named on Sales"})
    al.log("alert_review_changed", "Updated 'Something' — rewritten", actor="executive",
           details={"alert_id": routed_id, "headline": "Something"})
    # The principal undid the dismissal after the review ran.
    assert alert_store.reopen_alert(undone_id)

    res = _make_client().get("/today")
    assert res.status_code == 200
    rows = {r["headline"]: r for r in res.json()["handled_overnight"]}
    assert "Something" not in rows  # rewrites are not "handled"

    closed = rows["Stripe payouts delayed"]
    assert closed["kind"] == "closed" and closed["outcome"] == "resolved"
    assert closed["detail"] == "vendor status page confirmed the fix"
    assert closed["evidence_ref"] == "R2" and closed["event_type"] == "alert_review_closed"
    assert closed["status"] == "resolved" and closed["alert_id"] == closed_id

    undone = rows["Old webinar follow-up"]
    assert undone["outcome"] == "dismissed" and undone["status"] == "open"

    routed = rows["Q3 pricing page copy stale"]
    assert routed["target"] == "Dana Kim" and routed["outcome"] == "proposed"
    assert routed["status"] == "open"

    merged = rows["Payout delay (duplicate)"]
    assert merged["target"] == "Payout delay" and merged["superseded_by_alert_id"] == survivor_id
    assert merged["status"] == "merged"

    # The principal approved this one after the escalation: not "open" (no
    # card to jump to), not reopenable.
    escalated = rows["SOC2 evidence request"]
    assert escalated["target"] == "high" and escalated["detail"] == "auditor deadline Friday"
    assert escalated["status"] == "acked"

    watching = rows["stock-acme"]
    assert watching["kind"] == "watching" and watching["alert_id"] is None
    assert watching["detail"] == "competitor ticker named on Sales" and watching["status"] == ""
