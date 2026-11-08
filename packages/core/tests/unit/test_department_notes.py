"""A department's own notes (memory/department_notes.py)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.memory import department_notes as dn
from openexecutive.memory import episodic

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    return path


def test_notes_are_kept_per_department_and_matched_to_the_question() -> None:
    dn.add("growth", "decision", "Hold the spring launch until the waitlist passes 300.", now=NOW)
    dn.add("growth", "goal_update", "Goal set: 30-day sign-ups to 500.", now=NOW + timedelta(hours=1))
    dn.add("finance", "advice", "Move the launch budget to Q1.", now=NOW)
    text = dn.for_prompt("growth", "when is the launch?")
    lines = text.splitlines()
    assert lines[0].startswith("- 2026-10-03 Decision: Hold the spring launch")
    assert "Goal: Goal set: 30-day sign-ups" in text
    assert "budget" not in text
    assert dn.for_prompt("finance", "x").startswith("- 2026-10-03 Advice:")
    assert dn.for_prompt("legal", "launch") == ""
    assert dn.for_prompt(None, "launch") == ""


def test_nothing_kept_without_a_department_or_a_body() -> None:
    assert dn.add(None, "decision", "x") is None
    assert dn.add("growth", "decision", "   ") is None
    assert dn.for_prompt("growth", "x") == ""


def test_capped_and_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dn, "KEEP", 5)
    for i in range(8):
        dn.add("growth", "cadence_checkin", f"Check-in {i}: " + "y" * 900, now=NOW)
    text = dn.for_prompt("growth", "check-in")
    assert len(text) <= dn.MAX_CHARS
    assert "Check-in 7" in text and "Check-in 0" not in text
    conn = dn._connect(None)
    try:
        assert conn.execute("SELECT COUNT(*) FROM department_notes").fetchone()[0] == 5
    finally:
        conn.close()


def test_append_department_note_keeps_a_local_copy_with_honcho_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from openexecutive.memory import honcho_client

    monkeypatch.setattr(honcho_client, "get_settings", lambda: SimpleNamespace(honcho_enabled=False))
    monkeypatch.setattr(honcho_client, "_emit_peer_memory", lambda **k: None)
    honcho_client.append_department_note(department_slug="growth", kind="decision", body="Ship on Tuesday.")
    assert "Ship on Tuesday." in dn.for_prompt("growth", "ship")


def test_router_reads_local_notes_when_peer_memory_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import honcho_client
    from openexecutive.orchestrator import router

    async def _empty(**_k: object) -> str:
        return ""

    monkeypatch.setattr(honcho_client, "prefetch_department", _empty)
    monkeypatch.setattr("openexecutive.departments.registry.slug_for_specialist", lambda _s: "growth")
    dn.add("growth", "decision", "Hold the spring launch.", now=NOW)
    got = asyncio.run(router._prefetch_department_for_call({"specialist": "marketing", "query": "launch"}, None))
    assert "Hold the spring launch." in got
