"""HTTP-level tests for /watchlist routes.

Mirrors the chat-tool tests in tests/unit/test_watchlist_tools.py: the
shared ``store.py`` + validators mean the two surfaces should behave
identically on the happy path and reject the same bad inputs.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts.models import AlertSeverity
from openexecutive.alerts.store import initialize_db as initialize_alerts_db
from openexecutive.api.routes import watchlist as watchlist_route
from openexecutive.memory.episodic import initialize_db as initialize_episodic_db
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import Signal


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "watchlist_routes.db"
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr("openexecutive.alerts.store.DB_PATH", db_path)
    initialize_episodic_db(db_path)
    initialize_alerts_db(db_path)
    ms.initialize_db(db_path)

    app = FastAPI()
    app.include_router(watchlist_route.router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_round_trips(client: TestClient) -> None:
    resp = client.post(
        "/watchlist",
        json={
            "slug": "stock-aapl",
            "signal_type": "stock",
            "target": "AAPL",
            "trigger": {"abs_change_pct_gte": 5},
            "cadence": "hourly",
            "severity_floor": "medium",
            "mode": "active",
            "display_label": "Apple",
            "notes": "Earnings season — watch closely",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "stock-aapl"
    assert body["signal_type"] == "stock"
    assert body["target"] == "AAPL"
    assert body["trigger_json"] == {"abs_change_pct_gte": 5}
    assert body["cadence"] == "hourly"
    assert body["severity_floor"] == "medium"
    assert body["mode"] == "active"
    # display_label maps into config_json under the signal-type-specific key.
    assert body["config_json"] == {"display_name": "Apple"}
    assert body["enabled"] is True


def test_a_watch_a_teammate_makes_tells_them(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 11-13: Alex's news watches told the owner instead of Alex.
    from openexecutive.people import store as people

    db_path = tmp_path / "watchlist_routes.db"
    monkeypatch.setattr(people, "DB_PATH", db_path)
    people.initialize_db(db_path)
    people.upsert_person(full_name="Jordan Avery", email="jordan@acme.example", is_principal=True, db_path=db_path)
    sam_id = people.upsert_person(full_name="Sam Okafor", email="sam@acme.example", db_path=db_path)

    as_sam = {"x-caller-email": "sam@acme.example"}
    made = client.post("/watchlist", json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"}, headers=as_sam)
    assert made.status_code == 201, made.text
    row = ms.get_watchlist_item_by_slug("stock-aapl", db_path=db_path)
    assert row is not None and row.route_to_person_id == sam_id

    as_owner = {"x-caller-email": "jordan@acme.example"}
    made = client.post("/watchlist", json={"slug": "stock-msft", "signal_type": "stock", "target": "MSFT"}, headers=as_owner)
    assert made.status_code == 201, made.text
    row = ms.get_watchlist_item_by_slug("stock-msft", db_path=db_path)
    assert row is not None and row.route_to_person_id is None


def test_create_rejects_uppercase_slug(client: TestClient) -> None:
    resp = client.post(
        "/watchlist",
        json={"slug": "Stock-AAPL", "signal_type": "stock", "target": "AAPL"},
    )
    assert resp.status_code == 400
    assert "kebab-case" in resp.json()["detail"]


def test_create_rejects_unknown_signal_type(client: TestClient) -> None:
    resp = client.post(
        "/watchlist",
        json={"slug": "x", "signal_type": "weather", "target": "anywhere"},
    )
    assert resp.status_code == 400
    assert "no registered adapter" in resp.json()["detail"]


def test_create_rejects_duplicate_slug(client: TestClient) -> None:
    payload = {"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"}
    assert client.post("/watchlist", json=payload).status_code == 201
    dup = client.post("/watchlist", json=payload)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_create_integrity_error_returns_409_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate the race where the pre-check passes but the UNIQUE
    # constraint fires (a concurrent POST landed between the SELECT
    # and the INSERT). The route must surface 409, not 500.
    import sqlite3 as _sqlite3

    from openexecutive.api.routes import watchlist as watchlist_route

    def boom(**kwargs: object) -> int:
        raise _sqlite3.IntegrityError("UNIQUE constraint failed: watchlist.slug")

    monkeypatch.setattr(watchlist_route.ms, "insert_watchlist_item", boom)
    resp = client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]
    # And the raw sqlite error text must NOT leak.
    assert "UNIQUE constraint" not in resp.json()["detail"]


def test_create_rejects_unknown_cadence(client: TestClient) -> None:
    resp = client.post(
        "/watchlist",
        json={
            "slug": "stock-aapl",
            "signal_type": "stock",
            "target": "AAPL",
            "cadence": "fortnightly",
        },
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def test_list_and_get(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    client.post(
        "/watchlist",
        json={
            "slug": "stock-msft",
            "signal_type": "stock",
            "target": "MSFT",
            "mode": "dry_run",
        },
    )

    lst = client.get("/watchlist").json()
    assert {it["slug"] for it in lst} == {"stock-aapl", "stock-msft"}

    one = client.get("/watchlist/stock-aapl").json()
    assert one["target"] == "AAPL"
    assert one["mode"] == "active"


def test_get_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/watchlist/does-not-exist").status_code == 404


def test_list_filters_to_enabled_only(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    client.post(
        "/watchlist",
        json={"slug": "stock-msft", "signal_type": "stock", "target": "MSFT"},
    )
    client.patch("/watchlist/stock-msft", json={"enabled": False})

    enabled = client.get("/watchlist?enabled_only=true").json()
    assert [it["slug"] for it in enabled] == ["stock-aapl"]


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #


def test_signals_returns_newest_first(client: TestClient, tmp_path: Path) -> None:
    created = client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    ).json()
    wl_id = created["id"]

    # Seed two signals directly through the store; the API just reads.
    now = datetime.now(UTC)
    for i in range(2):
        ms.insert_signal(
            Signal(
                watchlist_id=wl_id,
                source_kind="stock",
                source_external_id=f"AAPL-{i}",
                captured_at=(now.replace(microsecond=i)).isoformat(),
                normalized_summary=f"AAPL moved {i}%",
                raw_payload={},
                provenance_url="https://finance.yahoo.com/quote/AAPL",
                severity_hint=AlertSeverity.LOW,
                dedup_key=f"AAPL-{i}",
            ),
        )

    sigs = client.get("/watchlist/stock-aapl/signals").json()
    assert len(sigs) == 2
    # Newest first.
    assert sigs[0]["dedup_key"] == "AAPL-1"
    assert sigs[1]["dedup_key"] == "AAPL-0"
    # Audit-trail fields surface.
    assert "provenance_url" in sigs[0]
    assert "processed_outcome" in sigs[0]


def test_signals_404_for_unknown_slug(client: TestClient) -> None:
    assert client.get("/watchlist/missing/signals").status_code == 404


def test_signals_limit_clamped(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    # 9999 -> clamped to MAX (200); empty list, but still 200 OK.
    resp = client.get("/watchlist/stock-aapl/signals?limit=9999")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Patch
# --------------------------------------------------------------------------- #


def test_patch_toggles_enabled(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    resp = client.patch("/watchlist/stock-aapl", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp2 = client.patch("/watchlist/stock-aapl", json={"enabled": True})
    assert resp2.json()["enabled"] is True


def test_patch_replaces_trigger(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={
            "slug": "stock-aapl",
            "signal_type": "stock",
            "target": "AAPL",
            "trigger": {"abs_change_pct_gte": 5},
        },
    )
    resp = client.patch(
        "/watchlist/stock-aapl",
        json={"trigger": {"abs_change_pct_gte": 10}},
    )
    assert resp.status_code == 200
    assert resp.json()["trigger_json"] == {"abs_change_pct_gte": 10}


def test_patch_rejects_empty_body(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    resp = client.patch("/watchlist/stock-aapl", json={})
    assert resp.status_code == 400
    assert "at least one" in resp.json()["detail"]


def test_patch_rejects_bad_mode(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    resp = client.patch("/watchlist/stock-aapl", json={"mode": "panic"})
    assert resp.status_code == 400


def test_patch_404_for_unknown(client: TestClient) -> None:
    resp = client.patch("/watchlist/nothing", json={"enabled": False})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_delete_removes_row(client: TestClient) -> None:
    client.post(
        "/watchlist",
        json={"slug": "stock-aapl", "signal_type": "stock", "target": "AAPL"},
    )
    assert client.delete("/watchlist/stock-aapl").status_code == 204
    assert client.get("/watchlist/stock-aapl").status_code == 404


def test_delete_404_for_unknown(client: TestClient) -> None:
    assert client.delete("/watchlist/nothing").status_code == 404
