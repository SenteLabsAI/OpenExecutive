"""Quick workflows: one per playbook that no other workflow follows."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from openexecutive.knowledge import skills_index, skills_repo
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.workflows import dynamic_store, get_workflow, list_workflows
from openexecutive.workflows import quick as quick_mod
from openexecutive.workflows.base import WorkflowSection
from openexecutive.workflows.dynamic_models import DynamicWorkflowDef, validate_definition
from openexecutive.workflows.playbooks import playbook_users
from openexecutive.workflows.quick import QuickWorkflow, quick_name, quick_workflows

# The built-in playbooks no built-in workflow follows.
BUILTIN_QUICK = {
    "quick_board_update_memo": "Board update memo",
    "quick_customer_interview_plan": "Customer interview plan",
    "quick_exec_1on1_template": "1:1 agenda",
    "quick_feature_prioritization": "Feature prioritization",
    "quick_layoff_comms_plan": "Layoff comms plan",
    "quick_market_sizing": "Market sizing",
    "quick_positioning_statement": "Positioning statement",
    "quick_post_mortem_template": "Post-mortem",
}


@pytest.fixture()
def no_custom(monkeypatch: pytest.MonkeyPatch) -> list[DynamicWorkflowDef]:
    defs: list[DynamicWorkflowDef] = []
    monkeypatch.setattr(
        dynamic_store,
        "list_definitions",
        lambda active_only=False, **_: [d for d in defs if d.is_active or not active_only],
    )
    monkeypatch.setattr(
        dynamic_store,
        "get_definition",
        lambda name, **_: next((d for d in defs if d.name == name), None),
    )
    return defs


@pytest.fixture()
def company(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ChromaDBStore]:
    """Built-ins as shipped; the company's own playbooks in a temp dir."""
    company_root = tmp_path / "company_skills"
    company_root.mkdir()
    for mod in (skills_index, skills_repo):
        monkeypatch.setattr(mod, "_company_skills_path", lambda: company_root)
    yield ChromaDBStore(persist_directory=str(tmp_path / "chroma"))


def _custom_following(playbook: str, *, active: bool = True) -> DynamicWorkflowDef:
    return DynamicWorkflowDef.model_validate({
        "name": "vendor_check",
        "title": "Vendor check",
        "is_active": active,
        "input_fields": [{"name": "vendor", "label": "Vendor"}],
        "steps": [
            {"kind": "specialist", "id": "look", "title": "Look", "specialist": "coo",
             "goal": "Review {vendor}.", "playbook": playbook},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    })


def test_every_builtin_playbook_belongs_to_a_workflow(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    quick = {w.name: w.title for w in quick_workflows()}
    assert quick == BUILTIN_QUICK
    users = playbook_users()
    shipped = {p.stem for p in skills_index.BUILTIN_SKILLS_PATH.rglob("*.md")}
    assert shipped == set(users)


def test_quick_workflows_are_listed_and_resolvable(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    listed = {w.name for w in list_workflows()}
    assert set(BUILTIN_QUICK) <= listed
    wf = get_workflow("quick_market_sizing")
    assert isinstance(wf, QuickWorkflow)
    meta = wf.meta()
    assert meta.quick is True
    assert meta.playbooks == ["market-sizing"]
    assert meta.section == WorkflowSection.GROWTH
    assert list(meta.input_schema["properties"]) == ["request"]
    assert get_workflow("board_prep").meta().quick is False
    with pytest.raises(KeyError):
        get_workflow("quick_no_such_playbook")


def test_a_company_playbook_is_a_quick_workflow_until_a_custom_one_follows_it(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    skills_repo.create_skill(
        name="vendor-review",
        description="Review a vendor before renewal",
        when_to_use="renewals",
        category="operations",
        body="contract terms, spend, risks",
        store=company,
    )
    wf = get_workflow(quick_name("vendor-review"))
    assert (wf.name, wf.title) == ("quick_vendor_review", "Vendor review")
    assert wf.meta().section == WorkflowSection.OPERATING

    # A switched-off custom workflow that follows it still counts.
    no_custom.append(_custom_following("vendor-review", active=False))
    assert "quick_vendor_review" not in {w.name for w in quick_workflows()}
    assert [u.name for u in playbook_users()["vendor-review"]] == ["vendor_check"]


def test_a_hidden_builtin_playbook_has_no_quick_workflow(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    skills_repo.delete_skill("market-sizing", store=company)  # hides the built-in
    assert "quick_market_sizing" not in {w.name for w in quick_workflows()}


def test_playbooks_that_share_a_quick_name_get_none(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    # `market_sizing` and the built-in `market-sizing` both map to
    # quick_market_sizing: neither is offered, and neither resolves.
    skills_repo.create_skill(
        name="market_sizing",
        description="d",
        when_to_use="w",
        category="board",
        body="another method",
        store=company,
    )
    assert "quick_market_sizing" not in {w.name for w in quick_workflows()}
    with pytest.raises(KeyError):
        get_workflow("quick_market_sizing")


def test_no_quick_workflows_when_custom_workflows_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_: Any) -> list[Any]:
        raise RuntimeError("db down")

    # Unknown followers: offer none rather than a Quick workflow for a
    # playbook some custom workflow may follow.
    monkeypatch.setattr(dynamic_store, "list_definitions", boom)
    assert quick_workflows() == []
    with pytest.raises(RuntimeError):
        quick_workflows(strict=True)


def test_a_stored_custom_workflow_keeps_an_older_quick_name(
    company: ChromaDBStore, no_custom: list[DynamicWorkflowDef]
) -> None:
    # Saved as `quick_market_sizing` before the prefix was reserved: it is
    # still the one that name lists and runs.
    legacy = _custom_following("").model_copy(update={"name": "quick_market_sizing"})
    no_custom.append(legacy)
    assert "quick_market_sizing" not in {w.name for w in quick_workflows()}
    assert [w.name for w in list_workflows()].count("quick_market_sizing") == 1
    assert not isinstance(get_workflow("quick_market_sizing"), QuickWorkflow)


def test_custom_workflows_may_not_take_a_quick_name() -> None:
    defn = _custom_following("").model_copy(update={"name": "quick_vendor"})
    assert any("may not start with 'quick_'" in e for e in validate_definition(defn))


@pytest.mark.asyncio
async def test_run_follows_the_playbook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_route(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "Timeline, cause, impact."

    profile = MagicMock()
    profile.is_empty.return_value = True
    profile.name = "Acme"
    monkeypatch.setattr(quick_mod, "load_or_create_profile", lambda: profile)
    monkeypatch.setattr(quick_mod, "route_to_specialist", fake_route)
    monkeypatch.setattr(quick_mod, "retrieve", lambda **_: "")
    monkeypatch.setattr(quick_mod, "load_playbook", lambda name: f"METHOD for {name}")

    wf = QuickWorkflow(skills_repo.get_skill("post-mortem-template"))
    inputs = wf.input_model()(request="Tuesday's checkout outage")
    events = [e async for e in wf.run(inputs=inputs, store=MagicMock())]

    assert not [e for e in events if e.type == "error"]
    assert calls[0]["specialist_name"] == "coo"
    assert "Tuesday's checkout outage" in calls[0]["query"]
    assert calls[0]["query"].endswith("Follow this method:\n\nMETHOD for post-mortem-template")
    artifact = next(e for e in events if e.type == "artifact")
    assert artifact.content == "# Post-mortem\n\nTimeline, cause, impact."
    assert [e.step_id for e in events if e.type == "step_done"] == ["context", "draft"]


@pytest.mark.asyncio
async def test_run_stops_when_the_playbook_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_route(**_: Any) -> str:
        raise AssertionError("must not draft without the method")

    profile = MagicMock()
    profile.name = "Acme"
    monkeypatch.setattr(quick_mod, "load_or_create_profile", lambda: profile)
    monkeypatch.setattr(quick_mod, "route_to_specialist", fake_route)
    monkeypatch.setattr(quick_mod, "retrieve", lambda **_: "")
    monkeypatch.setattr(quick_mod, "load_playbook", lambda name: "")

    wf = QuickWorkflow(skills_repo.get_skill("post-mortem-template"))
    inputs = wf.input_model()(request="Tuesday's checkout outage")
    events = [e async for e in wf.run(inputs=inputs, store=MagicMock())]

    assert events[-1].type == "error"
    assert not [e for e in events if e.type == "artifact"]
