"""GET /memories/facts and POST /memories/facts/{id}/retire — the Pulse
page's Corrections tab. Anyone signed in sees the standing facts (every
conversation already does); only the principal sees the quote of their own
message, and only the principal may retire one."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import episodic as episodic_route
from openexecutive.memory import episodic, facts


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "facts.db")
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, **kw}),
    )
    yield rows


def _client(monkeypatch: pytest.MonkeyPatch, *, principal: bool) -> TestClient:
    monkeypatch.setattr(episodic_route, "_caller_is_principal", lambda request: principal)
    app = FastAPI()
    app.include_router(episodic_route.router)
    return TestClient(app)


def _seed() -> tuple[int, int, int]:
    old, _ = facts.record_fact(subject="Units", statement="Maple House has 52 units.", source_quote="52")
    new, _ = facts.record_fact(subject="Units", statement="Maple House has 48 units.",
                               source_quote="Maple House is 48 units, not 52", source_channel="web",
                               session_id="s-principal", turn_id="t-1", recorded_by_person_id=1)
    prof, _ = facts.record_fact(kind="profile", subject="Company profile — Headcount",
                                statement="Headcount set to 42", source_quote="we're 42 now")
    return old.id, new.id, prof.id


def test_the_principal_sees_everything_with_history(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new, prof = _seed()
    body = _client(monkeypatch, principal=True).get("/memories/facts").json()
    assert body["can_retire"] is True
    by_id = {f["id"]: f for f in body["facts"]}
    assert set(by_id) == {old, new, prof}
    assert by_id[new]["kind"] == "correction" and by_id[new]["status"] == "active"
    assert by_id[new]["source_quote"] == "Maple House is 48 units, not 52"
    assert by_id[new]["session_id"] == "s-principal" and by_id[new]["recorded_by_person_id"] == 1
    assert by_id[old]["status"] == "superseded" and by_id[old]["superseded_by"] == new
    active = _client(monkeypatch, principal=True).get("/memories/facts?include_inactive=false").json()
    assert {f["id"] for f in active["facts"]} == {new, prof}


def test_a_teammate_sees_the_facts_but_not_the_principals_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    facts.retire_fact(new, reason="annex sold, keep quiet")
    body = _client(monkeypatch, principal=False).get("/memories/facts").json()
    assert body["can_retire"] is False
    assert body["facts"] and all(f["source_quote"] == "" for f in body["facts"])
    assert all(
        f["session_id"] is None and f["turn_id"] is None and f["recorded_by_person_id"] is None
        for f in body["facts"]
    )
    # Only what is in force: no retired or replaced text, whatever is asked.
    assert all(f["status"] == "active" for f in body["facts"])
    assert "52 units" not in str(body) and "48 units" not in str(body)
    mine = _client(monkeypatch, principal=True).get("/memories/facts").json()
    assert any(f["retired_reason"] == "annex sold, keep quiet" for f in mine["facts"])


def test_only_the_principal_retires(monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]]) -> None:
    _, new, prof = _seed()
    assert _client(monkeypatch, principal=False).post(f"/memories/facts/{new}/retire").status_code == 403
    assert facts.render_facts_for_prompt() != ""

    client = _client(monkeypatch, principal=True)
    res = client.post(f"/memories/facts/{new}/retire", json={"reason": "annex sold"})
    assert res.status_code == 200 and res.json()["status"] == "retired"
    assert res.json()["retired_reason"] == "annex sold"
    assert facts.render_facts_for_prompt() == ""
    assert [r["event_type"] for r in db] == ["fact_retired"]
    assert "annex sold" not in str(db)
    # It names what was retired: the principal's alone, never on /audit for
    # a teammate who can no longer read the fact itself.
    assert db[0]["private"] is True
    assert client.post(f"/memories/facts/{new}/retire").status_code == 409
    # Profile rows are the audit trail of a profile edit, not something to retire.
    assert client.post(f"/memories/facts/{prof}/retire").status_code == 404
    assert client.post("/memories/facts/999/retire").status_code == 404


def test_a_teammate_never_gets_the_principals_session_or_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    body = _client(monkeypatch, principal=False).get("/memories/facts").json()
    row = next(f for f in body["facts"] if f["id"] == new)
    assert row["statement"] == "Maple House has 48 units." and row["status"] == "active"
    assert row["session_id"] is None and row["turn_id"] is None
    assert row["recorded_by_person_id"] is None and row["source_quote"] == ""


def test_a_failed_audit_row_is_logged_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    warned: list[tuple[Any, ...]] = []

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr("openexecutive.audit.log_event", broken)
    # The logger itself, not caplog: another test's logging setup can stop
    # propagation to caplog's handler in a full run.
    monkeypatch.setattr(episodic_route.logger, "warning", lambda *a, **k: warned.append(a))
    res = _client(monkeypatch, principal=True).post(f"/memories/facts/{new}/retire")
    assert res.status_code == 200 and res.json()["status"] == "retired"
    assert warned and "fact_retired audit row failed" in warned[0][0]
    # The fact's text never reaches the log line.
    assert "Maple House" not in str(warned)
