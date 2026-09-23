"""HTTP-level tests for /departments and /departments/{slug}/goals.

Includes coverage for the legacy /okrs aliases kept for one release.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import departments as departments_route
from openexecutive.departments import registry, store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    path = tmp_path / "departments.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    # Drop any stale registry cache from a previous test process.
    registry.invalidate()
    store.initialize_db()
    store.seed_default_departments()

    app = FastAPI()
    app.include_router(departments_route.router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------------- #

def test_list_returns_eight(client: TestClient) -> None:
    resp = client.get("/departments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 8
    slugs = {item["config"]["slug"] for item in data}
    assert "finance" in slugs
    assert "board_comms" in slugs


def test_get_known_department(client: TestClient) -> None:
    resp = client.get("/departments/finance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["slug"] == "finance"
    assert data["config"]["specialist_key"] == "cfo"
    assert data["config"]["authority_level"] == "propose_only"
    assert data["config"]["charter"]["mission"]
    assert data["goals"] == []


def test_get_unknown_department(client: TestClient) -> None:
    resp = client.get("/departments/does-not-exist")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Patch
# --------------------------------------------------------------------------- #

def test_patch_authority_and_headcount(client: TestClient) -> None:
    resp = client.patch(
        "/departments/finance",
        json={"authority_level": "auto_execute", "headcount": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["authority_level"] == "auto_execute"
    assert data["headcount"] == 3


def test_patch_rejects_invalid_authority(client: TestClient) -> None:
    resp = client.patch(
        "/departments/finance",
        json={"authority_level": "do_whatever"},
    )
    assert resp.status_code == 422


def test_patch_watched_entities_cleans_and_validates(client: TestClient) -> None:
    resp = client.patch(
        "/departments/finance",
        json={"watched_entities": ["  Brex ", "brex", "", "Stripe   Inc"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["watched_entities"] == ["Brex", "Stripe Inc"]
    # Omitting the field leaves it alone; sending [] clears it.
    assert client.patch("/departments/finance", json={"headcount": 2}).json()["config"]["watched_entities"] == ["Brex", "Stripe Inc"]
    assert client.patch("/departments/finance", json={"watched_entities": []}).json()["config"]["watched_entities"] == []
    assert client.patch("/departments/finance", json={"watched_entities": ["x" * 129]}).status_code == 422
    assert client.patch("/departments/finance", json={"watched_entities": [f"e{i}" for i in range(51)]}).status_code == 422


@pytest.mark.parametrize("spec", ["mondays@09:00", "daily@25:00", "weekly@xyz@09:00", "quarterly@00-09:00", " "])
def test_patch_rejects_invalid_cadence(client: TestClient, spec: str) -> None:
    before = client.get("/departments/finance").json()["config"]["cadences"]
    resp = client.patch("/departments/finance", json={"cadences": {"check_in": spec}})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert spec in detail
    assert "weekly@mon@09:00" in detail
    # Nothing was stored.
    assert client.get("/departments/finance").json()["config"]["cadences"] == before


@pytest.mark.parametrize("spec", ["daily@09:00", "weekly@mon@09:00", "weekly@Mon-09:00", "quarterly@01-09:00", ""])
def test_patch_accepts_valid_or_empty_cadence(client: TestClient, spec: str) -> None:
    resp = client.patch("/departments/finance", json={"cadences": {"check_in": spec}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["cadences"]["check_in"] == spec


def test_patch_unknown_department(client: TestClient) -> None:
    resp = client.patch("/departments/nope", json={"headcount": 1})
    assert resp.status_code == 404


def test_patch_empty_body_is_noop(client: TestClient) -> None:
    resp = client.patch("/departments/finance", json={})
    assert resp.status_code == 200
    assert resp.json()["config"]["slug"] == "finance"


# --------------------------------------------------------------------------- #
# Goal CRUD (primary endpoints)
# --------------------------------------------------------------------------- #

def test_create_goal(client: TestClient) -> None:
    resp = client.post(
        "/departments/finance/goals",
        json={
            "period_type": "quarter",
            "period_value": "Q2 2026",
            "key_result": "Close Series A by Jun 30",
            "target": "termsheet signed",
            "current": "in negotiation",
            "status": "on_track",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["department_slug"] == "finance"
    assert body["status"] == "on_track"
    assert body["period_type"] == "quarter"
    assert body["period_value"] == "Q2 2026"


def test_create_goal_period_type_defaults_to_quarter(client: TestClient) -> None:
    resp = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    assert resp.status_code == 201
    assert resp.json()["period_type"] == "quarter"


def test_create_goal_with_each_period_type(client: TestClient) -> None:
    for period_type, period_value in (
        ("week", "Week of May 18"),
        ("month", "May 2026"),
        ("year", "2026"),
        ("ongoing", "Ongoing"),
    ):
        resp = client.post(
            "/departments/finance/goals",
            json={
                "period_type": period_type,
                "period_value": period_value,
                "key_result": f"KR for {period_type}",
                "target": "x",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["period_type"] == period_type
        assert body["period_value"] == period_value


def test_create_goal_for_unknown_department(client: TestClient) -> None:
    resp = client.post(
        "/departments/nope/goals",
        json={"period_value": "Q2", "key_result": "x", "target": "y"},
    )
    assert resp.status_code == 404


def test_patch_goal(client: TestClient) -> None:
    create = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = create.json()["id"]

    resp = client.patch(
        f"/departments/finance/goals/{goal_id}",
        json={"current": "halfway", "status": "at_risk"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "halfway"
    assert body["status"] == "at_risk"


def test_patch_goal_wrong_department_404s(client: TestClient) -> None:
    """A Goal id that exists but under a different department must not be
    mutable via the wrong slug — otherwise the URL-shape contract is a lie
    and authority-gated edits in Phase 4 will route to the wrong department."""
    create = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = create.json()["id"]

    resp = client.patch(
        f"/departments/product/goals/{goal_id}",
        json={"current": "x"},
    )
    assert resp.status_code == 404


def test_delete_goal(client: TestClient) -> None:
    create = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = create.json()["id"]

    resp = client.delete(f"/departments/finance/goals/{goal_id}")
    assert resp.status_code == 204

    after = client.get("/departments/finance")
    assert after.json()["goals"] == []


def test_delete_goal_wrong_department_404s(client: TestClient) -> None:
    create = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = create.json()["id"]
    resp = client.delete(f"/departments/product/goals/{goal_id}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Legacy /okrs aliases (deprecated, removed at Sunset)
# --------------------------------------------------------------------------- #

def test_okrs_alias_create_returns_goal(client: TestClient) -> None:
    resp = client.post(
        "/departments/finance/okrs",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    assert resp.status_code == 201
    assert resp.json()["key_result"] == "K"
    assert resp.headers.get("deprecation") == "true"
    assert resp.headers.get("sunset", "").endswith("GMT")
    link = resp.headers.get("link", "")
    assert "/goals" in link
    assert 'rel="successor-version"' in link


def test_okrs_alias_accepts_legacy_quarter_key(client: TestClient) -> None:
    """A client still on the old shape (`{"quarter": "Q2 2026", ...}`) must
    keep working through the /okrs alias until the Sunset date."""
    resp = client.post(
        "/departments/finance/okrs",
        json={"quarter": "Q2 2026", "key_result": "K", "target": "T"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["period_type"] == "quarter"
    assert body["period_value"] == "Q2 2026"


def test_okrs_alias_patch_accepts_legacy_quarter_key(client: TestClient) -> None:
    created = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = created.json()["id"]
    resp = client.patch(
        f"/departments/finance/okrs/{goal_id}",
        json={"quarter": "Q3 2026"},
    )
    assert resp.status_code == 200
    assert resp.json()["period_value"] == "Q3 2026"


def test_okrs_alias_patch_returns_goal(client: TestClient) -> None:
    created = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = created.json()["id"]
    resp = client.patch(
        f"/departments/finance/okrs/{goal_id}",
        json={"status": "at_risk"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "at_risk"
    assert resp.headers.get("deprecation") == "true"


def test_okrs_alias_delete_succeeds(client: TestClient) -> None:
    created = client.post(
        "/departments/finance/goals",
        json={"period_value": "Q2 2026", "key_result": "K", "target": "T"},
    )
    goal_id = created.json()["id"]
    resp = client.delete(f"/departments/finance/okrs/{goal_id}")
    assert resp.status_code == 204


# --------------------------------------------------------------------------- #
# Registry cache invalidation
# --------------------------------------------------------------------------- #

def test_patch_invalidates_registry_cache(client: TestClient) -> None:
    # Warm the cache, then assert the warm state actually populated module
    # state — otherwise this test would pass even if the cache were broken.
    before = registry.list_states()
    assert all(s.config.authority_level.value == "propose_only" for s in before)
    assert registry._cache is not None
    assert registry._cache_expires_at > 0.0

    resp = client.patch(
        "/departments/finance",
        json={"authority_level": "auto_execute"},
    )
    assert resp.status_code == 200
    # Mutation must have cleared the cache, not just overwritten its value.
    assert registry._cache is None

    after = {s.config.slug: s.config.authority_level.value for s in registry.list_states()}
    assert after["finance"] == "auto_execute"


# --------------------------------------------------------------------------- #
# POST /departments
# --------------------------------------------------------------------------- #

def test_create_department(client: TestClient) -> None:
    resp = client.post(
        "/departments",
        json={"title": "Customer Success", "mission": "Own post-sale relationships."},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["config"]["slug"] == "customer-success"
    assert body["config"]["title"] == "Customer Success"
    assert body["config"]["specialist_key"] is None
    assert body["config"]["authority_level"] == "propose_only"
    assert body["goals"] == []


def test_create_department_minimal(client: TestClient) -> None:
    resp = client.post("/departments", json={"title": "Engineering"})
    assert resp.status_code == 201
    assert resp.json()["config"]["slug"] == "engineering"


def test_create_department_rejects_blank_title(client: TestClient) -> None:
    resp = client.post("/departments", json={"title": ""})
    assert resp.status_code == 422


def test_create_department_appears_in_list(client: TestClient) -> None:
    client.post("/departments", json={"title": "Legal Ops"})
    resp = client.get("/departments")
    slugs = {item["config"]["slug"] for item in resp.json()}
    assert "legal-ops" in slugs


def test_create_department_invalidates_registry(client: TestClient) -> None:
    registry.list_states()  # warm the cache
    assert registry._cache is not None
    client.post("/departments", json={"title": "New Dept"})
    assert registry._cache is None


# --------------------------------------------------------------------------- #
# DELETE /departments/{slug}
# --------------------------------------------------------------------------- #

def test_delete_department(client: TestClient) -> None:
    resp = client.delete("/departments/finance")
    assert resp.status_code == 204


def test_delete_department_removes_from_list(client: TestClient) -> None:
    client.delete("/departments/finance")
    resp = client.get("/departments")
    slugs = {item["config"]["slug"] for item in resp.json()}
    assert "finance" not in slugs


def test_delete_department_unknown_404s(client: TestClient) -> None:
    resp = client.delete("/departments/does-not-exist")
    assert resp.status_code == 404


def test_delete_department_then_get_404s(client: TestClient) -> None:
    client.delete("/departments/finance")
    assert client.get("/departments/finance").status_code == 404


def test_delete_department_cascades_goals(client: TestClient) -> None:
    client.post("/departments/finance/goals", json={
        "period_value": "Q2 2026", "key_result": "K", "target": "T",
    })
    client.delete("/departments/finance")
    assert store.list_goals("finance") == []


def test_delete_department_invalidates_registry(client: TestClient) -> None:
    registry.list_states()
    assert registry._cache is not None
    client.delete("/departments/finance")
    assert registry._cache is None
