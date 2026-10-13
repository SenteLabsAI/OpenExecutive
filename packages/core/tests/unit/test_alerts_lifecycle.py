"""Alert lifecycle: TTL, live view, expiry sweep, and the new store helpers."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.alerts import lifecycle
from openexecutive.alerts import store as alert_store
from openexecutive.alerts.store import (
    bulk_set_status,
    coalesce_alert,
    count_superseded_by,
    get_alert,
    initialize_db,
    insert_alert,
    mark_superseded,
    reopen_alert,
    set_review,
    update_alert_content,
    update_alert_routing,
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "alerts.db"
    initialize_db(db_path)
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(lifecycle, "_ttl_settings", lambda: (3, 14))
    return db_path


def _backdate(db: Path, alert_id: int, days: float) -> None:
    ts = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE alerts SET created_at=? WHERE id=?", (ts, alert_id))
        conn.commit()


def _insert(db: Path, **kw: object) -> int:
    defaults: dict = dict(
        source="triage", external_id=None, severity="medium",
        headline="h", body="b", db_path=db,
    )
    defaults.update(kw)
    if defaults["external_id"] is None:
        defaults["external_id"] = f"ext-{datetime.now(UTC).timestamp()}-{kw.get('headline', 'h')}"
    aid = insert_alert(**defaults)
    assert aid is not None
    return aid


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


def test_migration_adds_lifecycle_columns_to_existing_db(tmp_path: Path) -> None:
    """A DB created by an older build gains the new columns with defaults."""
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(str(legacy)) as conn:
        conn.executescript(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT, source TEXT NOT NULL, severity TEXT NOT NULL,
                headline TEXT NOT NULL, body TEXT NOT NULL,
                suggested_action TEXT DEFAULT '', topic_tags TEXT DEFAULT '[]',
                channels_attempted TEXT DEFAULT '[]', channels_delivered TEXT DEFAULT '[]',
                dedup_key TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'unread',
                created_at TEXT NOT NULL, UNIQUE(source, external_id)
            );
            INSERT INTO alerts (external_id, source, severity, headline, body, created_at)
            VALUES ('x', 'email', 'high', 'old row', 'b', '2026-09-01T00:00:00+00:00');
            """
        )
    initialize_db(legacy)
    initialize_db(legacy)  # idempotent
    row = get_alert(1, db_path=legacy)
    assert row is not None
    assert row.occurrence_count == 1
    assert row.review_verdict == ""
    assert row.last_seen_at is None
    assert row.snoozed_until is None


# --------------------------------------------------------------------------- #
# TTL predicate
# --------------------------------------------------------------------------- #


def test_ttl_monitoring_vs_action(db: Path) -> None:
    monitoring = _insert(db, source="stock", severity="low", topic_tags=["external:stock"])
    action = _insert(db, source="triage", severity="high")
    now = datetime.now(UTC)
    m = get_alert(monitoring, db_path=db)
    a = get_alert(action, db_path=db)
    assert m is not None and a is not None
    assert lifecycle.ttl_days_for(m, monitoring_days=3, action_days=14) == 3
    assert lifecycle.ttl_days_for(a, monitoring_days=3, action_days=14) == 14
    # Fresh rows are live under both TTLs.
    assert not lifecycle.is_expired(m, now, monitoring_days=3, action_days=14)
    assert not lifecycle.is_expired(a, now, monitoring_days=3, action_days=14)
    _backdate(db, monitoring, 4)
    _backdate(db, action, 4)
    m = get_alert(monitoring, db_path=db)
    a = get_alert(action, db_path=db)
    assert m is not None and a is not None
    assert lifecycle.is_expired(m, now, monitoring_days=3, action_days=14)
    assert not lifecycle.is_expired(a, now, monitoring_days=3, action_days=14)


def test_zero_ttl_disables_expiry(db: Path) -> None:
    aid = _insert(db, source="triage", severity="high")
    _backdate(db, aid, 400)
    a = get_alert(aid, db_path=db)
    assert a is not None
    assert lifecycle.ttl_days_for(a, monitoring_days=3, action_days=0) is None
    assert not lifecycle.is_expired(a, monitoring_days=3, action_days=0)


def test_a_snooze_restarts_the_ttl_so_the_card_comes_back(db: Path) -> None:
    aid = _insert(db, source="stock", severity="low", topic_tags=["external:stock"])
    _backdate(db, aid, 2)
    now = datetime.now(UTC)
    # Snoozed for two days: past the 3-day monitoring TTL counted from creation.
    alert_store.set_snoozed_until(aid, (now + timedelta(days=2)).isoformat(), db_path=db)
    a = get_alert(aid, db_path=db)
    assert a is not None
    later = now + timedelta(days=2, hours=1)
    assert not lifecycle.is_expired(a, later, monitoring_days=3, action_days=14)
    assert lifecycle.is_live(a, later)
    assert not lifecycle.is_live(a, now)


def test_exempt_sources_never_expire(db: Path) -> None:
    art = _insert(db, source="artifact", severity="medium", topic_tags=["artifact"])
    dec = _insert(db, source="decision_scheduling", severity="medium")
    _backdate(db, art, 90)
    _backdate(db, dec, 90)
    for aid in (art, dec):
        a = get_alert(aid, db_path=db)
        assert a is not None
        assert lifecycle.ttl_days_for(a, monitoring_days=3, action_days=14) is None
        assert not lifecycle.is_expired(a, monitoring_days=3, action_days=14)


# --------------------------------------------------------------------------- #
# Live view + sweep
# --------------------------------------------------------------------------- #


def test_list_live_alerts_skips_past_ttl_and_snoozed_before_any_sweep(db: Path) -> None:
    live = _insert(db, headline="live")
    stale = _insert(db, headline="stale")
    snoozed = _insert(db, headline="snoozed")
    _backdate(db, stale, 20)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE alerts SET snoozed_until=? WHERE id=?",
            ((datetime.now(UTC) + timedelta(hours=3)).isoformat(), snoozed),
        )
        conn.commit()
    ids = {a.id for a in lifecycle.list_live_alerts(db_path=db)}
    assert ids == {live}
    # Still unread on disk — the view hides, the sweep persists.
    stale_row = get_alert(stale, db_path=db)
    assert stale_row is not None and stale_row.status == "unread"


def test_list_live_alerts_not_starved_by_expired_rows(db: Path) -> None:
    """A run of expired rows newer than the live one must not push it out."""
    old_live = _insert(db, headline="old but live")
    _backdate(db, old_live, 1)
    for i in range(12):
        aid = _insert(db, headline=f"stale-{i}")
        _backdate(db, aid, 15 + i / 100)
    # Expired rows are older, so they sort after; the live one leads.
    rows = lifecycle.list_live_alerts(limit=5, db_path=db)
    assert [a.id for a in rows] == [old_live]


def test_expire_stale_alerts_marks_expired_and_audits(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict]] = []

    def fake_audit(event_type: str, summary: str, **kw: object) -> None:
        events.append((event_type, dict(kw.get("details") or {})))

    import openexecutive.audit as audit_pkg

    monkeypatch.setattr(audit_pkg, "log_event", fake_audit)

    live = _insert(db, headline="live")
    stale_action = _insert(db, headline="stale action", severity="high")
    stale_monitoring = _insert(
        db, headline="stale mon", source="rss", severity="low", topic_tags=["external:rss"]
    )
    exempt = _insert(db, headline="artifact", source="artifact", topic_tags=["artifact"])
    _backdate(db, stale_action, 15)
    _backdate(db, stale_monitoring, 4)
    _backdate(db, exempt, 60)

    count = lifecycle.expire_stale_alerts(db_path=db)
    assert count == 2
    assert get_alert(stale_action, db_path=db).status == "expired"  # type: ignore[union-attr]
    assert get_alert(stale_monitoring, db_path=db).status == "expired"  # type: ignore[union-attr]
    assert get_alert(live, db_path=db).status == "unread"  # type: ignore[union-attr]
    assert get_alert(exempt, db_path=db).status == "unread"  # type: ignore[union-attr]
    assert events and events[0][0] == "alert_sweep"
    assert events[0][1]["count"] == 2
    assert set(events[0][1]["alert_ids"]) == {stale_action, stale_monitoring}

    # Second sweep: nothing left to do, no audit row.
    events.clear()
    assert lifecycle.expire_stale_alerts(db_path=db) == 0
    assert events == []


def test_expire_stale_alerts_never_raises(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> list:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(alert_store, "list_alerts", boom)
    assert lifecycle.expire_stale_alerts(db_path=db) == 0


# --------------------------------------------------------------------------- #
# Store helpers
# --------------------------------------------------------------------------- #


def test_coalesce_alert_bumps_count_last_seen_and_severity(db: Path) -> None:
    aid = _insert(db, source="stock", dedup_key="watch:stock-aapl", severity="low", body="v1")
    hit = coalesce_alert(
        source="stock", dedup_key="watch:stock-aapl", severity="medium", body="v2", db_path=db
    )
    assert hit == (aid, True)
    row = get_alert(aid, db_path=db)
    assert row is not None
    assert row.occurrence_count == 2
    assert row.last_seen_at is not None
    assert row.body == "v2"
    assert row.severity == "medium"
    # A milder repeat never lowers severity, and reports no escalation.
    hit = coalesce_alert(
        source="stock", dedup_key="watch:stock-aapl", severity="low", body="v3", db_path=db
    )
    assert hit == (aid, False)
    row = get_alert(aid, db_path=db)
    assert row is not None and row.severity == "medium" and row.occurrence_count == 3


def test_coalesce_alert_ignores_closed_rows_and_requires_key(db: Path) -> None:
    aid = _insert(db, source="email", dedup_key="churn-acme")
    alert_store.set_status(aid, "ack", db_path=db)
    assert coalesce_alert(
        source="email", dedup_key="churn-acme", severity="high", body="x", db_path=db
    ) is None
    assert coalesce_alert(
        source="email", dedup_key="", severity="high", body="x", db_path=db
    ) is None
    # Different source, same key → no match either.
    _insert(db, source="slack", dedup_key="k1")
    assert coalesce_alert(
        source="email", dedup_key="k1", severity="high", body="x", db_path=db
    ) is None


def test_bulk_set_status_by_ids_and_by_age_with_category(db: Path) -> None:
    a1 = _insert(db, headline="a1")
    a2 = _insert(db, headline="a2")
    m1 = _insert(db, headline="m1", source="rss", severity="low", topic_tags=["external:rss"])
    _backdate(db, a2, 10)
    _backdate(db, m1, 10)

    assert bulk_set_status("dismissed", alert_ids=[a1], db_path=db) == [a1]
    assert get_alert(a1, db_path=db).status == "dismissed"  # type: ignore[union-attr]
    # Already dismissed → not unread → not counted again.
    assert bulk_set_status("dismissed", alert_ids=[a1], db_path=db) == []

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    assert bulk_set_status("dismissed", before=cutoff, category="monitoring", db_path=db) == [m1]
    assert get_alert(m1, db_path=db).status == "dismissed"  # type: ignore[union-attr]
    assert get_alert(a2, db_path=db).status == "unread"  # type: ignore[union-attr]
    assert bulk_set_status("ack", before=cutoff, category="action", db_path=db) == [a2]
    assert get_alert(a2, db_path=db).status == "ack"  # type: ignore[union-attr]
    # No selector → no-op.
    assert bulk_set_status("ack", db_path=db) == []
    assert bulk_set_status("ack", alert_ids=[], db_path=db) == []


def test_update_content_review_routing_and_reopen(db: Path) -> None:
    aid = _insert(db, headline="old", body="old body", severity="low")
    assert update_alert_content(aid, headline="new", severity="high", db_path=db)
    row = get_alert(aid, db_path=db)
    assert row is not None and row.headline == "new" and row.body == "old body"
    assert row.severity == "high"
    assert not update_alert_content(aid, db_path=db)  # nothing to set

    assert set_review(
        aid, verdict="likely_stale", note="no activity in 9 days",
        recommended_move="close", why_now="", db_path=db,
    )
    row = get_alert(aid, db_path=db)
    assert row is not None
    assert row.review_verdict == "likely_stale"
    assert row.review_note == "no activity in 9 days"
    assert row.recommended_move == "close"
    assert row.last_reviewed_at is not None

    assert update_alert_routing(aid, 7, db_path=db)
    assert get_alert(aid, db_path=db).routed_to_person_id == 7  # type: ignore[union-attr]

    alert_store.set_status(aid, "expired", db_path=db)
    assert reopen_alert(aid, db_path=db)
    row = get_alert(aid, db_path=db)
    assert row is not None and row.status == "unread" and row.review_verdict == ""
    assert row.last_reviewed_at is None and row.why_now == "" and row.due_at is None
    assert not reopen_alert(aid, db_path=db)  # already unread
    assert not reopen_alert(9999, db_path=db)
    # `ack` (approved + executed) is not an Undo target; exempt sources never reopen.
    alert_store.set_status(aid, "ack", db_path=db)
    assert not reopen_alert(aid, db_path=db)
    art = _insert(db, source="artifact", topic_tags=["artifact"])
    alert_store.set_status(art, "dismissed", db_path=db)
    assert not reopen_alert(art, db_path=db, exclude_sources=("artifact",))
    assert reopen_alert(art, db_path=db)  # without the exclusion the store itself allows it


def test_mark_superseded_folds_into_survivor(db: Path) -> None:
    survivor = _insert(db, headline="story")
    dup = _insert(db, headline="dup")
    assert mark_superseded(dup, survivor, db_path=db)
    d = get_alert(dup, db_path=db)
    s = get_alert(survivor, db_path=db)
    assert d is not None and d.status == "dismissed" and d.superseded_by_alert_id == survivor
    assert s is not None and s.occurrence_count == 2  # absorbs the dup's own count (1)
    assert count_superseded_by(db_path=db) == {survivor: 1}
    # Self-merge, missing survivor, or already-closed row → False.
    assert not mark_superseded(survivor, survivor, db_path=db)
    assert not mark_superseded(dup, 9999, db_path=db)
    assert not mark_superseded(dup, survivor, db_path=db)


def test_is_expired_uses_last_seen_as_the_age_anchor(db: Path) -> None:
    """A situation that keeps re-firing stays live: coalescing bumps last_seen_at."""
    aid = _insert(db, source="stock", severity="low", topic_tags=["external:stock"], dedup_key="watch:x")
    _backdate(db, aid, 10)  # created well past the 3-day monitoring TTL
    assert lifecycle.is_expired(get_alert(aid, db_path=db), monitoring_days=3, action_days=14)  # type: ignore[arg-type]
    coalesce_alert(source="stock", dedup_key="watch:x", severity="low", body="again", db_path=db)
    assert not lifecycle.is_expired(get_alert(aid, db_path=db), monitoring_days=3, action_days=14)  # type: ignore[arg-type]
    assert lifecycle.is_live(get_alert(aid, db_path=db))  # type: ignore[arg-type]


def test_mute_pattern_allowed_rejects_broad_patterns(db: Path) -> None:
    from openexecutive.alerts.models import Alert

    alert = Alert(source="rss", severity="low", headline="h", body="b", created_at="2026-09-11T00:00:00+00:00",
                  topic_tags=["external:rss", "external:acme-news", "press"])
    assert lifecycle.mute_pattern_allowed(alert, "external:acme-news")
    assert lifecycle.mute_pattern_allowed(alert, "press")
    assert lifecycle.mute_pattern_allowed(alert, "department:finance")
    assert not lifecycle.mute_pattern_allowed(alert, "e")
    assert not lifecycle.mute_pattern_allowed(alert, ":")
    assert not lifecycle.mute_pattern_allowed(alert, "finance")  # not this alert's tag, not scoped
    assert not lifecycle.mute_pattern_allowed(alert, "external:")


def test_reopen_makes_a_past_ttl_row_live_again(db: Path) -> None:
    aid = _insert(db, headline="old but wanted")
    _backdate(db, aid, 20)
    assert lifecycle.expire_stale_alerts(db_path=db) == 1
    assert reopen_alert(aid, db_path=db)
    assert lifecycle.is_live(get_alert(aid, db_path=db))  # type: ignore[arg-type]
    assert lifecycle.expire_stale_alerts(db_path=db) == 0  # not re-expired by the next sweep
    assert [a.id for a in lifecycle.list_live_alerts(db_path=db)] == [aid]


def test_reopen_after_merge_hands_back_the_absorbed_count(db: Path) -> None:
    survivor = _insert(db, headline="story")
    dup = _insert(db, headline="dup")
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE alerts SET occurrence_count = 4 WHERE id = ?", (dup,))
        conn.commit()
    assert mark_superseded(dup, survivor, db_path=db)
    assert get_alert(survivor, db_path=db).occurrence_count == 5  # type: ignore[union-attr]
    assert reopen_alert(dup, db_path=db)
    assert get_alert(survivor, db_path=db).occurrence_count == 1  # type: ignore[union-attr]
    assert get_alert(dup, db_path=db).superseded_by_alert_id is None  # type: ignore[union-attr]


def test_bulk_age_cutoff_uses_last_seen_anchor(db: Path) -> None:
    refiring = _insert(db, headline="refiring", source="stock", dedup_key="watch:y", severity="low",
                       topic_tags=["external:stock"])
    idle = _insert(db, headline="idle")
    _backdate(db, refiring, 10)
    _backdate(db, idle, 10)
    coalesce_alert(source="stock", dedup_key="watch:y", severity="low", body="again", db_path=db)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    assert bulk_set_status("dismissed", before=cutoff, db_path=db) == [idle]
