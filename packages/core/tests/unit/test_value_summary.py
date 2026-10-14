"""The owner's monthly tally (briefing/value_summary.py): what counts, what
doesn't, the period bounds, the route's gate and the monthly message."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.briefing import narrative_cache, value_summary
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence

UTC_ZONE = ZoneInfo("UTC")
# Mid-October 2026; "this month" is October, the one before September.
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
OCT = "2026-10-05T10:00:00.000000Z"
SEP = "2026-09-20T10:00:00.000000Z"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(narrative_cache, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    persistence.initialize_runs_db(path)
    people_registry.invalidate()
    set_audit_logger(AuditLogger(db_path=path))
    monkeypatch.setattr(value_summary, "_last_check_at", None)
    monkeypatch.setattr(value_summary, "_task", None)
    yield path
    set_audit_logger(None)
    people_registry.invalidate()


@pytest.fixture
def team() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Pat Principal", is_principal=True, email="pat@northwind.test",
        slack_user_id="UPAT",
    )
    sara = people_store.upsert_person(full_name="Sara Kim", email="sara@northwind.test")
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, sara=sara)


def _audit(db: Path, ts: str, event_type: str, details: dict[str, Any], *,
           private_to_person: int | None = None, private: bool = False) -> None:
    from openexecutive.audit import get_audit_logger

    row_id = get_audit_logger().log(event_type, "row", details=details, private=private,
                                    private_to_person=private_to_person)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit_log SET ts = ? WHERE id = ?", (ts, row_id))


def _run(db: Path, run_id: str, name: str, at: str, status: str = "done") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO workflow_runs (run_id, workflow_name, title, status, inputs, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?)",
            (run_id, name, f"{name} report", status, at, at),
        )


def _summary(**kw: Any) -> value_summary.ValueSummary:
    return value_summary.summarize("month", 0, now=NOW, zone=UTC_ZONE, **kw)


def _count(summary: value_summary.ValueSummary, key: str) -> tuple[int, int]:
    line = summary.line(key)
    assert line is not None
    return line.count, line.previous


def test_period_windows() -> None:
    zone = ZoneInfo("America/New_York")
    month = value_summary.period_window("month", 0, now=NOW, zone=zone)
    assert month.label == "October 2026"
    assert month.bounds == ("2026-10-01T04:00:00+00:00", "2026-11-01T04:00:00+00:00")
    jan = value_summary.period_window("month", 10, now=NOW, zone=UTC_ZONE)
    assert jan.label == "December 2025"
    week = value_summary.period_window("week", 0, now=NOW, zone=UTC_ZONE)
    assert week.label == "Week of 12 October"
    assert week.bounds == ("2026-10-12T00:00:00+00:00", "2026-10-19T00:00:00+00:00")


def test_messages_count_sends_to_others_not_to_the_owner(db: Path, team: SimpleNamespace) -> None:
    sent = {"tool": "send_slack_dm", "kind": "outbound", "ok": True}
    _audit(db, OCT, "tool_invocation", {**sent, "user_id": "USARA"})
    _audit(db, OCT, "tool_invocation", {**sent, "user_id": "USARA"})
    _audit(db, OCT, "tool_invocation", {**sent, "user_id": "UPAT"})  # a brief to the owner
    _audit(db, OCT, "tool_invocation", {**sent, "user_id": "USARA", "held": True})
    _audit(db, OCT, "tool_invocation", {"tool": "send_slack_dm", "kind": "outbound", "ok": False})
    # The executive loop's own row for the same call is not a second send.
    _audit(db, OCT, "tool_invocation", {"tool": "send_slack_dm", "kind": "skill"})
    _audit(db, OCT, "tool_invocation", {"tool": "send_telegram_message", "kind": "outbound",
                                        "ok": True, "chat_id": 42})
    # Mail sent from a chat turn, and one the gateway refused.
    _audit(db, OCT, "tool_invocation", {"tool": "google_workspace__send_gmail_message",
                                        "kind": "mcp", "result_preview": "Message sent"})
    _audit(db, OCT, "tool_invocation", {"tool": "microsoft_365__send-mail", "kind": "mcp",
                                        "result_preview": "{}"})
    _audit(db, OCT, "tool_invocation", {"tool": "google_workspace__send_gmail_message",
                                        "kind": "mcp", "result_preview": '{"error": "blocked"}'})
    _audit(db, SEP, "tool_invocation", {**sent, "user_id": "USARA"})
    assert _count(_summary(), "messages_sent") == (5, 1)


def test_other_lines(db: Path, team: SimpleNamespace) -> None:
    _audit(db, OCT, "delegation_reply_sent", {"person_id": team.principal})
    _audit(db, OCT, "delegation_reply_drafted", {"person_id": team.principal})
    # Sara's own inbox rows are hers, not the owner's.
    _audit(db, OCT, "delegation_reply_sent", {"person_id": team.sara},
           private_to_person=team.sara)
    _audit(db, OCT, "tool_invocation", {"tool": "create_calendar_event", "kind": "skill"})
    _audit(db, OCT, "tool_invocation", {"tool": "create_calendar_event", "kind": "skill",
                                        "ok": False})
    _audit(db, OCT, "scheduled_action", {"phase": "delivered", "kind": "principal_brief_morning"})
    _audit(db, OCT, "scheduled_action", {"phase": "delivered", "action_id": 4})
    _audit(db, OCT, "attunement", {"op": "loop_closed", "reason": "done"})
    _audit(db, OCT, "attunement", {"op": "loop_closed", "reason": "expired"})
    _audit(db, OCT, "chat_turn", {"direction": "out"})
    _run(db, "r1", "investor_update", "2026-10-03T09:00:00+00:00")
    _run(db, "r2", "morning_brief", "2026-10-03T09:00:00+00:00")
    _run(db, "r3", "board_prep", "2026-10-04T09:00:00+00:00", status="failed")
    _run(db, "r4", "board_prep", "2026-09-04T09:00:00+00:00")
    summary = _summary()
    assert _count(summary, "inbox_sent") == (1, 0)
    assert _count(summary, "inbox_drafted") == (1, 0)
    assert _count(summary, "meetings_booked") == (1, 0)
    assert _count(summary, "briefs_delivered") == (1, 0)
    assert _count(summary, "follow_ups") == (1, 0)
    assert _count(summary, "tasks_closed") == (1, 0)
    assert _count(summary, "answered") == (1, 0)
    assert _count(summary, "reports") == (1, 1)
    assert [d.run_id for d in summary.deliverables] == ["r1"]
    assert not summary.is_empty()


def test_an_empty_install_reads_as_zero(tmp_path: Path) -> None:
    summary = _summary(db_path=tmp_path / "missing.db", audit_db_path=tmp_path / "none.db")
    assert summary.is_empty() and summary.deliverables == []
    assert "Nothing to report" in value_summary.render_message(summary)


def test_message_lists_non_zero_lines_and_the_link(db: Path, team: SimpleNamespace) -> None:
    _audit(db, OCT, "chat_turn", {"direction": "out"})
    text = value_summary.render_message(_summary(), link="https://oe.test/memories?tab=value")
    assert text.startswith("**What your Executive did in October 2026**")
    assert "- Requests answered: 1" in text and "Meetings booked" not in text
    assert text.endswith("The full breakdown: https://oe.test/memories?tab=value")
    assert "$" not in text


async def test_monthly_message_goes_once_on_the_first_weekday(
    db: Path, team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.scheduler import runner

    sent: list[str] = []

    async def deliver(text: str, *, label: str = "Update") -> runner.PrincipalDelivery:
        sent.append(text)
        return runner.PrincipalDelivery(True, "fake", "delivered", "slack_dm")

    monkeypatch.setattr(runner, "_deliver_to_principal", deliver)
    monkeypatch.setattr("openexecutive.memory.workspace_settings.get_user_timezone",
                        lambda *a, **k: UTC_ZONE)
    _audit(db, OCT, "chat_turn", {"direction": "out"})
    # 2 November 2026 is a Monday, the month's first weekday (the 1st is a Sunday).
    sunday = datetime(2026, 11, 1, 10, tzinfo=UTC)
    monday = datetime(2026, 11, 2, 10, tzinfo=UTC)
    assert not await value_summary.send_monthly(sunday)
    assert not await value_summary.send_monthly(monday.replace(hour=6))
    assert await value_summary.send_monthly(monday)
    assert not await value_summary.send_monthly(monday.replace(hour=11))
    assert len(sent) == 1 and "October 2026" in sent[0]
    monkeypatch.setenv("VALUE_SUMMARY_MONTHLY_ENABLED", "false")
    assert not await value_summary.send_monthly(datetime(2026, 12, 1, 10, tzinfo=UTC))


def test_route_is_the_owners_alone(team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes.today import router

    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    owner = client.get("/today/value?period=week")
    assert owner.status_code == 200, owner.text
    assert owner.json()["period"] == "week" and "lines" in owner.json()
    sara = client.get("/today/value", headers={"x-caller-email": "sara@northwind.test"})
    assert sara.status_code == 403
    assert client.get("/today/value?period=year").status_code == 422
