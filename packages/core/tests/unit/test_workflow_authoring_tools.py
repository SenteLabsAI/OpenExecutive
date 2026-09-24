"""Tests for the draft_workflow / save_workflow chat tools and their handshake."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.orchestrator import workflow_authoring_tools as wat  # noqa: E402
from openexecutive.workflows import dynamic_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    dynamic_store.initialize_dynamic_workflows_db(db_path)
    # The store resolves DB_PATH from its own module global at call time.
    monkeypatch.setattr(dynamic_store, "DB_PATH", db_path)
    return db_path


def _valid_definition() -> dict[str, Any]:
    return {
        "name": "weekly_watch",
        "title": "Weekly Watch",
        "input_fields": [{"name": "topic", "label": "Topic", "required": True}],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }


def _draft(definition: dict) -> dict:
    return json.loads(asyncio.run(wat.handle_draft_workflow({"definition": definition})))


def _save(definition: dict, token: str, **extra: Any) -> dict:
    payload = {"definition": definition, "confirm_token": token, **extra}
    return json.loads(asyncio.run(wat.handle_save_workflow(payload)))


def test_draft_returns_token_and_summary() -> None:
    out = _draft(_valid_definition())
    assert out["status"] == "drafted"
    assert out["confirm_token"]
    assert "Weekly Watch" in out["summary"]
    # Draft does NOT persist.
    assert dynamic_store.get_definition("weekly_watch") is None


def test_draft_rejects_invalid_definition() -> None:
    bad = _valid_definition()
    bad["steps"] = [bad["steps"][0]]  # no synthesis step
    out = _draft(bad)
    assert "error" in out
    assert "synthesis" in out["error"]


def test_save_persists_with_matching_token() -> None:
    definition = _valid_definition()
    token = _draft(definition)["confirm_token"]
    out = _save(definition, token)
    assert out["status"] == "saved"
    assert out["name"] == "weekly_watch"
    assert out["deep_link"].endswith("/jobs/weekly_watch")
    assert dynamic_store.get_definition("weekly_watch") is not None


def test_save_rejects_token_mismatch() -> None:
    definition = _valid_definition()
    token = _draft(definition)["confirm_token"]
    # Mutate the definition after drafting → token no longer matches.
    definition["title"] = "Changed After Draft"
    out = _save(definition, token)
    assert "error" in out
    assert "confirm_token does not match" in out["error"]
    assert dynamic_store.get_definition("weekly_watch") is None


def test_save_rejects_collision_without_overwrite() -> None:
    definition = _valid_definition()
    token = _draft(definition)["confirm_token"]
    assert _save(definition, token)["status"] == "saved"
    # Second save of the same name without overwrite is rejected.
    out2 = _save(definition, _draft(definition)["confirm_token"])
    assert "already exists" in out2["error"]


def test_save_overwrite_allowed() -> None:
    definition = _valid_definition()
    _save(definition, _draft(definition)["confirm_token"])
    definition["title"] = "Weekly Watch v2"
    token = _draft(definition)["confirm_token"]
    out = _save(definition, token, overwrite=True)
    assert out["status"] == "saved"
    stored = dynamic_store.get_definition("weekly_watch")
    assert stored is not None and stored.title == "Weekly Watch v2"


def test_save_rejects_is_active_flip() -> None:
    # Drafting active then saving inactive must be rejected (token covers
    # is_active) — the user approved an active workflow.
    definition = _valid_definition()
    definition["is_active"] = True
    token = _draft(definition)["confirm_token"]
    definition["is_active"] = False
    out = _save(definition, token)
    assert "error" in out
    assert "confirm_token does not match" in out["error"]


def test_draft_warns_on_existing_dynamic_name() -> None:
    definition = _valid_definition()
    _save(definition, _draft(definition)["confirm_token"])
    # A fresh draft for the same name should now carry an overwrite warning.
    out = _draft(definition)
    assert out["status"] == "drafted"
    assert "warning" in out and "already exists" in out["warning"]


def test_save_rejects_builtin_collision() -> None:
    definition = _valid_definition()
    definition["name"] = "board_prep"
    out = _draft(definition)
    # Even drafting fails validation (built-in collision).
    assert "error" in out and "built-in" in out["error"]


def _action_definition() -> dict[str, Any]:
    d = _valid_definition()
    d["steps"] = [
        {"kind": "action", "id": "file_bills", "title": "File bills",
         "goal": "Add {topic} bills to the sheet.", "tools": ["oe__read_file"]},
        {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
    ]
    return d


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record cadence scheduling instead of touching the scheduler DB."""
    from openexecutive.workflows import dynamic_cadence, dynamic_models

    calls: list[str] = []
    monkeypatch.setattr(dynamic_models, "_person_exists", lambda _pid: True)
    monkeypatch.setattr(dynamic_cadence, "cancel_cadence_rows", lambda _name: 0)
    monkeypatch.setattr(
        dynamic_cadence,
        "schedule_dynamic_workflow_cadence",
        lambda defn: calls.append(defn.name) or 1,
    )
    return calls


def _with_cadence(d: dict[str, Any]) -> dict[str, Any]:
    return {**d, "input_fields": [], "cadence": "daily@09:00", "cadence_person_id": 1,
            "steps": [{**s, "goal": s["goal"].replace("{topic}", "the")} if "goal" in s else s
                      for s in d["steps"]]}


def test_tool_workflow_drafts_with_requires_review() -> None:
    out = _draft(_action_definition())
    assert out["status"] == "drafted"
    assert out["requires_review"] is True
    assert "switched OFF" in out["note"]
    assert "action · tools: oe__read_file" in out["summary"]
    # Analysis-only drafts carry no review flag.
    assert "requires_review" not in _draft(_valid_definition())


def test_chat_saves_tool_workflow_switched_off(scheduled: list[str]) -> None:
    """Chat 'confirmation' is a token the model holds itself, so a tool
    workflow is saved inactive — a person turns it on from its review card."""
    d = _with_cadence(_action_definition())
    out = _save(d, wat._canonical_token(d))
    assert out["status"] == "saved_pending_review"
    assert out["deep_link"].endswith("/jobs/weekly_watch")
    stored = dynamic_store.get_definition("weekly_watch")
    assert stored is not None and stored.is_active is False
    # Inactive, so its cadence is not scheduled.
    assert scheduled == []


def test_chat_overwrite_switches_approved_tool_workflow_off(scheduled: list[str]) -> None:
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

    # An already-approved (active) tool workflow...
    dynamic_store.upsert_definition(DynamicWorkflowDef.model_validate(_action_definition()))
    assert dynamic_store.get_definition("weekly_watch").is_active is True  # type: ignore[union-attr]
    # ...edited from chat needs approving again.
    d = _action_definition()
    d["title"] = "Weekly Watch v2"
    out = _save(d, wat._canonical_token(d), overwrite=True)
    assert out["status"] == "saved_pending_review"
    stored = dynamic_store.get_definition("weekly_watch")
    assert stored is not None and stored.is_active is False
    assert stored.title == "Weekly Watch v2"


def test_analysis_workflow_still_saves_active_and_schedules(scheduled: list[str]) -> None:
    d = _with_cadence(_valid_definition())
    out = _save(d, wat._canonical_token(d))
    assert out["status"] == "saved"
    stored = dynamic_store.get_definition("weekly_watch")
    assert stored is not None and stored.is_active is True
    assert scheduled == ["weekly_watch"]
