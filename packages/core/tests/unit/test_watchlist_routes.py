"""Watchlist HTTP routes: research suggestions (approve / decline), decline
memory on delete, and the origin field on responses."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import watchlist as watchlist_route
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.memory import episodic
from openexecutive.monitoring import store as ms


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "watchlist.db"
    monkeypatch.setattr(alert_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    alert_store.initialize_db(db_path)
    episodic.initialize_db(db_path)
    ms.initialize_db(db_path)
    audit = AuditLogger(db_path=db_path)
    audit.initialize_db()
    set_audit_logger(audit)
    return db_path


@pytest.fixture()
def client(db: Path) -> TestClient:
    app = FastAPI()
    app.include_router(watchlist_route.router)
    return TestClient(app)


def _suggest(db: Path, slug: str = "rss-initech", target: str = "https://initech.com/feed") -> int:
    return ms.insert_watchlist_item(
        slug=slug, signal_type="rss", target=target, mode="dry_run",
        origin="research_proposed", notes="Adjacent competitor blog",
        config={"_policy": {"entity": "Initech", "grounding_kind": "", "specialist": "cso"}},
        db_path=db,
    )


def test_list_carries_origin(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    _suggest(db)
    rows = {r["slug"]: r for r in client.get("/watchlist").json()}
    assert rows["stock-aapl"]["origin"] == "manual"
    assert rows["rss-initech"]["origin"] == "research_proposed" and rows["rss-initech"]["mode"] == "dry_run"


def test_approve_turns_suggestion_live_and_records_outcome(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/approve")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["origin"] == "research" and body["mode"] == "active" and body["enabled"] is True
    assert ms.list_pending_suggestions(db_path=db) == []
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"approved": 1}}
    assert ms.specialist_outcome_counts(db_path=db) == {"cso": {"approved": 1}}
    # Not a pending suggestion any more → 409; unknown → 404.
    assert client.post("/watchlist/rss-initech/approve").status_code == 409
    assert client.post("/watchlist/nope/approve").status_code == 404


def test_approve_rejects_manual_rows(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    assert client.post("/watchlist/stock-aapl/approve").status_code == 409


def test_decline_not_relevant_removes_and_remembers(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline", json={"reason": "not_relevant"})
    assert res.status_code == 200, res.text
    assert res.json() == {"slug": "rss-initech", "reason": "not_relevant", "result": "removed"}
    assert ms.get_watchlist_item_by_slug("rss-initech", db_path=db) is None
    declines = ms.list_declines(db_path=db)
    assert declines[0].normalized_target == "https://initech.com/feed"
    assert declines[0].kind == "declined_explicit" and declines[0].entity == "Initech"
    assert ms.is_declined("https://initech.com/feed", db_path=db)
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"declined": 1}}


def test_decline_defaults_to_not_relevant_without_a_body(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline")
    assert res.status_code == 200 and res.json()["reason"] == "not_relevant"


def test_decline_too_noisy_keeps_it_live_for_big_changes_only(client: TestClient, db: Path) -> None:
    _suggest(db)
    res = client.post("/watchlist/rss-initech/decline", json={"reason": "too_noisy"})
    assert res.status_code == 200 and res.json()["result"] == "kept_high_floor"
    row = ms.get_watchlist_item_by_slug("rss-initech", db_path=db)
    assert row is not None
    assert row.mode == "active" and row.origin == "research"
    assert row.trigger_json.get("only_big") is True and row.severity_floor.value == "low"
    assert ms.list_declines(db_path=db) == []
    # For calibration it is a decline: the principal objected to the guess.
    assert ms.policy_outcome_counts(db_path=db) == {("rss", ""): {"declined": 1}}
    # A second decline / approve now 409s: no longer pending.
    assert client.post("/watchlist/rss-initech/decline", json={"reason": "too_noisy"}).status_code == 409


def test_decline_validates_reason_and_state(client: TestClient, db: Path) -> None:
    _suggest(db)
    assert client.post("/watchlist/rss-initech/decline", json={"reason": "meh"}).status_code == 400
    ms.insert_watchlist_item(slug="stock-aapl", signal_type="stock", target="AAPL", db_path=db)
    assert client.post("/watchlist/stock-aapl/decline", json={"reason": "not_relevant"}).status_code == 409
    assert client.post("/watchlist/nope/decline").status_code == 404


def test_delete_research_row_records_decline_with_reason(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(
        slug="stock-acme", signal_type="stock", target="acme", origin="research", db_path=db,
    )
    ms.insert_watchlist_item(slug="stock-mine", signal_type="stock", target="MINE", db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=wrong_source").status_code == 204
    declines = ms.list_declines(db_path=db)
    assert [(d.normalized_target, d.reason) for d in declines] == [("ACME", "wrong_source")]
    assert client.delete("/watchlist/stock-mine").status_code == 204
    assert len(ms.list_declines(db_path=db)) == 1  # manual rows are not declines
    assert client.delete("/watchlist/stock-mine?reason=bogus").status_code == 404


def test_delete_rejects_unknown_reason(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="stock-acme", signal_type="stock", target="ACME", origin="research", db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=bogus").status_code == 400
    assert ms.get_watchlist_item_by_slug("stock-acme", db_path=db) is not None


def test_delete_and_decline_work_after_the_watch_recorded_signals(client: TestClient, db: Path) -> None:
    from openexecutive.monitoring.models import Signal

    wid = ms.insert_watchlist_item(
        slug="stock-acme", signal_type="stock", target="ACME", origin="research", db_path=db,
    )
    sid = _suggest(db)
    for w, key in ((wid, "k1"), (sid, "k2")):
        ms.insert_signal(Signal(
            watchlist_id=w, source_kind="stock", source_external_id=key,
            captured_at="2026-09-12T00:00:00+00:00", normalized_summary="s",
            provenance_url="https://x/1", dedup_key=key,
        ), db_path=db)
    assert client.delete("/watchlist/stock-acme?reason=not_relevant").status_code == 204
    assert client.post("/watchlist/rss-initech/decline", json={"reason": "wrong_source"}).status_code == 200
    assert ms.list_watchlist(db_path=db) == []
    assert ms.list_signals_for_watchlist(wid, db_path=db) == []


# --------------------------------------------------------------------- #
# What each watch says about itself, and who it is for
# --------------------------------------------------------------------- #


def _told(db: Path, watch_id: int, key: str, summary: str, outcome: str = "alerted") -> None:
    from datetime import UTC, datetime

    from openexecutive.monitoring.models import Signal

    signal_id = ms.insert_signal(Signal(
        watchlist_id=watch_id, source_kind="stock", source_external_id=key,
        captured_at=datetime.now(UTC).isoformat(), normalized_summary=summary,
        provenance_url="https://example.com/q", dedup_key=f"stock:{key}",
    ), db_path=db)
    assert signal_id is not None
    ms.mark_signal_processed(signal_id, outcome, db_path=db)


def test_a_watch_says_what_it_is_and_what_it_last_said(client: TestClient, db: Path) -> None:
    watch_id = ms.insert_watchlist_item(
        slug="tesla-stock", signal_type="stock", target="TSLA",
        config={"display_name": "Tesla"}, cadence="15min", db_path=db,
    )
    ms.set_last_check_note(watch_id, "Down 1.1% today. Under 5%, so it stayed quiet.", db_path=db)
    row = client.get("/watchlist/tesla-stock").json()
    assert row["title"] == "Tesla (TSLA) stock"
    assert row["tells_you_when"] == "It moves 5% or more in a day, up or down"
    assert row["checks"] == "It checks every 15 minutes."
    assert row["last_told"] is None
    assert row["last_check_note"] == "Down 1.1% today. Under 5%, so it stayed quiet."

    _told(db, watch_id, "held", "[Tesla] down 2% today", outcome="suppressed_dry_run")
    _told(db, watch_id, "told", "[Tesla] down 6.2% today")
    (listed,) = client.get("/watchlist").json()
    assert listed["last_told"]["summary"] == "Down 6.2% today"
    assert listed["last_told"]["url"] == "https://example.com/q"

    # "What it told you" lists what reached the person, not what was held.
    told = client.get("/watchlist/tesla-stock/signals?told=true").json()
    assert [s["normalized_summary"] for s in told] == ["[Tesla] down 6.2% today"]
    assert len(client.get("/watchlist/tesla-stock/signals").json()) == 2


def test_a_watch_can_be_for_a_goal_and_a_person(
    client: TestClient, db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.departments import store as departments
    from openexecutive.people import store as people

    monkeypatch.setattr(departments, "DB_PATH", db)
    monkeypatch.setattr(people, "DB_PATH", db)
    people.initialize_db(db)
    departments.initialize_db(db)
    goal_id = departments.insert_goal(
        None, period_value="2031-Q1", key_result="Open the second roastery", target="by March", db_path=db,
    )
    sam_id = people.upsert_person(full_name="Sam Okafor", role="Operations", email="sam@acme.example", db_path=db)
    ms.insert_watchlist_item(slug="rss-acme", signal_type="rss", target="https://acme.example/feed", db_path=db)

    res = client.patch("/watchlist/rss-acme", json={"goal_id": goal_id, "person_id": sam_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["goal_title"] == "Open the second roastery"
    assert (body["person_name"], body["person_role"]) == ("Sam Okafor", "Operations")
    row = ms.get_watchlist_item_by_slug("rss-acme", db_path=db)
    assert row is not None and row.goal_id == goal_id and row.route_to_person_id == sam_id
    assert [w.slug for w in ms.watches_for_goals(db_path=db)[goal_id]] == ["rss-acme"]

    # Unknown goal or person: refused, nothing saved.
    assert client.patch("/watchlist/rss-acme", json={"goal_id": 9999}).status_code == 422
    assert client.patch("/watchlist/rss-acme", json={"person_id": 9999}).status_code == 422

    cleared = client.patch("/watchlist/rss-acme", json={"goal_id": None, "person_id": None}).json()
    assert cleared["goal_title"] == "" and cleared["person_name"] == ""


def test_big_changes_only_is_part_of_tells_you_when(client: TestClient, db: Path) -> None:
    ms.insert_watchlist_item(slug="rss-acme", signal_type="rss", target="https://acme.example/feed", db_path=db)
    res = client.patch("/watchlist/rss-acme", json={"trigger": {"keywords": ["pricing"], "only_big": True}})
    assert res.status_code == 200, res.text
    assert res.json()["tells_you_when"] == (
        "There's a new post that mentions “pricing”, and only when it is a big change"
    )


def test_only_the_owner_chooses_who_a_watch_tells(
    client: TestClient, db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.departments import store as departments
    from openexecutive.people import store as people

    monkeypatch.setattr(departments, "DB_PATH", db)
    monkeypatch.setattr(people, "DB_PATH", db)
    people.initialize_db(db)
    departments.initialize_db(db)
    people.upsert_person(full_name="Jordan Avery", email="jordan@acme.example", is_principal=True, db_path=db)
    sam_id = people.upsert_person(full_name="Sam Okafor", email="sam@acme.example", db_path=db)
    ms.insert_watchlist_item(slug="rss-acme", signal_type="rss", target="https://acme.example/feed", db_path=db)

    as_sam = {"x-caller-email": "sam@acme.example"}
    res = client.patch("/watchlist/rss-acme", json={"person_id": sam_id}, headers=as_sam)
    assert res.status_code == 403
    row = ms.get_watchlist_item_by_slug("rss-acme", db_path=db)
    assert row is not None and row.route_to_person_id is None
    # A teammate can still change the rest.
    assert client.patch("/watchlist/rss-acme", json={"notes": "ours"}, headers=as_sam).status_code == 200

    as_owner = {"x-caller-email": "jordan@acme.example"}
    assert client.patch("/watchlist/rss-acme", json={"person_id": sam_id}, headers=as_owner).status_code == 200


def test_a_link_that_is_not_a_web_address_is_not_passed_on(client: TestClient, db: Path) -> None:
    from datetime import UTC, datetime

    from openexecutive.monitoring.models import Signal

    watch_id = ms.insert_watchlist_item(slug="rss-acme", signal_type="rss", target="https://acme.example/feed", db_path=db)
    signal_id = ms.insert_signal(Signal(
        watchlist_id=watch_id, source_kind="rss", source_external_id="1",
        captured_at=datetime.now(UTC).isoformat(), normalized_summary="[Acme] new post",
        provenance_url="javascript:alert(1)", dedup_key="rss:1",
    ), db_path=db)
    assert signal_id is not None
    ms.mark_signal_processed(signal_id, "alerted", db_path=db)
    assert client.get("/watchlist/rss-acme").json()["last_told"]["url"] == ""
    assert client.get("/watchlist/rss-acme/signals").json()[0]["provenance_url"] == ""
