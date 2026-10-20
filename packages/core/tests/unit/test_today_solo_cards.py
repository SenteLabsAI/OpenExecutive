"""The solo Briefing's own cards (api/routes/today.py): GET /today/top-three
(today's top three, as the morning brief picks them) and GET
/today/weekly-review (the latest completed weekly review). Both answer the
principal alone, in solo mode only; everyone else, and team mode, get null."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import today as today_route
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.briefing import top_three
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic
from openexecutive.memory import workspace_settings as ws
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import persistence

PRINCIPAL_EMAIL = "maya@example.com"
TEAMMATE_EMAIL = "sam@example.com"
STRANGER_EMAIL = "someone@example.com"
# A Friday, before working hours (09:00–18:00; the user's zone is UTC here).
NOW = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)

_CALENDAR = (
    "Successfully retrieved 1 events from calendar 'maya@example.com':\n"
    '- "Client call" (Starts: 2026-09-25T09:00:00+00:00, Ends: 2026-09-25T10:00:00+00:00) '
    "ID: a1 | Link: https://x"
)
_OVERDUE = {
    "loop_id": 4, "description": "Maya Lindqvist committed to: send the revised invoice",
    "due_at": "2026-09-23T15:00:00+00:00", "due_date": "2026-09-23", "state": "overdue",
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "cards.db"
    for mod in (episodic, dept_store, people_store, persistence):
        monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(ws, "_configured_timezone", lambda: ZoneInfo("UTC"))
    # Audit rows go to the temp DB, never ./episodic_memory.db.
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    set_audit_logger(AuditLogger(db_path=db))
    episodic.initialize_db(db)
    dept_store.initialize_db(db)
    people_store.initialize_db(db)
    persistence.initialize_runs_db(db)
    dept_registry.invalidate()
    people_registry.invalidate()
    yield db
    set_audit_logger(None)
    dept_registry.invalidate()
    people_registry.invalidate()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(today_route.router)
    return TestClient(app)


def _mode(mode: str) -> None:
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode=mode))  # type: ignore[arg-type]


def _roster() -> dict[str, int]:
    ids = {
        "principal": people_store.upsert_person(
            full_name="Maya Lindqvist", is_principal=True, email=PRINCIPAL_EMAIL
        ),
        "teammate": people_store.upsert_person(full_name="Sam Ortiz", email=TEAMMATE_EMAIL),
    }
    people_registry.invalidate()
    return ids


def _get(client: TestClient, path: str, email: str | None) -> Any:
    r = client.get(path, headers={"x-caller-email": email} if email else {})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# GET /today/top-three
# --------------------------------------------------------------------------- #


class _Gateway:
    def __init__(self, reply: str | Exception, delay: float = 0.0) -> None:
        self.reply, self.delay = reply, delay
        self.calls = 0

    async def call_tool(self, _tool_input: dict[str, Any]) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _connect(monkeypatch: pytest.MonkeyPatch, gateway: _Gateway | None) -> None:
    monkeypatch.setattr(
        "openexecutive.scheduler.runner.google_workspace_ready", lambda: gateway is not None
    )
    monkeypatch.setattr(
        "openexecutive.orchestrator.mcp_gateway.get_active_gateway", lambda: gateway
    )


@pytest.fixture
def picked(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A solo workspace with something to pick: an overdue commitment (what
    ``principal_due_soon`` returns) and a goal at risk. The route's
    ``build_top_three`` runs for real, pinned to NOW; ``handed`` records each
    ``due_soon`` it was given and ``real`` is the unpinned function."""
    _mode("solo")
    dept_store.create_department("Finance", specialist_key="cfo")
    dept_store.insert_goal("finance", period_value="Q4 2026", key_result="Build a cash buffer",
                           target="3 months", current="2.1 months", status="at_risk")
    dept_registry.invalidate()
    monkeypatch.setattr(
        "openexecutive.attunement.open_loops.principal_due_soon", lambda **_kw: [dict(_OVERDUE)]
    )
    real = top_three.build_top_three
    handed: list[list[dict[str, Any]]] = []

    async def _pinned(due_soon: list[dict[str, Any]], **_kw: Any) -> Any:
        handed.append(due_soon)
        return await real(due_soon, now=NOW)

    monkeypatch.setattr(top_three, "build_top_three", _pinned)
    return SimpleNamespace(handed=handed, real=real)


def test_the_principal_gets_the_briefs_top_three_without_a_calendar(
    client: TestClient, picked: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster()
    _connect(monkeypatch, None)
    for email in (PRINCIPAL_EMAIL, None):  # signed in, and no caller header
        body = _get(client, "/today/top-three", email)
        assert [(i["kind"], i["text"], i["why"], i["slot"]) for i in body["items"]] == [
            ("commitment", "Maya Lindqvist committed to: send the revised invoice",
             "overdue (was due 2026-09-23)", None),
            ("goal", "Finance: Build a cash buffer",
             "at risk; target 3 months, now 2.1 months", None),
        ]
    # The morning brief's input, handed on untouched.
    assert picked.handed == [[_OVERDUE], [_OVERDUE]]


def test_the_items_match_the_morning_brief_with_a_calendar(
    client: TestClient, picked: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster()
    gw = _Gateway(_CALENDAR)
    _connect(monkeypatch, gw)
    body = _get(client, "/today/top-three", PRINCIPAL_EMAIL)
    assert gw.calls == 1
    assert [i["slot"] for i in body["items"]] == ["10:00–11:00", "11:00–12:00"]
    # Same items, order and slots as the brief builds from the same input.
    items, _calendar = asyncio.run(picked.real([dict(_OVERDUE)], now=NOW))
    assert body["items"] == items and gw.calls == 2


@pytest.mark.parametrize("failure", ["raises", "slow", "error_reply"])
def test_a_calendar_failure_gives_items_without_slots(
    client: TestClient, picked: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _roster()
    monkeypatch.setattr(top_three, "CALENDAR_TIMEOUT_SECONDS", 0.05)
    gw = {
        "raises": _Gateway(RuntimeError("mcp down")),
        "slow": _Gateway(_CALENDAR, delay=1.0),
        "error_reply": _Gateway("Error calling tool 'get_events': 404 Not Found"),
    }[failure]
    _connect(monkeypatch, gw)
    body = _get(client, "/today/top-three", PRINCIPAL_EMAIL)
    assert gw.calls == 1
    assert len(body["items"]) == 2
    assert all(i["slot"] is None for i in body["items"])


def test_nothing_to_pick_is_an_empty_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode("solo")
    _roster()
    _connect(monkeypatch, None)
    monkeypatch.setattr(
        "openexecutive.attunement.open_loops.principal_due_soon", lambda **_kw: []
    )
    assert _get(client, "/today/top-three", PRINCIPAL_EMAIL) == {"items": []}


def test_anyone_but_the_principal_gets_null(
    client: TestClient, picked: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _roster()
    gw = _Gateway(_CALENDAR)
    _connect(monkeypatch, gw)
    # A teammate, and a signed-in email that is on nobody's entry.
    for email in (TEAMMATE_EMAIL, STRANGER_EMAIL):
        assert _get(client, "/today/top-three", email) is None
    # No principal on the roster lets no signed-in caller in.
    people_store.archive_person(ids["principal"])
    people_registry.invalidate()
    for email in (TEAMMATE_EMAIL, PRINCIPAL_EMAIL):
        assert _get(client, "/today/top-three", email) is None
    assert picked.handed == [] and gw.calls == 0


def test_team_mode_gets_null(
    client: TestClient, picked: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster()
    _mode("team")
    gw = _Gateway(_CALENDAR)
    _connect(monkeypatch, gw)
    for email in (PRINCIPAL_EMAIL, None):
        assert _get(client, "/today/top-three", email) is None
    assert picked.handed == [] and gw.calls == 0


# --------------------------------------------------------------------------- #
# GET /today/weekly-review
# --------------------------------------------------------------------------- #

_REVIEW = (
    "# Weekly review — Week of Sep 21\n\n"
    "## Goals by area\n\n### Finance\n"
    "- **Build a cash buffer** — at risk; target 3 months, now 2.1 months.\n\n"
    "## Commitments due\n\n- OVERDUE (was due 2026-09-23): Send the revised invoice\n\n"
    "## Next week's top 3\n\n"
    "1. **Send the revised invoice** — it is two days overdue\n"
    "2. Close the Northwind proposal — the `Q4` pipeline needs it\n"
    "3. Rebuild the cash buffer — see [the plan](https://example.com/plan)\n"
)


def _run(db: Path, run_id: str, *, status: str, updated_at: str,
         artifact: str | None = None, workflow: str = "weekly_review") -> None:
    persistence.create_run(run_id, workflow, "Weekly Review", {}, db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE workflow_runs SET status = ?, artifact = ?, updated_at = ? WHERE run_id = ?",
            (status, artifact, updated_at, run_id),
        )


def test_the_latest_completed_review_wins(client: TestClient, _isolated: Path) -> None:
    _mode("solo")
    _roster()
    _run(_isolated, "older", status="done", updated_at="2026-09-11T16:02:00+00:00",
         artifact="# Weekly review — Week of Sep 7\n\n## Next week's top 3\n\n1. old")
    _run(_isolated, "latest", status="done", updated_at="2026-09-18T16:02:00+00:00",
         artifact=_REVIEW)
    # Newer, but failed, still running, or another workflow.
    _run(_isolated, "failed", status="error", updated_at="2026-09-25T16:02:00+00:00")
    _run(_isolated, "running", status="running", updated_at="2026-09-25T16:03:00+00:00")
    _run(_isolated, "brief", status="done", updated_at="2026-09-25T08:00:00+00:00",
         artifact="# Morning brief", workflow="morning_brief")
    for email in (PRINCIPAL_EMAIL, None):
        assert _get(client, "/today/weekly-review", email) == {
            "run_id": "latest",
            "completed_at": "2026-09-18T16:02:00+00:00",
            "period": "Week of Sep 21",
            "top_three": [
                "Send the revised invoice — it is two days overdue",
                "Close the Northwind proposal — the Q4 pipeline needs it",
                "Rebuild the cash buffer — see the plan",
            ],
            "excerpt": "",
        }


def test_a_review_with_nothing_to_pick_shows_its_note(
    client: TestClient, _isolated: Path
) -> None:
    _mode("solo")
    _roster()
    _run(_isolated, "quiet", status="done", updated_at="2026-09-25T16:02:00+00:00",
         artifact="# Weekly review — Week of Sep 21\n\n## Goals by area\n\n"
                  "_No goals tracked yet._\n\n## Next week's top 3\n\n"
                  "_Nothing stands out — a good week to get ahead._\n")
    body = _get(client, "/today/weekly-review", PRINCIPAL_EMAIL)
    assert (body["top_three"], body["excerpt"]) == (
        [], "Nothing stands out — a good week to get ahead."
    )


def test_no_completed_review_is_null(client: TestClient, _isolated: Path) -> None:
    _mode("solo")
    _roster()
    assert _get(client, "/today/weekly-review", PRINCIPAL_EMAIL) is None
    _run(_isolated, "failed", status="error", updated_at="2026-09-25T16:02:00+00:00")
    _run(_isolated, "running", status="running", updated_at="2026-09-25T16:03:00+00:00")
    assert _get(client, "/today/weekly-review", PRINCIPAL_EMAIL) is None


def test_the_review_is_the_principals_alone(client: TestClient, _isolated: Path) -> None:
    _mode("solo")
    ids = _roster()
    _run(_isolated, "latest", status="done", updated_at="2026-09-18T16:02:00+00:00",
         artifact=_REVIEW)
    for email in (TEAMMATE_EMAIL, STRANGER_EMAIL):
        assert _get(client, "/today/weekly-review", email) is None
    people_store.archive_person(ids["principal"])
    people_registry.invalidate()
    assert _get(client, "/today/weekly-review", TEAMMATE_EMAIL) is None
    # Team mode: null even for the principal.
    people_store.upsert_person(full_name="Maya Lindqvist", is_principal=True,
                               email="maya.new@example.com")
    people_registry.invalidate()
    assert _get(client, "/today/weekly-review", "maya.new@example.com")["run_id"] == "latest"
    _mode("team")
    for email in ("maya.new@example.com", None):
        assert _get(client, "/today/weekly-review", email) is None


def test_an_unreadable_run_store_is_null(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode("solo")
    _roster()

    def _boom(**_kw: Any) -> list[dict[str, Any]]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(persistence, "list_runs", _boom)
    assert _get(client, "/today/weekly-review", PRINCIPAL_EMAIL) is None
