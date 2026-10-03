"""Alert queue-grooming routes: ack feedback + mute, bulk-ack, reopen, review."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import lifecycle
from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import alerts as alerts_route
from openexecutive.memory import episodic
from openexecutive.monitoring import store as monitoring_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "alerts.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(lifecycle, "_ttl_settings", lambda: (3, 14))
    alert_store.initialize_db(db_path)
    episodic.initialize_db(db_path)
    monitoring_store.initialize_db(db_path)
    return db_path


@pytest.fixture()
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(alerts_route.router)
    return TestClient(app)


def _insert(db: Path, headline: str, **kw: object) -> int:
    params: dict = dict(
        source="triage", external_id=f"ext-{headline}", severity="medium",
        headline=headline, body="b", db_path=db,
    )
    params.update(kw)
    aid = alert_store.insert_alert(**params)
    assert aid is not None
    return aid


def _watch(db: Path, slug: str) -> None:
    monitoring_store.insert_watchlist_item(
        slug=slug, signal_type="rss", target="https://example.com/feed", db_path=db,
    )


def test_single_ack_still_works_and_teaches_the_watch(client: TestClient, db: Path) -> None:
    _watch(db, "acme-news")
    aid = _insert(db, "acme raised", source="rss", topic_tags=["external:rss", "external:acme-news"])
    res = client.post(f"/alerts/{aid}/ack", json={"status": "dismissed"})
    assert res.status_code == 200 and res.json()["status"] == "dismissed"
    item = monitoring_store.get_watchlist_item_by_slug("acme-news", db_path=db)
    assert item is not None and item.dismiss_count == 1 and item.trust_score == pytest.approx(0.8)

    # Approving a later alert from the same watch recovers a little trust.
    aid2 = _insert(db, "acme again", source="rss", topic_tags=["external:rss", "external:acme-news"])
    client.post(f"/alerts/{aid2}/ack", json={"status": "ack"})
    item = monitoring_store.get_watchlist_item_by_slug("acme-news", db_path=db)
    assert item is not None and item.trust_score == pytest.approx(0.85)
    assert client.post("/alerts/9999/ack", json={"status": "ack"}).status_code == 404
    assert client.post(f"/alerts/{aid}/ack", json={"status": "expired"}).status_code == 422
    # Re-posting the same status is idempotent for the feedback loop.
    client.post(f"/alerts/{aid}/ack", json={"status": "dismissed"})
    client.post(f"/alerts/{aid}/ack", json={"status": "dismissed"})
    item = monitoring_store.get_watchlist_item_by_slug("acme-news", db_path=db)
    assert item is not None and item.dismiss_count == 1


def test_ack_with_mute_topic_adds_a_mute(client: TestClient, db: Path) -> None:
    aid = _insert(db, "newsletter", topic_tags=["external:rss", "external:acme-news"])
    res = client.post(f"/alerts/{aid}/ack", json={"status": "dismissed", "mute_topic": True})
    assert res.status_code == 200
    assert [m.pattern for m in alert_store.list_mutes(db_path=db)] == ["external:acme-news"]
    aid2 = _insert(db, "finance thing", topic_tags=["finance", "department:finance"])
    client.post(f"/alerts/{aid2}/ack", json={"status": "dismissed", "mute_topic": "finance"})
    assert sorted(m.pattern for m in alert_store.list_mutes(db_path=db)) == ["external:acme-news", "finance"]
    # An ack (not a dismiss) never mutes, even if asked.
    aid3 = _insert(db, "keep", topic_tags=["customer"])
    client.post(f"/alerts/{aid3}/ack", json={"status": "ack", "mute_topic": True})
    assert len(alert_store.list_mutes(db_path=db)) == 2
    # A broad pattern (substring-matched against every future alert) is refused.
    aid4 = _insert(db, "broad", topic_tags=["customer"])
    client.post(f"/alerts/{aid4}/ack", json={"status": "dismissed", "mute_topic": "e"})
    assert len(alert_store.list_mutes(db_path=db)) == 2


def test_bulk_ack_by_ids_and_by_age(client: TestClient, db: Path) -> None:
    a = _insert(db, "a")
    b = _insert(db, "b")
    old_mon = _insert(db, "old mon", source="rss", severity="low", topic_tags=["external:rss"])
    old_art = _insert(db, "old artifact", source="artifact", topic_tags=["artifact"])
    with sqlite3.connect(str(db)) as conn:
        for aid in (old_mon, old_art):
            conn.execute(
                "UPDATE alerts SET created_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(days=10)).isoformat(), aid),
            )
        conn.commit()

    res = client.post("/alerts/bulk-ack", json={"status": "dismissed", "alert_ids": [a, b]})
    assert res.status_code == 200 and res.json() == {"count": 2}
    res = client.post(
        "/alerts/bulk-ack",
        json={"status": "dismissed", "older_than_days": 7, "category": "monitoring"},
    )
    assert res.json() == {"count": 1}
    assert alert_store.get_alert(old_mon, db_path=db).status == "dismissed"  # type: ignore[union-attr]
    # Exempt sources are never bulk-closed by an age sweep.
    assert alert_store.get_alert(old_art, db_path=db).status == "unread"  # type: ignore[union-attr]
    assert client.post("/alerts/bulk-ack", json={"status": "ack"}).status_code == 400
    assert client.post("/alerts/bulk-ack", json={"status": "read", "alert_ids": [a]}).status_code == 422
    # A 0-day sweep would close the whole company queue → rejected.
    assert client.post("/alerts/bulk-ack", json={"status": "dismissed", "older_than_days": 0}).status_code == 422
    assert client.post("/alerts/bulk-ack", json={"status": "ack", "alert_ids": []}).json() == {"count": 0}
    assert client.post("/alerts/bulk-ack", json={"status": "ack", "alert_ids": list(range(501))}).status_code == 422


def test_bulk_ack_teaches_the_watch_like_a_single_dismiss(client: TestClient, db: Path) -> None:
    _watch(db, "acme-news")
    ids = [
        _insert(db, f"n{i}", source="rss", severity="low", topic_tags=["external:rss", "external:acme-news"])
        for i in range(2)
    ]
    assert client.post("/alerts/bulk-ack", json={"status": "dismissed", "alert_ids": ids}).json() == {"count": 2}
    item = monitoring_store.get_watchlist_item_by_slug("acme-news", db_path=db)
    assert item is not None and item.dismiss_count == 2 and item.trust_score == pytest.approx(0.64)


def test_reopen_brings_a_closed_alert_back(
    client: TestClient, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator.artifact_records import Viewer

    monkeypatch.setattr(alerts_route, "_caller_viewer", lambda request: Viewer(person_id=7))
    aid = _insert(db, "closed")
    alert_store.set_status(aid, "expired", db_path=db)
    alert_store.set_review(aid, verdict="likely_stale", note="x", db_path=db)
    res = client.post(f"/alerts/{aid}/reopen")
    assert res.status_code == 200
    assert res.json()["status"] == "unread" and res.json()["review_verdict"] == ""
    assert client.post(f"/alerts/{aid}/reopen").status_code == 409
    assert client.post("/alerts/9999/reopen").status_code == 404
    # `ack` and exempt sources are not Undo targets.
    acked = _insert(db, "approved")
    alert_store.set_status(acked, "ack", db_path=db)
    assert client.post(f"/alerts/{acked}/reopen").status_code == 409
    art = _insert(db, "doc", source="artifact", topic_tags=["artifact"], owner_person_id=7)
    alert_store.set_status(art, "dismissed", db_path=db)
    assert client.post(f"/alerts/{art}/reopen").status_code == 409


def test_someone_elses_drafted_artifact_is_not_found(
    client: TestClient, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.orchestrator.artifact_records import Viewer

    theirs = _insert(db, "their doc", source="artifact", topic_tags=["artifact"], owner_person_id=8)
    principals = _insert(db, "principal doc", source="artifact", topic_tags=["artifact"])
    # Not even the principal sees a teammate's document; nobody but the
    # principal sees one with no owner.
    for viewer, hidden in (
        (Viewer(person_id=1, is_principal=True), theirs),
        (Viewer(person_id=7), principals),
        (Viewer(person_id=None), theirs),
    ):
        monkeypatch.setattr(alerts_route, "_caller_viewer", lambda request, v=viewer: v)
        res = client.post(f"/alerts/{hidden}/ack", json={"status": "dismissed"})
        assert res.status_code == 404
    assert alert_store.get_alert(theirs, db_path=db).status == "unread"  # type: ignore[union-attr]
    monkeypatch.setattr(alerts_route, "_caller_viewer", lambda request: Viewer(person_id=8))
    res = client.post(f"/alerts/{theirs}/ack", json={"status": "dismissed"})
    assert res.status_code == 200 and res.json()["status"] == "dismissed"
