"""Tests for the Anthropic tool handlers exposed to the Executive."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from openexecutive.knowledge import skills_index, skills_repo
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.orchestrator import skills_tools
from openexecutive.orchestrator.skills_tools import (
    SKILL_TOOL_HANDLERS,
    SKILL_TOOLS,
)


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    builtin_root = tmp_path / "builtin_skills"
    company_root = tmp_path / "company_skills"
    builtin_root.mkdir()
    company_root.mkdir()

    monkeypatch.setattr(skills_index, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_repo, "BUILTIN_SKILLS_PATH", builtin_root)
    monkeypatch.setattr(skills_index, "_company_skills_path", lambda: company_root)
    monkeypatch.setattr(skills_repo, "_company_skills_path", lambda: company_root)

    store = ChromaDBStore(persist_directory=str(tmp_path / "chroma"))
    monkeypatch.setattr(skills_tools, "_get_store", lambda: store)
    yield


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_tool_schema_shape() -> None:
    names = {t["name"] for t in SKILL_TOOLS}
    assert names == set(SKILL_TOOL_HANDLERS)
    for tool in SKILL_TOOLS:
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


def test_create_then_search_then_load(isolated: None) -> None:
    create_result = json.loads(
        _run(SKILL_TOOL_HANDLERS["create_skill"]({
            "name": "revenue-summary",
            "description": "Weekly revenue summary",
            "when_to_use": "Each Monday morning",
            "category": "finance",
            "body": "# Revenue summary\n\nSteps go here.",
        }))
    )
    assert create_result["saved"] is True
    assert create_result["name"] == "revenue-summary"

    search_result = json.loads(
        _run(SKILL_TOOL_HANDLERS["search_skills"]({"query": "weekly revenue"}))
    )
    names = [h["name"] for h in search_result["results"]]
    assert "revenue-summary" in names
    # search_skills must NOT include body
    assert all("body" not in h for h in search_result["results"])

    load_result = _run(SKILL_TOOL_HANDLERS["load_skill"]({"name": "revenue-summary"}))
    assert isinstance(load_result, str)
    assert "Revenue summary" in load_result
    assert "Steps go here" in load_result


def test_create_conflict(isolated: None) -> None:
    _run(SKILL_TOOL_HANDLERS["create_skill"]({
        "name": "dup",
        "description": "d",
        "when_to_use": "w",
        "category": "general",
        "body": "b",
    }))
    second = json.loads(
        _run(SKILL_TOOL_HANDLERS["create_skill"]({
            "name": "dup",
            "description": "d2",
            "when_to_use": "w2",
            "category": "general",
            "body": "b2",
        }))
    )
    assert second["code"] == "conflict"


def test_load_missing_skill(isolated: None) -> None:
    result = json.loads(
        _run(SKILL_TOOL_HANDLERS["load_skill"]({"name": "does-not-exist"}))
    )
    assert "error" in result


def test_update_and_delete(isolated: None) -> None:
    _run(SKILL_TOOL_HANDLERS["create_skill"]({
        "name": "edit-me",
        "description": "d",
        "when_to_use": "w",
        "category": "general",
        "body": "b",
    }))
    updated = json.loads(
        _run(SKILL_TOOL_HANDLERS["update_skill"]({
            "name": "edit-me",
            "description": "new desc",
            "when_to_use": "new when",
            "category": "general",
            "body": "new body",
        }))
    )
    assert updated["updated"] is True

    deleted = json.loads(_run(SKILL_TOOL_HANDLERS["delete_skill"]({"name": "edit-me"})))
    assert deleted["deleted"] is True
    assert deleted["outcome"] == "deleted"

    deleted_again = json.loads(
        _run(SKILL_TOOL_HANDLERS["delete_skill"]({"name": "edit-me"}))
    )
    assert deleted_again["code"] == "not_found"


def test_deleting_builtin_hides_it_from_load(isolated: None) -> None:
    target = skills_index.BUILTIN_SKILLS_PATH / "board" / "stock.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\nname: stock\ndescription: d\nwhen_to_use: w\ncategory: board\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert "body" in _run(SKILL_TOOL_HANDLERS["load_skill"]({"name": "stock"}))

    deleted = json.loads(_run(SKILL_TOOL_HANDLERS["delete_skill"]({"name": "stock"})))
    assert deleted["outcome"] == "hidden"
    loaded = json.loads(_run(SKILL_TOOL_HANDLERS["load_skill"]({"name": "stock"})))
    assert "not found" in loaded["error"]


def test_missing_required_fields(isolated: None) -> None:
    result = json.loads(_run(SKILL_TOOL_HANDLERS["search_skills"]({"query": ""})))
    assert "error" in result

    result = json.loads(_run(SKILL_TOOL_HANDLERS["load_skill"]({})))
    assert "error" in result
