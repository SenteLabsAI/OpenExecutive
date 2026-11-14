"""CRUD round-trip tests for the dynamic-workflow definition store."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.workflows import dynamic_store  # noqa: E402
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "episodic.db"
    dynamic_store.initialize_dynamic_workflows_db(path)
    return path


def _def(name: str = "weekly_watch", **overrides) -> DynamicWorkflowDef:
    base = {
        "name": name,
        "title": "Weekly Watch",
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research", "specialist": "cso", "goal": "Go."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    base.update(overrides)
    return DynamicWorkflowDef.model_validate(base)


def test_upsert_and_get(db: Path) -> None:
    dynamic_store.upsert_definition(_def(), db_path=db)
    got = dynamic_store.get_definition("weekly_watch", db_path=db)
    assert got is not None
    assert got.title == "Weekly Watch"
    assert got.created_at and got.updated_at


def test_get_missing_returns_none(db: Path) -> None:
    assert dynamic_store.get_definition("nope", db_path=db) is None


def test_list_active_only(db: Path) -> None:
    dynamic_store.upsert_definition(_def("active_wf"), db_path=db)
    dynamic_store.upsert_definition(_def("inactive_wf", is_active=False), db_path=db)
    active = [d.name for d in dynamic_store.list_definitions(active_only=True, db_path=db)]
    everything = [d.name for d in dynamic_store.list_definitions(active_only=False, db_path=db)]
    assert active == ["active_wf"]
    assert set(everything) == {"active_wf", "inactive_wf"}


def test_update_preserves_created_at(db: Path) -> None:
    first = dynamic_store.upsert_definition(_def(), db_path=db)
    updated = dynamic_store.upsert_definition(
        _def(title="Renamed"), db_path=db
    )
    assert updated.created_at == first.created_at
    assert updated.title == "Renamed"


def test_set_active_toggles(db: Path) -> None:
    dynamic_store.upsert_definition(_def(), db_path=db)
    assert dynamic_store.set_active("weekly_watch", False, db_path=db) is True
    got = dynamic_store.get_definition("weekly_watch", db_path=db)
    assert got is not None and got.is_active is False
    # active_only list now excludes it.
    assert dynamic_store.list_definitions(active_only=True, db_path=db) == []


def test_set_active_missing_returns_false(db: Path) -> None:
    assert dynamic_store.set_active("nope", True, db_path=db) is False


def test_delete(db: Path) -> None:
    dynamic_store.upsert_definition(_def(), db_path=db)
    assert dynamic_store.delete_definition("weekly_watch", db_path=db) is True
    assert dynamic_store.delete_definition("weekly_watch", db_path=db) is False
    assert dynamic_store.get_definition("weekly_watch", db_path=db) is None


def test_missing_db_is_safe(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    assert dynamic_store.list_definitions(db_path=missing) == []
    assert dynamic_store.get_definition("x", db_path=missing) is None
    assert dynamic_store.delete_definition("x", db_path=missing) is False


# --- Approved targets across an edit ---------------------------------------


def _tool_def(goal: str = "Email the weekly numbers.", tools: list[str] | None = None) -> DynamicWorkflowDef:
    return _def(steps=[
        {"kind": "action", "id": "send", "title": "Send", "goal": goal,
         "tools": tools or ["send_gmail_message"]},
    ])


def _approve(db: Path) -> None:
    from openexecutive.workflows import approved_targets

    approved_targets.remember("weekly_watch", [("to", "investor@example.com")], db_path=db)


def _approved(db: Path) -> set[str]:
    from openexecutive.workflows import approved_targets

    return approved_targets.approved_values("weekly_watch", db_path=db)


def test_the_approvers_own_edit_keeps_the_approved_targets(db: Path) -> None:
    dynamic_store.upsert_definition(_tool_def(), db_path=db)
    _approve(db)

    dynamic_store.upsert_definition(_tool_def("Email the numbers and a chart."), db_path=db)

    assert _approved(db) == {"investor@example.com"}


def test_someone_elses_edit_to_the_steps_drops_the_approved_targets(db: Path) -> None:
    dynamic_store.upsert_definition(_tool_def(), db_path=db)
    _approve(db)

    # Not the steps: nothing a run writes changed, so nothing to ask again.
    dynamic_store.upsert_definition(
        _tool_def().model_copy(update={"title": "Weekly numbers"}), db_path=db, keep_approvals=False,
    )
    assert _approved(db) == {"investor@example.com"}

    dynamic_store.upsert_definition(
        _tool_def("Email the full customer list."), db_path=db, keep_approvals=False,
    )
    assert _approved(db) == set()


def test_save_if_unchanged_drops_them_for_someone_elses_edit_too(db: Path) -> None:
    stored = dynamic_store.upsert_definition(_tool_def(), db_path=db)
    _approve(db)

    saved = dynamic_store.save_if_unchanged(
        _tool_def("Email the full customer list."), stored, db_path=db, keep_approvals=False,
    )

    assert saved is not None
    assert _approved(db) == set()


def test_only_the_approver_or_the_principal_keeps_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.memory import episodic
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    from openexecutive.workflows import approved_targets

    path = tmp_path / "people.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(dynamic_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    owner = people_store.upsert_person(full_name="Jordan Avery", is_principal=True, email="j@example.com")
    creator = people_store.upsert_person(full_name="Sam Lee", email="s@example.com")
    other = people_store.upsert_person(full_name="Dana Cho", email="d@example.com")
    people_registry.invalidate()
    dynamic_store.initialize_dynamic_workflows_db(path)
    dynamic_store.upsert_definition(_tool_def(), db_path=path, owner_person_id=creator)

    assert approved_targets.kept_on_edit_by("weekly_watch", creator)
    assert approved_targets.kept_on_edit_by("weekly_watch", owner)
    assert not approved_targets.kept_on_edit_by("weekly_watch", other)
    assert not approved_targets.kept_on_edit_by("weekly_watch", None)
    people_registry.invalidate()
