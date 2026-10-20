"""Unit tests for the alerts SQLite store."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.alerts.store import (
    add_mute,
    delete_alert,
    delete_mute,
    get_alert,
    get_alert_by_external,
    initialize_db,
    insert_alert,
    list_alerts,
    list_artifact_alerts,
    list_mutes,
    recent_alerts,
    set_status,
    set_status_by_external,
    update_delivery,
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "alerts.db"
    initialize_db(db_path)
    return db_path


def test_insert_and_read_alert(db: Path) -> None:
    aid = insert_alert(
        source="email",
        external_id="msg-1",
        severity="high",
        headline="Top customer threatening cancellation",
        body="Long body",
        suggested_action="Call them today",
        topic_tags=["customer", "churn"],
        dedup_key="churn-acme",
        db_path=db,
    )
    assert aid is not None

    fetched = get_alert(aid, db_path=db)
    assert fetched is not None
    assert fetched.severity == "high"
    assert fetched.topic_tags == ["customer", "churn"]
    assert fetched.dedup_key == "churn-acme"
    assert fetched.status == "unread"


def test_get_alert_by_external(db: Path) -> None:
    insert_alert(
        source="decision_scheduling",
        external_id="decision:7",
        severity="medium",
        headline="Approve meeting: Sync",
        body="...",
        db_path=db,
    )
    found = get_alert_by_external("decision_scheduling", "decision:7", db_path=db)
    assert found is not None
    assert found.headline == "Approve meeting: Sync"
    # Miss: wrong source / wrong external_id both return None.
    assert get_alert_by_external("other", "decision:7", db_path=db) is None
    assert get_alert_by_external("decision_scheduling", "decision:8", db_path=db) is None


def test_set_status_by_external(db: Path) -> None:
    insert_alert(
        source="decision_scheduling",
        external_id="decision:9",
        severity="medium",
        headline="Approve meeting: Review",
        body="...",
        db_path=db,
    )
    # Hit: returns True and transitions status.
    assert set_status_by_external("decision_scheduling", "decision:9", "ack", db_path=db) is True
    updated = get_alert_by_external("decision_scheduling", "decision:9", db_path=db)
    assert updated is not None
    assert updated.status == "ack"
    # Miss: no matching row → False, harmless no-op.
    assert set_status_by_external("decision_scheduling", "decision:404", "dismissed", db_path=db) is False


def test_set_status_by_external_missing_db(tmp_path: Path) -> None:
    """On a cold system (DB file absent) the clear is a no-op and must NOT
    create a schemaless DB file."""
    missing = tmp_path / "absent.db"
    assert set_status_by_external("decision_scheduling", "decision:1", "ack", db_path=missing) is False
    assert not missing.exists()


def test_idempotent_on_source_external_id(db: Path) -> None:
    first = insert_alert(
        source="email",
        external_id="dup-1",
        severity="medium",
        headline="A",
        body="",
        db_path=db,
    )
    second = insert_alert(
        source="email",
        external_id="dup-1",
        severity="urgent",
        headline="A again",
        body="",
        db_path=db,
    )
    assert first is not None
    assert second is None  # IGNORE'd
    listed = list_alerts(db_path=db)
    assert len(listed) == 1
    assert listed[0].severity == "medium"  # original preserved


def test_status_transitions_and_delete(db: Path) -> None:
    aid = insert_alert(
        source="slack",
        external_id="t-1",
        severity="low",
        headline="hi",
        body="",
        db_path=db,
    )
    assert aid is not None

    assert set_status(aid, "ack", db_path=db) is True
    fetched = get_alert(aid, db_path=db)
    assert fetched is not None
    assert fetched.status == "ack"

    assert list_alerts(status="unread", db_path=db) == []
    assert len(list_alerts(status="ack", db_path=db)) == 1

    assert delete_alert(aid, db_path=db) is True
    assert get_alert(aid, db_path=db) is None


def test_update_delivery(db: Path) -> None:
    aid = insert_alert(
        source="email",
        external_id="d-1",
        severity="high",
        headline="x",
        body="",
        db_path=db,
    )
    assert aid is not None
    update_delivery(aid, attempted=["web", "slack_dm"], delivered=["web"], db_path=db)
    fetched = get_alert(aid, db_path=db)
    assert fetched is not None
    assert fetched.channels_attempted == ["web", "slack_dm"]
    assert fetched.channels_delivered == ["web"]


def test_recent_alerts_ordering(db: Path) -> None:
    for i in range(3):
        insert_alert(
            source="email",
            external_id=f"o-{i}",
            severity="medium",
            headline=f"alert {i}",
            body="",
            db_path=db,
        )
    recent = recent_alerts(limit=5, db_path=db)
    assert [a.headline for a in recent] == ["alert 2", "alert 1", "alert 0"]


def test_recent_alerts_exclude_source_not_starved(db: Path) -> None:
    """exclude_source filters in SQL, so a backlog of the excluded source can't
    starve the wanted rows out of the `limit` page (the activity feed relies on
    this to keep real alerts from being crowded out by decision_scheduling
    companion alerts)."""
    # Three excluded-source alerts inserted most recently, one wanted alert first.
    insert_alert(
        source="triage", external_id="want", severity="high",
        headline="real alert", body="", db_path=db,
    )
    for i in range(3):
        insert_alert(
            source="decision_scheduling", external_id=f"ds-{i}",
            severity="medium", headline=f"booking {i}", body="", db_path=db,
        )

    # limit=2 unfiltered would return only the two newest decision_scheduling rows.
    filtered = recent_alerts(limit=2, db_path=db, exclude_source="decision_scheduling")
    assert [a.headline for a in filtered] == ["real alert"]
    assert all(a.source != "decision_scheduling" for a in filtered)


def test_mute_lifecycle(db: Path) -> None:
    mid = add_mute("hiring", db_path=db)
    assert mid is not None

    # Idempotent on pattern
    again = add_mute("hiring", db_path=db)
    assert again == mid

    listed = list_mutes(db_path=db)
    assert len(listed) == 1
    assert listed[0].pattern == "hiring"

    assert delete_mute(mid, db_path=db) is True
    assert list_mutes(db_path=db) == []


# --- list_artifact_alerts (Executive Artifacts section) ---


def test_list_artifact_alerts_filters_by_source(db: Path) -> None:
    insert_alert(
        source="artifact", external_id="a-1", severity="medium",
        headline="Memo", body="## Body", topic_tags=["artifact"], db_path=db,
    )
    insert_alert(
        source="email", external_id="e-1", severity="high",
        headline="Inbound", body="not an artifact", db_path=db,
    )
    artifacts = list_artifact_alerts(owner_person_id=None, include_unowned=True, db_path=db)
    assert [a.headline for a in artifacts] == ["Memo"]
    assert artifacts[0].source == "artifact"


def test_list_artifact_alerts_includes_acked_and_dismissed(db: Path) -> None:
    """A drafted artifact must remain browsable after it leaves /today."""
    acked = insert_alert(
        source="artifact", external_id="a-ack", severity="medium",
        headline="Acked memo", body="x", db_path=db,
    )
    dismissed = insert_alert(
        source="artifact", external_id="a-dis", severity="low",
        headline="Dismissed memo", body="y", db_path=db,
    )
    assert acked is not None and dismissed is not None
    set_status(acked, "ack", db_path=db)
    set_status(dismissed, "dismissed", db_path=db)
    headlines = {a.headline for a in list_artifact_alerts(owner_person_id=None, include_unowned=True, db_path=db)}
    assert headlines == {"Acked memo", "Dismissed memo"}


def test_list_artifact_alerts_respects_limit_and_order(db: Path) -> None:
    for i in range(5):
        insert_alert(
            source="artifact", external_id=f"a-{i}", severity="medium",
            headline=f"Memo {i}", body="z", db_path=db,
        )
    # Limit caps the result.
    limited = list_artifact_alerts(owner_person_id=None, include_unowned=True, limit=2, db_path=db)
    assert len(limited) == 2

    # Full fetch must be newest-first (DESC). Assert monotonic non-increasing
    # created_at — this fails if ORDER BY flips to ASC.
    full = list_artifact_alerts(owner_person_id=None, include_unowned=True, db_path=db)
    created = [a.created_at for a in full]
    assert created == sorted(created, reverse=True)
    # The capped result is the newest slice of that ordering.
    assert [a.id for a in limited] == [a.id for a in full[:2]]


def test_list_artifact_alerts_empty_on_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    assert list_artifact_alerts(owner_person_id=None, include_unowned=True, db_path=missing) == []


def test_list_artifact_alerts_keeps_to_one_owner(db: Path) -> None:
    for ext, owner in (("mine", 7), ("theirs", 8), ("unowned", None)):
        insert_alert(
            source="artifact", external_id=ext, severity="low", headline=ext,
            body="b", owner_person_id=owner, db_path=db,
        )

    def heads(**kw: object) -> set[str]:
        return {a.headline for a in list_artifact_alerts(db_path=db, **kw)}  # type: ignore[arg-type]

    assert heads(owner_person_id=7) == {"mine"}
    assert heads(owner_person_id=7, include_unowned=True) == {"mine", "unowned"}
    assert heads(owner_person_id=None) == set()
