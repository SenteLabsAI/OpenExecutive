"""Unit tests for the departments.store goal → Honcho dept-peer mirror.

insert_goal fires one ``append_department_note(kind="goal_update")``
with ``transition=created``; update_goal fires one with
``transition=updated`` (only on real row updates — no-op updates and
unknown goal_ids don't fire).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.departments import store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated SQLite + seeded departments. Patches DB_PATH so
    insert_goal's default db_path lookup also hits the test DB."""
    path = tmp_path / "depts.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.initialize_db()
    store.seed_default_departments()
    return path


@pytest.fixture
def mirror_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _spy(
        *,
        department_slug: str | None,
        kind: str,
        body: str,
        person_id: int | None = None,
    ) -> None:
        calls.append(
            {
                "department_slug": department_slug,
                "kind": kind,
                "body": body,
                "person_id": person_id,
            }
        )

    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.append_department_note", _spy
    )
    return calls


def test_insert_goal_fires_created_mirror(
    db: Path, mirror_calls: list[dict[str, Any]]
) -> None:
    store.insert_goal(
        department_slug="finance",
        period_value="Q3-2026",
        key_result="Cut burn by 20%",
        target="$1.2M monthly",
        db_path=db,
    )
    assert len(mirror_calls) == 1
    c = mirror_calls[0]
    assert c["department_slug"] == "finance"
    assert c["kind"] == "goal_update"
    assert "Goal created" in c["body"]
    assert "Cut burn by 20%" in c["body"]
    assert "Target: $1.2M monthly" in c["body"]


def test_mirror_omits_an_empty_target(
    db: Path, mirror_calls: list[dict[str, Any]]
) -> None:
    store.insert_goal(
        department_slug="finance",
        period_value="Q3 2026",
        key_result="Close Series A",
        target="",
        db_path=db,
    )
    assert len(mirror_calls) == 1
    assert "Target:" not in mirror_calls[0]["body"]
    assert "Close Series A" in mirror_calls[0]["body"]

def test_update_goal_fires_updated_mirror(
    db: Path, mirror_calls: list[dict[str, Any]]
) -> None:
    goal_id = store.insert_goal(
        department_slug="finance",
        period_value="Q3-2026",
        key_result="Cut burn by 20%",
        target="$1.2M monthly",
        db_path=db,
    )
    mirror_calls.clear()
    store.update_goal(goal_id, current="$1.4M", status="at_risk", db_path=db)
    assert len(mirror_calls) == 1
    c = mirror_calls[0]
    assert "Goal updated" in c["body"]
    # Re-fetched merged state — body restates the full KR, not just the deltas.
    assert "Cut burn by 20%" in c["body"]
    assert "Current: $1.4M" in c["body"]
    assert "Status: at_risk" in c["body"]


def test_update_goal_no_op_does_not_fire_mirror(
    db: Path, mirror_calls: list[dict[str, Any]]
) -> None:
    """update_goal with no fields supplied is a degenerate no-op that
    should not mirror — it didn't change anything."""
    goal_id = store.insert_goal(
        department_slug="finance",
        period_value="Q3-2026",
        key_result="x",
        target="y",
        db_path=db,
    )
    mirror_calls.clear()
    store.update_goal(goal_id, db_path=db)  # no fields
    assert mirror_calls == []


def test_update_goal_unknown_id_does_not_fire_mirror(
    db: Path, mirror_calls: list[dict[str, Any]]
) -> None:
    store.update_goal(99999, current="x", db_path=db)
    assert mirror_calls == []


def test_goal_mirror_failure_does_not_break_sql_write(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror is wrapped in try/except so a Honcho explosion never
    rolls back the SQL goal mutation."""

    def boom(**_: Any) -> None:
        raise RuntimeError("honcho boom")

    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.append_department_note", boom
    )
    # Must not raise.
    goal_id = store.insert_goal(
        department_slug="finance",
        period_value="Q3-2026",
        key_result="x",
        target="y",
        db_path=db,
    )
    # SQL row still visible.
    g = store.get_goal(goal_id, db_path=db)
    assert g is not None
    assert g.key_result == "x"
