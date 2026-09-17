"""Unit tests for the /review API surface: curation and bulk approve."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.api.routes import review as review_routes  # noqa: E402
from openexecutive.knowledge.review_store import (  # noqa: E402
    ContentType,
    ReviewStatus,
    ReviewStore,
)


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep audit writes out of the default ./episodic_memory.db.

    `audit.log_event` writes to the module default unless patched, and the
    leaked rows only break *other* modules' assertions in a full-suite run.
    """
    import openexecutive.audit as audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewStore:
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    monkeypatch.setattr(review_routes, "_store", lambda: ReviewStore(db_path=db))
    return ReviewStore(db_path=db)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _default(store: ReviewStore, filename: str, domain: str = "finance") -> str:
    """Register an item in its shipped state: approved, trusted, never reviewed."""
    import sqlite3

    item_id = f"builtin:{domain}:{filename}"
    store.register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )
    with sqlite3.connect(store._db_path) as conn:
        conn.execute(
            "UPDATE review_items SET status = 'approved', trusted_default = 1 "
            "WHERE item_id = ?",
            (item_id,),
        )
    return item_id


# ---------------------------------------------------------------------------
# POST /review/curate
# ---------------------------------------------------------------------------


def test_curate_start_queues_a_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    _default(store, "b.md")
    _default(store, "hr.md", domain="hr")

    res = client.post("/review/curate", json={"domain": "finance", "action": "start"})

    assert res.status_code == 200
    assert res.json() == {"domain": "finance", "action": "start", "affected_count": 2}
    # Queued items are withheld from retrieval; other domains are untouched.
    assert store.get_withheld_keys(ContentType.BUILTIN) == {("finance", "a.md"), ("finance", "b.md")}


def test_curate_stop_restores_a_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/curate", json={"domain": "finance", "action": "stop"})

    assert res.status_code == 200
    assert res.json()["affected_count"] == 1
    assert store.get_withheld_keys(ContentType.BUILTIN) == set()


def test_curate_requires_a_domain(client: TestClient, store: ReviewStore) -> None:
    """No 'curate everything' selector — one click must not dark the whole KB."""
    assert client.post("/review/curate", json={"action": "start"}).status_code == 422
    assert (
        client.post("/review/curate", json={"domain": "", "action": "start"}).status_code
        == 422
    )


def test_curate_rejects_an_unknown_action(client: TestClient, store: ReviewStore) -> None:
    res = client.post("/review/curate", json={"domain": "finance", "action": "nuke"})
    assert res.status_code == 422


def test_curate_unknown_domain_is_a_noop(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    res = client.post("/review/curate", json={"domain": "nope", "action": "start"})
    assert res.status_code == 200
    assert res.json()["affected_count"] == 0


# ---------------------------------------------------------------------------
# GET /review/trusted-defaults
# ---------------------------------------------------------------------------


def test_trusted_defaults_counts_by_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    _default(store, "b.md")
    _default(store, "h.md", domain="hr")

    res = client.get("/review/trusted-defaults")

    assert res.status_code == 200
    assert res.json() == {"finance": 2, "hr": 1}


def test_trusted_defaults_excludes_reviewed_items(
    client: TestClient, store: ReviewStore
) -> None:
    signed_off = _default(store, "a.md")
    store.set_status(signed_off, ReviewStatus.APPROVED, "our CFO checked this")

    assert client.get("/review/trusted-defaults").json() == {}


# ---------------------------------------------------------------------------
# POST /review/bulk-approve
# ---------------------------------------------------------------------------


def test_bulk_approve_requires_a_selector(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={})

    assert res.status_code == 400
    assert store.count_by_status()["pending"] == 1  # nothing was touched


def test_bulk_approve_all_pending_needs_the_explicit_flag(
    client: TestClient, store: ReviewStore
) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={"all_pending": True})

    assert res.status_code == 200
    assert res.json()["approved_count"] == 1
    assert store.count_by_status()["pending"] == 0


def test_bulk_approve_by_item_ids(client: TestClient, store: ReviewStore) -> None:
    a = _default(store, "a.md")
    _default(store, "b.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={"item_ids": [a]})

    assert res.status_code == 200
    assert res.json()["item_ids"] == [a]
    assert store.get_item("builtin:finance:b.md").status == ReviewStatus.PENDING  # type: ignore[union-attr]


def test_bulk_approve_caps_the_id_list(client: TestClient, store: ReviewStore) -> None:
    res = client.post(
        "/review/bulk-approve", json={"item_ids": [f"builtin:x:{i}.md" for i in range(501)]}
    )
    assert res.status_code == 422


def test_bulk_approve_rejects_an_empty_domain(client: TestClient, store: ReviewStore) -> None:
    """An empty domain must not masquerade as a scoped selector.

    The store treats a falsy domain as "no filter", so `{"domain": ""}` used to
    pass the selector guard and then widen to every pending item in every
    domain — while the audit trail recorded a scoped approve.
    """
    _default(store, "a.md", domain="finance")
    _default(store, "b.md", domain="hr")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})
    client.post("/review/curate", json={"domain": "hr", "action": "start"})
    assert store.count_by_status()["pending"] == 2

    for bad in ("", "   "):
        res = client.post("/review/bulk-approve", json={"domain": bad})
        assert res.status_code == 422, f"domain={bad!r} should be rejected"

    assert store.count_by_status()["pending"] == 2  # nothing was approved


def test_curate_rejects_an_absurdly_long_domain(client: TestClient, store: ReviewStore) -> None:
    res = client.post("/review/curate", json={"domain": "x" * 5000, "action": "start"})
    assert res.status_code == 422
