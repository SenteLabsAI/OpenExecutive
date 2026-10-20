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
    # Chat only finds and reads playbooks; repeatable work is saved as a
    # workflow (draft_workflow), never as a new or changed playbook.
    assert names == set(SKILL_TOOL_HANDLERS) == {"search_skills", "load_skill"}
    for tool in SKILL_TOOLS:
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


def _live(name: str, body: str = "original", **fields: str) -> None:
    """Add a playbook straight to the library (what an approved draft does)."""
    skills_repo.create_skill(
        name=name,
        description=fields.get("description", "d"),
        when_to_use=fields.get("when_to_use", "w"),
        category=fields.get("category", "general"),
        body=body,
        store=skills_tools._get_store(),
    )


def test_load_missing_skill(isolated: None) -> None:
    result = json.loads(
        _run(SKILL_TOOL_HANDLERS["load_skill"]({"name": "does-not-exist"}))
    )
    assert "error" in result


def test_missing_required_fields(isolated: None) -> None:
    result = json.loads(_run(SKILL_TOOL_HANDLERS["search_skills"]({"query": ""})))
    assert "error" in result

    result = json.loads(_run(SKILL_TOOL_HANDLERS["load_skill"]({})))
    assert "error" in result


def test_search_hits_name_the_workflows_that_follow_them(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.workflows.playbooks import PlaybookUser

    _live(
        "month-review",
        body="steps",
        description="Monthly business review",
        when_to_use="month-end review",
        category="finance",
    )
    monkeypatch.setattr(
        skills_tools,
        "playbook_users",
        lambda: {"month-review": [PlaybookUser(name="mbr", title="MBR")]},
    )
    hits = json.loads(_run(SKILL_TOOL_HANDLERS["search_skills"]({"query": "monthly review"})))
    assert hits["results"][0]["workflows"] == ["mbr"]


def test_a_standalone_playbook_is_found_with_its_quick_workflow(isolated: None) -> None:
    _live(
        "vendor-review",
        body="contract terms, spend, service record, risks, renew or replace",
        description="Review a vendor before renewal",
        when_to_use="a vendor contract is up for renewal",
        category="operations",
    )
    hits = json.loads(_run(SKILL_TOOL_HANDLERS["search_skills"]({"query": "vendor renewal"})))
    assert hits["results"][0]["name"] == "vendor-review"
    assert hits["results"][0]["workflows"] == ["quick_vendor_review"]
