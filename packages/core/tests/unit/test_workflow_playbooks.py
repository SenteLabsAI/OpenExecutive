"""Workflows follow playbooks (skills): declaration, loading, prompts, dynamic steps."""
from __future__ import annotations

import inspect
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.knowledge import skills_index, skills_repo  # noqa: E402
from openexecutive.knowledge.store import ChromaDBStore  # noqa: E402
from openexecutive.workflows import (  # noqa: E402
    WORKFLOW_REGISTRY,  # noqa: E402
    competitive_teardown,
    exec_search_brief,
    fundraising_prep,
    mbr,
)
from openexecutive.workflows import dynamic as dyn  # noqa: E402
from openexecutive.workflows import playbooks as pb  # noqa: E402
from openexecutive.workflows.dynamic import DynamicWorkflow  # noqa: E402
from openexecutive.workflows.dynamic_models import (  # noqa: E402
    DynamicWorkflowDef,
    validate_definition,
)

# The six overlapping topics: each workflow follows its playbook.
EXPECTED = {
    "board_prep": ("board-prep-deck",),
    "quarterly_plan": ("quarterly-okr-set", "quarterly-forecast"),
    "mbr": ("monthly-business-review",),
    "competitive_teardown": ("competitive-teardown",),
    "fundraising_prep": ("fundraise-narrative",),
    "exec_search_brief": ("role-scorecard",),
    # The weekly review's top three follows its own playbook.
    "weekly_review": ("weekly-review",),
}


@pytest.fixture()
def skills_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChromaDBStore]:
    builtin_root = tmp_path / "builtin_skills"
    company_root = tmp_path / "company_skills"
    builtin_root.mkdir()
    company_root.mkdir()
    for mod in (skills_index, skills_repo):
        monkeypatch.setattr(mod, "BUILTIN_SKILLS_PATH", builtin_root)
        monkeypatch.setattr(mod, "_company_skills_path", lambda: company_root)
    yield ChromaDBStore(persist_directory=str(tmp_path / "chroma"))


def _write_builtin(name: str, body: str = "builtin steps") -> None:
    path = skills_index.BUILTIN_SKILLS_PATH / "general" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: d\nwhen_to_use: w\ncategory: general\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_builtins_declare_the_expected_playbooks() -> None:
    declared = {n: wf.playbooks for n, wf in WORKFLOW_REGISTRY.items() if wf.playbooks}
    assert declared == EXPECTED
    for name, wf in WORKFLOW_REGISTRY.items():
        assert wf.meta().playbooks == list(wf.playbooks), name


def test_declared_playbooks_ship_and_match_what_run_loads() -> None:
    """A playbook a workflow loads but doesn't declare would miss the hide warning."""
    shipped = {p.stem for p in skills_index.BUILTIN_SKILLS_PATH.rglob("*.md")}
    for name, wf in WORKFLOW_REGISTRY.items():
        source = inspect.getsource(inspect.getmodule(type(wf)))  # type: ignore[arg-type]
        loaded = set(re.findall(r'load_playbook\("([^"]+)"\)', source))
        assert loaded == set(wf.playbooks), name
        assert set(wf.playbooks) <= shipped, name


def test_load_playbook_follows_customization_and_hiding(skills_dirs: ChromaDBStore) -> None:
    _write_builtin("deck")
    assert pb.load_playbook("deck").strip() == "builtin steps"

    skills_repo.update_skill(
        name="deck", description="d", when_to_use="w", category="general",
        body="our steps", store=skills_dirs,
    )
    assert pb.load_playbook("deck").strip() == "our steps"

    assert skills_repo.delete_skill("deck", store=skills_dirs) == "reverted"
    assert skills_repo.delete_skill("deck", store=skills_dirs) == "hidden"
    assert pb.load_playbook("deck") == ""
    assert pb.load_playbook("never-existed") == ""
    assert pb.load_playbook("../escape") == ""


def test_playbook_clause() -> None:
    assert pb.playbook_clause("", "Follow") == ""
    assert pb.playbook_clause("  \n", "Follow") == ""
    assert pb.playbook_clause("steps", "Follow this") == "\n\nFollow this:\n\nsteps"


@pytest.mark.parametrize(
    ("module", "ctx_cls", "builders"),
    [
        (mbr, "_MBRContext",
         ["_build_financial_summary_prompt", "_build_kpi_prompt", "_build_function_prompt"]),
        (competitive_teardown, "_CTCtx",
         ["_build_positioning_prompt", "_build_product_prompt",
          "_build_counter_prompt", "_build_battlecard_prompt"]),
        (fundraising_prep, "_FundraisingPrepContext", ["_build_narrative_prompt"]),
        (exec_search_brief, "_ESCtx", ["_build_profile_prompt"]),
    ],
)
def test_wired_prompts_carry_the_playbook(module: Any, ctx_cls: str, builders: list[str]) -> None:
    wf = next(w for w in WORKFLOW_REGISTRY.values() if inspect.getmodule(type(w)) is module)
    ctx = getattr(module, ctx_cls)(wf.input_model()(**wf.sample_inputs()))
    for builder in builders:
        ctx.playbook = "PLAYBOOK-MARKER"
        assert getattr(module, builder)(ctx).endswith("PLAYBOOK-MARKER"), builder
        ctx.playbook = ""
        assert "PLAYBOOK-MARKER" not in getattr(module, builder)(ctx), builder


def _dyn_def(playbook: str) -> DynamicWorkflowDef:
    return DynamicWorkflowDef.model_validate({
        "name": "weekly_watch",
        "title": "Weekly Watch",
        "input_fields": [{"name": "topic", "label": "Topic"}],
        "steps": [
            {"kind": "specialist", "id": "research", "title": "Research",
             "specialist": "cso", "goal": "Analyze {topic}.", "playbook": playbook},
            {"kind": "specialist", "id": "again", "title": "Again",
             "specialist": "cfo", "goal": "More on {topic}.", "playbook": playbook},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    })


def test_dynamic_step_playbook_must_exist(skills_dirs: ChromaDBStore) -> None:
    _write_builtin("deck")
    assert not [e for e in validate_definition(_dyn_def("deck")) if "playbook" in e]
    assert any("unknown playbook 'nope'" in e for e in validate_definition(_dyn_def("nope")))

    skills_repo.delete_skill("deck", store=skills_dirs)  # hides it
    assert any("unknown playbook" in e for e in validate_definition(_dyn_def("deck")))


def test_dynamic_followed_playbooks_are_deduplicated() -> None:
    wf = DynamicWorkflow(_dyn_def("deck"))
    assert wf.followed_playbooks() == ["deck"]
    assert wf.meta().playbooks == ["deck"]
    assert DynamicWorkflow(_dyn_def("")).meta().playbooks == []


@pytest.mark.asyncio
async def test_dynamic_step_appends_playbook_after_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "section"

    profile = MagicMock()
    profile.is_empty.return_value = True
    monkeypatch.setattr(dyn, "load_or_create_profile", lambda: profile)
    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)
    # Braces in a playbook body must not be treated as input placeholders.
    monkeypatch.setattr(dyn, "load_playbook", lambda name: f"Use {{braces}} from {name}")

    wf = DynamicWorkflow(_dyn_def("deck"))
    events = [e async for e in wf.run(inputs=wf.input_model()(topic="pricing"), store=MagicMock())]

    assert not [e for e in events if e.type == "error"]
    assert calls[0]["query"] == (
        "Analyze pricing.\n\nFollow this playbook:\n\nUse {braces} from deck"
    )


def test_playbook_users_maps_builtins_and_survives_store_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows import dynamic_store

    def boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(dynamic_store, "list_definitions", boom)
    users = pb.playbook_users()
    assert [u.name for u in users["monthly-business-review"]] == ["mbr"]
    assert [u.name for u in users["quarterly-forecast"]] == ["quarterly_plan"]


def test_playbook_users_include_switched_off_custom_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.workflows import dynamic_store

    off = _dyn_def("deck").model_copy(update={"is_active": False})
    seen: dict[str, Any] = {}

    def fake_list(**kwargs: Any) -> list[DynamicWorkflowDef]:
        seen.update(kwargs)
        return [off]

    monkeypatch.setattr(dynamic_store, "list_definitions", fake_list)
    users = pb.playbook_users()
    assert seen == {"active_only": False}
    assert [(u.name, u.is_custom) for u in users["deck"]] == [("weekly_watch", True)]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: bad\ndescription: 2024\nwhen_to_use: w\ncategory: general\n",
        "name: bad\ndescription: yes\nwhen_to_use: w\ncategory: general\n",
        "name: [bad]\ndescription: d\nwhen_to_use: w\ncategory: general\n",
    ],
)
def test_non_text_frontmatter_is_a_parse_error(
    skills_dirs: ChromaDBStore, frontmatter: str
) -> None:
    from openexecutive.knowledge.skills import SkillParseError, parse_skill_file

    path = skills_index.BUILTIN_SKILLS_PATH / "general" / "bad.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\nbody\n", encoding="utf-8")
    with pytest.raises(SkillParseError):
        parse_skill_file(path, source="builtin")
    assert pb.load_playbook("bad") == ""


def test_undecodable_skill_file_is_a_parse_error(skills_dirs: ChromaDBStore) -> None:
    from openexecutive.knowledge.skills import SkillParseError, parse_skill_file

    path = skills_index.BUILTIN_SKILLS_PATH / "general" / "binary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(SkillParseError):
        parse_skill_file(path, source="builtin")
    assert pb.load_playbook("binary") == ""


def test_load_playbook_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pb, "get_skill", boom)
    assert pb.load_playbook("anything") == ""


def test_malformed_company_copy_falls_back_to_builtin(skills_dirs: ChromaDBStore) -> None:
    _write_builtin("deck")
    company = skills_repo._company_skills_path() / "general" / "deck.md"
    company.parent.mkdir(parents=True)
    company.write_text(
        "---\nname: deck\ndescription: 2024\nwhen_to_use: w\ncategory: general\n---\n\nx\n",
        encoding="utf-8",
    )
    assert pb.load_playbook("deck").strip() == "builtin steps"


@pytest.mark.asyncio
async def test_dynamic_step_reports_an_unavailable_playbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "section"

    profile = MagicMock()
    profile.is_empty.return_value = True
    monkeypatch.setattr(dyn, "load_or_create_profile", lambda: profile)
    monkeypatch.setattr(dyn, "route_to_specialist", fake_route)
    monkeypatch.setattr(dyn, "load_playbook", lambda name: "")

    wf = DynamicWorkflow(_dyn_def("gone"))
    events = [e async for e in wf.run(inputs=wf.input_model()(topic="pricing"), store=MagicMock())]

    notices = [e for e in events if e.type == "progress" and e.step_id == "research"]
    assert notices and "gone" in (notices[0].summary or "")
    assert calls[0]["query"] == "Analyze pricing."
    assert [e for e in events if e.type == "artifact"]


def test_playbook_users_strict_raises_on_store_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.workflows import dynamic_store

    def boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(dynamic_store, "list_definitions", boom)
    with pytest.raises(RuntimeError):
        pb.playbook_users(strict=True)


def test_authoring_summary_names_the_playbook() -> None:
    from openexecutive.orchestrator.workflow_authoring_tools import _summarize

    summary = _summarize(_dyn_def("deck"))
    assert "[cso · follows playbook deck] Research" in summary
    assert "follows playbook" not in _summarize(_dyn_def(""))
