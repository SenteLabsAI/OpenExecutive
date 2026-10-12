"""Tests for departments/rollup.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.departments.rollup import render_company_rollup
from openexecutive.memory import episodic
from openexecutive.people import store as people_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "rollup.db"
    monkeypatch.setattr(dept_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    dept_registry.invalidate()
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    dept_store.initialize_db(path)
    dept_store.seed_default_departments(db_path=path)
    yield path
    dept_registry.invalidate()


def test_rollup_shows_owner_goal_state_and_gaps(db: Path) -> None:
    pid = people_store.upsert_person(full_name="Dingus", role="CPO", db_path=db)
    dept_store.update_department("product", head_person_id=pid, db_path=db)
    gid = dept_store.insert_goal(
        "product", period_value="Q4 2026", key_result="Tenant isolation holds",
        target="No leakage in testing", status="at_risk", db_path=db,
    )
    dept_store.update_goal(gid, current="two of three tests written", db_path=db)

    block = render_company_rollup(exclude_slug="strategy", db_path=db)

    assert block.startswith("<company_rollup>") and block.endswith("</company_rollup>")
    assert "### Product (owner: Dingus;" in block
    assert "[at_risk] Tenant isolation holds" in block
    assert "current: two of three tests written" in block
    assert "last reviewed: never reviewed" in block
    assert "### Strategy" not in block
    # A department with no goals is called out rather than hidden.
    assert "### Finance (owner: unassigned;" in block and "No goals set." in block


def test_rollup_is_empty_without_departments(tmp_path: Path) -> None:
    assert render_company_rollup(db_path=tmp_path / "missing.db") == ""
