"""The monthly AI limit: storing it (memory.workspace_settings), this month's
spend against it and the budget pause (audit.spending), and the routes that
show and change it (PUT /workspace, /executive/*, GET /audit/spending)."""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import audit as audit_route
from openexecutive.api.routes import executive as executive_route
from openexecutive.api.routes import workspace as workspace_route
from openexecutive.audit import logger as audit_logger_module
from openexecutive.audit import spending
from openexecutive.audit.logger import AuditLogger, set_audit_logger
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import store as people_store
from openexecutive.scheduler import pause as pause_store

# Ten days into September, in any zone.
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
EARLY_SEPTEMBER = "2026-09-05T12:00:00.000000Z"
OWNER = {"x-caller-email": "ceo@example.com"}
TEAMMATE = {"x-caller-email": "tia@example.com"}


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the routes and the watch take as now — never the real clock, so
    no test lands across a month boundary."""
    monkeypatch.setattr(spending, "_now", lambda: NOW)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ws.ZoneInfo("UTC"))
    monkeypatch.setattr(pause_store, "_read_failing", False)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    return path


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """The pause/resume and settings audit rows (usage rows go to ``audit``)."""
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: captured.append((event_type, kw)),
    )
    return captured


@pytest.fixture()
def audit(tmp_path: Path, db: Path, events: list[Any]) -> Iterator[AuditLogger]:
    logger = AuditLogger(tmp_path / "audit.db")
    set_audit_logger(logger)
    yield logger
    set_audit_logger(None)


def _record(audit: AuditLogger, model: str, at: str = EARLY_SEPTEMBER, **counts: int) -> None:
    """One model call's usage row, stamped ``at``."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(audit_logger_module, "_now", lambda: at)
        audit.log("cache_event", "call", actor="executive", details={"model": model, **counts})


def _spend(audit: AuditLogger, usd: float, at: str = EARLY_SEPTEMBER) -> None:
    """A call costing ``usd`` at list price (claude-sonnet-5 input: $2 per
    million tokens)."""
    _record(audit, "claude-sonnet-5", at, input_tokens=round(usd * 500_000))


# ---------------------------------------------------------------------------
# Storing the limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stored"),
    [(None, None), ("", None), ("  ", None), (1, 1.0), ("40.5", 40.5), (19.999, 20.0),
     (1_000_000, 1_000_000.0)],
)
def test_a_limit_is_whole_cents_or_none(raw: object, stored: float | None) -> None:
    assert ws.validate_monthly_budget(raw) == stored


@pytest.mark.parametrize(
    "raw", [0, 0.99, -5, 1_000_000.01, float("inf"), float("nan"), 10**400, True, "ten", [10]]
)
def test_a_limit_outside_one_dollar_to_a_million_is_refused(raw: object) -> None:
    with pytest.raises(ValueError, match="monthly_budget_usd"):
        ws.validate_monthly_budget(raw)


def test_the_limit_is_stored_and_removed_without_touching_the_rest(db: Path) -> None:
    ws.restore_workspace_settings(
        ws.WorkspaceSettings(mode="solo", timezone="Europe/Madrid", role_title="COO")
    )
    assert ws.get_workspace().monthly_budget_usd is None
    assert ws.set_monthly_budget(250).monthly_budget_usd == 250.0
    stored = ws.get_workspace()
    assert (stored.mode, stored.timezone, stored.role_title) == ("solo", "Europe/Madrid", "COO")
    # A value that does not validate writes nothing.
    with pytest.raises(ValueError):
        ws.set_monthly_budget(-1)
    assert ws.get_workspace().monthly_budget_usd == 250.0
    assert ws.set_monthly_budget(None).monthly_budget_usd is None
    assert ws.get_workspace().role_title == "COO"


def test_a_table_from_before_the_limit_gains_the_column_on_write(db: Path) -> None:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE workspace_settings (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "mode TEXT NOT NULL DEFAULT 'team', timezone TEXT, updated_at TEXT)"
        )
        conn.execute("INSERT INTO workspace_settings (id, mode, timezone) VALUES (1, 'solo', 'Asia/Tokyo')")
        conn.commit()
    assert ws.get_workspace().monthly_budget_usd is None  # a read never migrates
    ws.set_monthly_budget(75)
    stored = ws.get_workspace()
    assert (stored.mode, stored.timezone, stored.monthly_budget_usd) == ("solo", "Asia/Tokyo", 75.0)
    # A hand-edited value that no longer validates reads as no limit.
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE workspace_settings SET monthly_budget_usd = -3")
        conn.commit()
    assert ws.get_workspace().monthly_budget_usd is None


# ---------------------------------------------------------------------------
# This month's spend
# ---------------------------------------------------------------------------


def test_the_month_starts_at_midnight_in_the_users_zone(audit: AuditLogger) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(timezone="America/Denver"))
    # Midnight on 1 September in Denver is 06:00 UTC.
    _spend(audit, 3.0, at="2026-09-01T05:59:59.999999Z")  # still August there
    _spend(audit, 5.0, at="2026-09-01T06:00:00.000000Z")
    late_on_the_30th = spending.current_spending(datetime(2026, 10, 1, 5, 0, tzinfo=UTC))
    assert (late_on_the_30th.month, late_on_the_30th.zone) == ("2026-09", "America/Denver")
    assert late_on_the_30th.spent_usd == 5.0
    october = spending.current_spending(datetime(2026, 10, 1, 6, 0, tzinfo=UTC))
    assert (october.month, october.spent_usd) == ("2026-10", 0.0)


def test_a_month_whose_first_midnight_repeats_starts_at_the_first_one() -> None:
    # Havana sets its clocks back at 1am on 1 November 2026, so midnight
    # happens at 04:00 UTC and again at 05:00 UTC.
    havana = ws.ZoneInfo("America/Havana")
    during_the_repeat = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    start = spending.month_start(during_the_repeat, havana)
    assert start.astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


def test_the_forecast_waits_three_days_then_follows_the_pace(audit: AuditLogger) -> None:
    _spend(audit, 10.0, at="2026-09-01T12:00:00.000000Z")
    assert spending.current_spending(datetime(2026, 9, 3, 23, 0, tzinfo=UTC)).forecast_usd is None
    # Ten days of September's thirty.
    assert spending.current_spending(datetime(2026, 9, 11, tzinfo=UTC)).forecast_usd == 30.0


@pytest.mark.parametrize(
    ("spent", "limit", "state"),
    [(5.0, None, "no_limit"), (7.99, 10.0, "ok"), (8.0, 10.0, "near"),
     (10.0, 10.0, "reached"), (12.0, 10.0, "reached")],
)
def test_the_state_reads_the_spend_against_the_limit(
    audit: AuditLogger, spent: float, limit: float | None, state: str
) -> None:
    ws.set_monthly_budget(limit)
    _spend(audit, spent)
    got = spending.current_spending(NOW)
    assert (got.spent_usd, got.limit_usd, got.state) == (spent, limit, state)


def test_calls_without_a_list_price_are_counted_not_treated_as_free(audit: AuditLogger) -> None:
    _record(audit, "llama3.3", input_tokens=5_000)
    _spend(audit, 2.0)
    got = spending.current_spending(NOW)
    assert (got.spent_usd, got.unpriced_calls) == (2.0, 1)


# ---------------------------------------------------------------------------
# The budget pause
# ---------------------------------------------------------------------------


def test_reaching_the_limit_pauses_background_work_once(
    audit: AuditLogger, events: list[tuple[str, dict[str, Any]]]
) -> None:
    ws.set_monthly_budget(10)
    _spend(audit, 9.0)
    assert spending.enforce_budget(NOW) is None
    assert pause_store.get_pause_state().paused is False

    _spend(audit, 1.0)
    assert spending.enforce_budget(NOW) == "paused"
    state = pause_store.get_pause_state()
    assert (state.paused, state.paused_by) == (True, spending.BUDGET_PAUSED_BY)
    assert "$10.00 monthly limit" in (state.reason or "")
    assert spending.enforce_budget(NOW) is None  # already paused
    assert [e[0] for e in events] == ["executive_paused"]
    assert events[0][1]["actor"] == spending.BUDGET_PAUSED_BY


def test_no_limit_never_pauses(audit: AuditLogger) -> None:
    _spend(audit, 5_000.0)
    assert spending.enforce_budget(NOW) is None
    assert pause_store.get_pause_state().paused is False


@pytest.mark.parametrize("change", ["raised", "removed", "new month"])
def test_the_budget_pause_lifts_once_spend_is_under_the_limit(
    audit: AuditLogger, events: list[tuple[str, dict[str, Any]]], change: str
) -> None:
    ws.set_monthly_budget(10)
    _spend(audit, 12.0)
    assert spending.enforce_budget(NOW) == "paused"
    now = NOW
    if change == "raised":
        ws.set_monthly_budget(20)
    elif change == "removed":
        ws.set_monthly_budget(None)
    else:
        now = datetime(2026, 10, 1, 0, 1, tzinfo=UTC)
    assert spending.enforce_budget(now) == "lifted"
    assert pause_store.get_pause_state().paused is False
    assert [e[0] for e in events] == ["executive_paused", "executive_resumed"]


def test_a_pause_someone_started_is_never_lifted_or_relabelled(audit: AuditLogger) -> None:
    ws.set_monthly_budget(10)
    _spend(audit, 12.0)
    pause_store.pause("ceo@example.com", "offsite")
    assert spending.enforce_budget(NOW) is None
    ws.set_monthly_budget(50)
    assert spending.enforce_budget(NOW) is None
    ws.set_monthly_budget(None)
    assert spending.enforce_budget(NOW) is None
    state = pause_store.get_pause_state()
    assert (state.paused, state.paused_by, state.reason) == (True, "ceo@example.com", "offsite")
    # The lift is compare-and-set: it only ever releases its own pause.
    assert pause_store.resume_if_paused_by(spending.BUDGET_PAUSED_BY) is False
    assert pause_store.get_pause_state().paused is True


def test_the_limit_never_claims_a_pause_someone_started_in_between(audit: AuditLogger) -> None:
    # enforce_budget read "running"; before it paused, a person paused.
    pause_store.pause("ceo@example.com", "offsite")
    assert pause_store.pause_if_running(spending.BUDGET_PAUSED_BY, "limit") is False
    assert pause_store.get_pause_state().paused_by == "ceo@example.com"
    pause_store.resume("ceo@example.com")
    assert pause_store.pause_if_running(spending.BUDGET_PAUSED_BY, "limit") is True
    assert pause_store.pause_if_running(spending.BUDGET_PAUSED_BY, "again") is False
    assert pause_store.get_pause_state().reason == "limit"


def test_resuming_a_persons_pause_while_over_the_limit_pauses_again(audit: AuditLogger) -> None:
    ws.set_monthly_budget(10)
    _spend(audit, 12.0)
    pause_store.pause("ceo@example.com")
    assert spending.enforce_budget(NOW) is None
    pause_store.resume("ceo@example.com")
    assert spending.enforce_budget(NOW) == "paused"
    assert pause_store.get_pause_state().paused_by == spending.BUDGET_PAUSED_BY


def test_resume_is_blocked_only_while_the_limit_is_still_reached(
    audit: AuditLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert spending.spending_blocks_resume() is False  # running
    ws.set_monthly_budget(10)
    _spend(audit, 12.0)
    pause_store.pause("ceo@example.com")
    assert spending.spending_blocks_resume() is False  # a person's pause
    pause_store.resume("ceo@example.com")
    assert spending.enforce_budget() == "paused"
    assert spending.spending_blocks_resume() is True
    # Raised, before the next check lifts it: a resume would hold.
    ws.set_monthly_budget(20)
    assert spending.spending_blocks_resume() is False
    # A spend it cannot read keeps it blocked.
    ws.set_monthly_budget(10)

    def unreadable(*_a: object, **_k: object) -> spending.Spending:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(spending, "current_spending", unreadable)
    assert spending.spending_blocks_resume() is True
    # So does a pause state it cannot read.
    monkeypatch.setattr(pause_store, "get_pause_state", unreadable)
    assert spending.spending_blocks_resume() is True


async def test_the_watch_keeps_checking_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def enforce() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(spending, "enforce_budget", enforce)
    task = asyncio.create_task(spending.run_budget_watch(0))
    for _ in range(500):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(audit: AuditLogger) -> TestClient:
    app = FastAPI()
    for route in (workspace_route, executive_route, audit_route):
        app.include_router(route.router)
    return TestClient(app)


def _roster_owner_and_teammate() -> None:
    people_store.upsert_person(full_name="Pat Principal", is_principal=True, email="ceo@example.com")
    people_store.upsert_person(full_name="Tia Teammate", email="tia@example.com")


def test_only_the_owner_sets_the_limit(
    client: TestClient, events: list[tuple[str, dict[str, Any]]]
) -> None:
    _roster_owner_and_teammate()
    denied = client.put("/workspace", json={"monthly_budget_usd": 50}, headers=TEAMMATE)
    assert denied.status_code == 403
    resp = client.put("/workspace", json={"monthly_budget_usd": 50}, headers=OWNER)
    assert resp.status_code == 200
    assert resp.json()["monthly_budget_usd"] == 50.0
    changed = [kw for e, kw in events if e == "workspace_settings_changed"]
    assert changed[-1]["details"]["monthly_budget_usd"] == {"from": None, "to": 50.0}
    # Sending the same limit again changes nothing and records nothing.
    client.put("/workspace", json={"monthly_budget_usd": 50}, headers=OWNER)
    assert len([e for e, _ in events if e == "workspace_settings_changed"]) == 1
    cleared = client.put("/workspace", json={"monthly_budget_usd": None}, headers=OWNER)
    assert cleared.json()["monthly_budget_usd"] is None


@pytest.mark.parametrize("amount", [0, -10, 0.5, 2_000_000, 10**400, "lots", True])
def test_a_limit_out_of_range_is_refused(client: TestClient, amount: object) -> None:
    resp = client.put("/workspace", json={"monthly_budget_usd": amount})
    assert resp.status_code == 422
    assert client.get("/workspace").json()["monthly_budget_usd"] is None


def test_a_new_limit_applies_at_once(client: TestClient, audit: AuditLogger) -> None:
    _roster_owner_and_teammate()
    _spend(audit, 30.0)
    client.put("/workspace", json={"monthly_budget_usd": 25}, headers=OWNER)
    status = client.get("/executive/status", headers=OWNER).json()
    assert (status["paused"], status["paused_for_budget"], status["can_resume"]) == (True, True, False)
    # Resuming would only be undone by the next check.
    resp = client.post("/executive/resume", headers=OWNER)
    assert resp.status_code == 409
    assert "monthly limit" in resp.json()["detail"]
    assert client.get("/executive/status").json()["paused"] is True
    # Raising the limit over this month's spend lifts the pause straight away.
    client.put("/workspace", json={"monthly_budget_usd": 40}, headers=OWNER)
    status = client.get("/executive/status", headers=OWNER).json()
    assert (status["paused"], status["paused_for_budget"]) == (False, False)


def test_resuming_a_persons_pause_over_the_limit_hands_over_at_once(
    client: TestClient, audit: AuditLogger, events: list[tuple[str, dict[str, Any]]]
) -> None:
    _roster_owner_and_teammate()
    client.post("/executive/pause", json={"reason": "offsite"}, headers=OWNER)
    client.put("/workspace", json={"monthly_budget_usd": 10}, headers=OWNER)
    _spend(audit, 12.0)
    resp = client.post("/executive/resume", headers=OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["paused"], body["paused_for_budget"], body["can_resume"]) == (True, True, False)
    flips = [(e, kw["actor"]) for e, kw in events if e.startswith("executive_")]
    assert flips == [
        ("executive_paused", "ceo@example.com"),
        ("executive_resumed", "ceo@example.com"),
        ("executive_paused", spending.BUDGET_PAUSED_BY),
    ]


def test_pausing_during_the_limits_pause_takes_it_over(
    client: TestClient, audit: AuditLogger, events: list[tuple[str, dict[str, Any]]]
) -> None:
    """Otherwise the limit, lifting its own pause on the 1st, would release
    work the person asked to hold."""
    _roster_owner_and_teammate()
    _spend(audit, 12.0)
    client.put("/workspace", json={"monthly_budget_usd": 10}, headers=OWNER)
    resp = client.post("/executive/pause", json={"reason": "offsite"}, headers=TEAMMATE)
    body = resp.json()
    assert (body["paused_by"], body["reason"], body["paused_for_budget"]) == (
        "tia@example.com", "offsite", False,
    )
    taken = [kw for e, kw in events if e == "executive_paused" and kw["actor"] == "tia@example.com"]
    assert taken[0]["details"]["took_over_from"] == spending.BUDGET_PAUSED_BY
    # A new month leaves it alone.
    assert spending.enforce_budget(datetime(2026, 10, 1, 0, 1, tzinfo=UTC)) is None
    assert pause_store.get_pause_state().paused_by == "tia@example.com"


def test_spending_shows_this_month_and_who_may_change_the_limit(
    client: TestClient, audit: AuditLogger
) -> None:
    _roster_owner_and_teammate()
    client.put("/workspace", json={"monthly_budget_usd": 100}, headers=OWNER)
    _spend(audit, 85.0)
    body = client.get("/audit/spending", headers=OWNER).json()
    assert set(body) == {
        "month", "zone", "spent_usd", "forecast_usd", "limit_usd", "unpriced_calls",
        "state", "paused_for_budget", "can_change_limit",
    }
    assert (body["spent_usd"], body["limit_usd"], body["state"]) == (85.0, 100.0, "near")
    assert (body["zone"], body["paused_for_budget"], body["can_change_limit"]) == ("UTC", False, True)
    assert client.get("/audit/spending", headers=TEAMMATE).json()["can_change_limit"] is False
