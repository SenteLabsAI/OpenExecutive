"""Decisions as standing facts (``memory.facts``, ``decision=1``): the
remember_fact tool keeps one, the older ``decisions`` table is copied in once,
and the decision readers (check-ins, weekly review, outcome follow-ups) read
them from there."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.delegation.settings import TurnDelegation
from openexecutive.memory import episodic, facts
from openexecutive.orchestrator import fact_tools
from openexecutive.orchestrator.schedule_tools import current_session

SAID = "We're going with Brex for expense cards, starting in January."


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from openexecutive.audit import AuditLogger, set_audit_logger

    path = tmp_path / "decisions.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    set_audit_logger(AuditLogger(db_path=path))
    yield path
    set_audit_logger(None)


@pytest.fixture
def principal(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setattr(
        "openexecutive.orchestrator.people_tools.is_principal_on_verified_surface",
        lambda session: True,
    )
    session = SimpleNamespace(
        session_id="s-1", caller_person_id=1, origin_channel="", from_web_chat=True,
        unattended=False, company_profile=None,
        turn_delegation=TurnDelegation(speaker_text=SAID, session_id="s-1"),
    )
    token = current_session.set(session)  # type: ignore[arg-type]
    yield session
    current_session.reset(token)


def _remember(**payload: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(fact_tools.handle_remember_fact(payload)))


def _old_decision(db: Path, summary: str, *, days_ago: int, department: str = "", outcome: str = "",
                  session_id: str = "", rationale: str = "") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, rationale, outcome, department, session_id) "
            "VALUES (?, 'finance', ?, ?, ?, ?, ?)",
            ((datetime.now(UTC) - timedelta(days=days_ago)).isoformat(), summary, rationale, outcome,
             department, session_id),
        )


def test_remember_fact_keeps_a_decision_with_its_department(
    principal: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.departments import registry

    monkeypatch.setattr(registry, "get_state", lambda slug: object() if slug == "finance" else None)
    mirrored: list[dict[str, Any]] = []
    monkeypatch.setattr(episodic, "_mirror_to_dept_honcho", lambda **kw: mirrored.append(kw))

    out = _remember(subject="Expense cards", statement="Going with Brex for expense cards from January.",
                    source_quote="We're going with Brex for expense cards", decision=True, department="Finance")
    assert out["status"] == "ok"
    [kept] = facts.decision_rows()
    assert kept.decision and kept.department == "finance"
    assert [m["department"] for m in mirrored] == ["finance"]
    assert "Going with Brex for expense cards from January. (decided)" in facts.render_facts_for_prompt()

    # An unknown department keeps the decision, untagged; a plain fact is no decision.
    out = _remember(subject="Card rollout", statement="Brex cards start in January.",
                    source_quote="starting in January", decision=True, department="nowhere")
    assert out["status"] == "ok" and facts.get_fact(out["fact_id"]).department == ""
    out = _remember(subject="Card vendor", statement="Brex issues the expense cards.", source_quote="Brex for expense cards")
    assert facts.get_fact(out["fact_id"]).decision is False


def test_an_edit_of_a_decision_stays_a_decision_with_its_outcome() -> None:
    first, _ = facts.record_fact(subject="Expense cards", statement="Going with Brex.", source_quote="",
                                 decision=True, department="finance")
    facts.set_decision_outcome(first.id, "Worked: rolled out in two weeks.")
    edited, replaced = facts.record_fact(subject="Expense cards", statement="Going with Ramp.", source_quote="",
                                         kind="correction", replaces_fact_id=first.id)
    assert [f.id for f in replaced] == [first.id]
    assert edited.decision and edited.department == "finance"
    assert edited.outcome == "Worked: rolled out in two weeks."
    assert "(decided; outcome: Worked: rolled out in two weeks.)" in facts.render_facts_for_prompt()


def test_old_decisions_are_copied_in_once(db: Path) -> None:
    _old_decision(db, "Slow hiring through Q3.", days_ago=45, department="people", outcome="Held to plan.",
                  rationale="Runway first.")
    _old_decision(db, "Slow hiring through Q3", days_ago=10, session_id="email:t9")

    facts.ensure_ready(db)
    facts.ensure_ready(db)
    # The one from mail waits for the principal's OK.
    [waiting] = [f for f in facts.list_facts(include_inactive=True) if f.status == "proposed"]
    assert [f.department for f in facts.decision_rows()] == ["people"]
    facts.approve_fact(waiting.id)
    rows = facts.decision_rows()
    assert len(rows) == 2, "copied once, and neither replaces the other"
    newest, oldest = rows
    assert oldest.department == "people" and oldest.outcome == "Held to plan."
    assert oldest.statement == "Slow hiring through Q3. Runway first.", "the reason is kept"
    assert oldest.created_at[:10] == (datetime.now(UTC) - timedelta(days=45)).date().isoformat()
    rendered = facts.render_facts_for_prompt()
    assert "(decided) (via email)" in rendered
    assert f"(decided; outcome: Held to plan.) — {oldest.created_at[:10]}" in rendered

    # Rows the table gets later are not copied again.
    _old_decision(db, "Late row", days_ago=1)
    facts.ensure_ready(db)
    assert len(facts.decision_rows()) == 2


def test_the_readers_see_decision_facts(db: Path) -> None:
    now = datetime.now(UTC)
    old, _ = facts.record_fact(subject="Pricing", statement="Raise prices 5%.", source_quote="",
                               decision=True, department="sales")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE facts SET created_at = ? WHERE id = ?", ((now - timedelta(days=40)).isoformat(), old.id))
    facts.record_fact(subject="Hiring", statement="Hire two engineers.", source_quote="", decision=True)
    facts.record_fact(subject="Office", statement="The office is on Pine St.", source_quote="")

    assert [d.summary for d in episodic.decisions_since(now - timedelta(days=7))] == ["Hire two engineers."]
    assert [d.id for d in episodic.decisions_awaiting_outcome(now - timedelta(days=30))] == [old.id]
    assert episodic.has_department_decision_since("sales", now - timedelta(days=50))
    assert not episodic.has_department_decision_since("sales", now - timedelta(days=7))
    assert episodic.update_decision(old.id, outcome="Margin held.")
    assert episodic.decisions_awaiting_outcome(now - timedelta(days=30)) == []
    assert len(episodic.list_decisions()) == 2, "a plain fact is no decision"


def test_a_reason_alone_is_kept_on_the_decision() -> None:
    kept, _ = facts.record_fact(subject="Pricing", statement="Raise prices 5%.", source_quote="", decision=True)
    assert episodic.update_decision(kept.id, rationale="Costs rose 8%.")
    [now] = episodic.list_decisions()
    assert now.summary == "Raise prices 5%. Costs rose 8%."
    assert episodic.update_decision(now.id, domain="legal", tags="x")
    assert [d.id for d in episodic.list_decisions()] == [now.id], "nothing else is kept, so nothing changes"
