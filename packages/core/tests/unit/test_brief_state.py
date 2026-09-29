"""Delivered-brief state: window, new-vs-carried split, fingerprint, handled."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.briefing import brief_state, narrative_cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "cache.db")


def _proposal(alert_id: int, *, hours_ago: float, **extra: object) -> dict:
    return {
        "alert_id": alert_id,
        "headline": f"item {alert_id}",
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
        **extra,
    }


def test_since_for_defaults_to_24h_then_last_delivery() -> None:
    now = datetime.now(UTC)
    cold = brief_state.since_for("principal_brief_morning", now=now)
    assert abs((now - cold) - timedelta(hours=24)) < timedelta(seconds=5)

    brief_state.record_delivered("principal_brief_morning", "fp1", "text")
    warm = brief_state.since_for("principal_brief_morning", now=now + timedelta(hours=3))
    assert now - timedelta(seconds=5) <= warm <= now + timedelta(seconds=5)

    # A delivery stamped AFTER `now` (clock skew) never yields a future window.
    skewed = brief_state.since_for("principal_brief_morning", now=now - timedelta(hours=1))
    assert skewed <= now - timedelta(hours=1)

    # A delivery older than a week is clamped to 7 days back.
    narrative_cache.put(narrative_cache.BriefingNarrative(
        scope=brief_state.scope_for("principal_brief_eod"), input_hash="x",
        narrative_text="t", generated_at=(now - timedelta(days=30)).isoformat(),
    ))
    clamped = brief_state.since_for("principal_brief_eod", now=now)
    assert abs((now - clamped) - timedelta(days=7)) < timedelta(seconds=5)


def test_record_and_last_delivered_roundtrip_per_kind() -> None:
    assert brief_state.last_delivered("principal_brief_morning") is None
    brief_state.record_delivered("principal_brief_morning", "fp-m", "morning text")
    brief_state.record_delivered("principal_brief_eod", "fp-e", "eod text")
    m = brief_state.last_delivered("principal_brief_morning")
    e = brief_state.last_delivered("principal_brief_eod")
    assert m is not None and m.input_hash == "fp-m" and m.narrative_text == "morning text"
    assert e is not None and e.input_hash == "fp-e"
    # The per-viewer header cache is untouched by the brief namespace.
    assert narrative_cache.get("principal") is None


def test_split_proposals_by_window() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    new, carried = brief_state.split_proposals(
        [_proposal(1, hours_ago=1), _proposal(2, hours_ago=30), {"alert_id": 3}], since
    )
    assert [p["alert_id"] for p in new] == [1, 3]  # unknown created_at counts as new
    assert [p["alert_id"] for p in carried] == [2]
    all_new, none = brief_state.split_proposals([_proposal(1, hours_ago=100)], None)
    assert len(all_new) == 1 and none == []


def test_fingerprint_is_order_independent_and_date_free() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    a = _proposal(1, hours_ago=1)
    b = _proposal(2, hours_ago=2)
    old = _proposal(3, hours_ago=40, review_verdict="likely_stale")
    act1 = {"kind": "dm_sent", "summary": "DM'd Dana", "at": "2026-09-11T08:00:00+00:00"}
    act2 = {"kind": "decision_logged", "summary": "Chose vendor", "at": "2026-09-11T09:00:00+00:00"}
    handled = [{"kind": "routed", "summary": "Routed X to Dana", "at": "2026-09-11T07:00:00+00:00"}]
    depts = [{"slug": "sales", "at_risk_count": 1, "off_track_count": 0}]
    people = [{"id": 5, "awaiting_count": 2}, {"id": 6, "awaiting_count": 0}]

    fp1 = brief_state.build_brief_fingerprint(
        today_data={"proposals": [a, b, old], "departments": depts, "people": people},
        activity=[act1, act2], handled=handled, since=since,
    )
    fp2 = brief_state.build_brief_fingerprint(
        today_data={"proposals": [old, b, a], "departments": depts, "people": list(reversed(people))},
        activity=[act2, act1], handled=handled, since=since,
    )
    assert fp1 == fp2
    # Same activity a day later (different stamps) → same fingerprint: the
    # suppression must be able to fire on a day whose activity is unchanged.
    later = [{**act1, "at": "2026-09-12T08:00:00+00:00"}, {**act2, "at": "2026-09-12T09:00:00+00:00"}]
    fp_later = brief_state.build_brief_fingerprint(
        today_data={"proposals": [a, b, old], "departments": depts, "people": people},
        activity=later, handled=handled, since=since,
    )
    assert fp_later == fp1
    # A new proposal, a resolved carried item, or a new handled move changes it.
    fp3 = brief_state.build_brief_fingerprint(
        today_data={"proposals": [a, b, old, _proposal(9, hours_ago=0.5)], "departments": depts, "people": people},
        activity=[act1, act2], handled=handled, since=since,
    )
    fp4 = brief_state.build_brief_fingerprint(
        today_data={"proposals": [a, b], "departments": depts, "people": people},
        activity=[act1, act2], handled=handled, since=since,
    )
    fp5 = brief_state.build_brief_fingerprint(
        today_data={"proposals": [a, b, old], "departments": depts, "people": people},
        activity=[act1, act2], handled=[], since=since,
    )
    assert len({fp1, fp3, fp4, fp5}) == 4


def test_suppressed_line_pluralises() -> None:
    assert brief_state.suppressed_line(1).endswith("1 item still waiting on you.")
    assert brief_state.suppressed_line(12).endswith("12 items still waiting on you.")


def test_handled_since_reads_review_audit_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.audit import logger as audit_logger

    al = audit_logger.AuditLogger(db_path=tmp_path / "audit.db")
    al.initialize_db()
    monkeypatch.setattr(audit_logger, "get_audit_logger", lambda: al)
    al.log("alert_review_routed", "Routed 'Acme renewal' to Dana", actor="executive")
    al.log("alert_review_closed", "Resolved 'Stripe incident' — vendor marked resolved", actor="executive")
    al.log("watchlist_research_added", "Started watching stock-acme — competitor ticker", actor="executive")
    al.log("watchlist_auto_disabled", "Stopped watching rss-noise — dismissed 3 of 6", actor="scheduler")
    al.log("tool_invocation", "unrelated", actor="executive")

    handled = brief_state.handled_since(datetime.now(UTC) - timedelta(hours=1))
    kinds = sorted(h["kind"] for h in handled)
    assert kinds == ["closed", "routed", "stopped_watching", "watching"]
    assert all(h["summary"] and h["at"] for h in handled)
    assert brief_state.handled_since(datetime.now(UTC) + timedelta(hours=1)) == []


def test_fingerprint_moves_with_pending_watch_suggestions() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    base = dict(today_data={"proposals": [], "departments": [], "people": []}, activity=[], handled=[], since=since)
    assert brief_state.build_brief_fingerprint(**base) == brief_state.build_brief_fingerprint(**base, pending_watch_suggestions=0)
    assert brief_state.build_brief_fingerprint(**base) != brief_state.build_brief_fingerprint(**base, pending_watch_suggestions=2)


def test_pending_watch_suggestions_counts_dry_run_research_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts.store import initialize_db as init_alerts
    from openexecutive.memory.episodic import initialize_db as init_episodic
    from openexecutive.monitoring import store as ms

    db = tmp_path / "w.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db)
    init_episodic(db)
    init_alerts(db)
    ms.initialize_db(db)
    assert brief_state.pending_watch_suggestions() == 0
    ms.insert_watchlist_item(slug="rss-a", signal_type="rss", target="https://a.com/f",
                             mode="dry_run", origin="research_proposed", db_path=db)
    ms.insert_watchlist_item(slug="rss-b", signal_type="rss", target="https://b.com/f", db_path=db)
    assert brief_state.pending_watch_suggestions() == 1


def test_handled_since_excludes_rewrites_and_strips_nudge_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.audit import logger as audit_logger

    al = audit_logger.AuditLogger(db_path=tmp_path / "audit.db")
    al.initialize_db()
    monkeypatch.setattr(audit_logger, "get_audit_logger", lambda: al)
    # A rewrite leaves the alert open: it is not "handled" and must not be listed.
    al.log("alert_review_changed", "Updated 'Old headline' — now 7 merchants", actor="executive",
           details={"alert_id": 4, "headline": "Old headline"})
    al.log("alert_review_nudged", "[alert 7] Nudged Dana Kim about 'Acme renewal'", actor="executive",
           details={"alert_id": 7, "headline": "Acme renewal", "target_person_name": "Dana Kim"})

    handled = brief_state.handled_since(datetime.now(UTC) - timedelta(hours=1))
    assert [h["kind"] for h in handled] == ["nudged"]
    row = handled[0]
    assert row["summary"] == "Nudged Dana Kim about 'Acme renewal'"
    assert row["event_type"] == "alert_review_nudged"
    assert row["alert_id"] == 7
    assert row["details"]["target_person_name"] == "Dana Kim"
    assert "alert_review_changed" not in brief_state.HANDLED_EVENT_KINDS


def test_rewritten_since_picks_changed_verdicts_reviewed_in_window() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    inside = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    before = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    proposals = [
        {"alert_id": 1, "review_verdict": "changed", "last_reviewed_at": inside},
        {"alert_id": 2, "review_verdict": "changed", "last_reviewed_at": before},
        {"alert_id": 3, "review_verdict": "relevant", "last_reviewed_at": inside},
        {"alert_id": 4, "review_verdict": "changed"},
    ]
    # 4 has no stamp: reported (fail open, like split_proposals), never dropped.
    assert [p["alert_id"] for p in brief_state.rewritten_since(proposals, since)] == [1, 4]
    assert brief_state.rewritten_since(proposals, None) == []


def test_fingerprint_moves_when_a_carried_item_is_rewritten() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    old = _proposal(5, hours_ago=40)
    base = dict(activity=[], handled=[], since=since)
    fp_plain = brief_state.build_brief_fingerprint(
        today_data={"proposals": [old], "departments": [], "people": []}, **base,
    )
    rewritten = {**old, "review_verdict": "changed", "review_note": "now 7 merchants",
                 "last_reviewed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    fp_rewritten = brief_state.build_brief_fingerprint(
        today_data={"proposals": [rewritten], "departments": [], "people": []}, **base,
    )
    assert fp_plain != fp_rewritten
    # Re-running with the same note (a later timestamp) is still the same brief.
    again = {**rewritten, "last_reviewed_at": datetime.now(UTC).isoformat()}
    assert brief_state.build_brief_fingerprint(
        today_data={"proposals": [again], "departments": [], "people": []}, **base,
    ) == fp_rewritten


def test_fingerprint_counts_the_live_world_only_when_given() -> None:
    since = datetime.now(UTC) - timedelta(hours=12)
    base = {"today_data": {}, "activity": [], "handled": [], "since": since}
    legacy = brief_state.build_brief_fingerprint(**base)
    # Nothing live → the fingerprint is exactly what it was before.
    empty_live = {"inbound": [], "stuck": [], "drafts": 0, "conversations": [], "calendar": ""}
    assert brief_state.build_brief_fingerprint(**base, live_keys=empty_live) == legacy
    mail = brief_state.build_brief_fingerprint(
        **base, live_keys={**empty_live, "inbound": ["sam@x.com|renewal|1"]},
    )
    assert mail != legacy
    assert brief_state.build_brief_fingerprint(**base, reflection_flags="- x") != legacy


def test_reflection_flags_since_reads_the_latest_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows import persistence

    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "runs.db")
    persistence.initialize_runs_db()
    since = datetime.now(UTC) - timedelta(hours=1)
    assert brief_state.reflection_flags_since(since) == ""
    persistence.create_run("r", "executive_reflection", "Executive Reflection", {})
    persistence.complete_run(
        "r", "**Flagged for the brief:**\n- Board deck due Friday\n\n**Quiet:** rest is calm",
    )
    assert brief_state.reflection_flags_since(since) == "- Board deck due Friday"
    later = datetime.now(UTC) + timedelta(minutes=1)
    assert brief_state.reflection_flags_since(later) == ""  # older than the window
    persistence.create_run("s", "executive_reflection", "Executive Reflection", {})
    persistence.complete_run("s", "**Acted on:**\n- DM'd Sam")
    assert brief_state.reflection_flags_since(since) == ""  # latest has no flags
