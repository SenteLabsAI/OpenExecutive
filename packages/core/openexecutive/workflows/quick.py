"""Quick workflows: one per playbook that no other workflow follows.

A playbook (``knowledge/skills_repo.py``) is the method for a piece of work.
Most built-in ones sit behind a workflow that follows them (board prep, the
MBR, the teardown...). The rest (a post-mortem, a positioning statement, a
company's own playbooks) would otherwise be methods with no way to run
them, so each one is offered as a Quick workflow: say what it is for, and
the area's specialist drafts the document following that playbook.

Quick workflows are derived, not stored. The set is worked out from the
playbooks in effect whenever the catalog is read, so a playbook a company
adds shows up as one, one it hides drops out, and one a custom workflow
starts following stops being offered on its own. Their names carry the
``quick_`` prefix, which custom workflows may not use.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from openexecutive.knowledge.retriever import retrieve
from openexecutive.knowledge.skills import Skill
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.onboarding.profile_builder import load_or_create_profile
from openexecutive.orchestrator.router import route_to_specialist
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowMeta,
    WorkflowSection,
    WorkflowStepDef,
)
from openexecutive.workflows.playbooks import load_playbook, playbook_clause

logger = logging.getLogger(__name__)

QUICK_PREFIX = "quick_"

# The specialist who drafts a playbook of each category, and where the
# workflow is filed on /jobs.
_SPECIALIST_FOR: dict[str, str] = {
    "strategy": "cso",
    "finance": "cfo",
    "hr": "chro",
    "legal": "gc",
    "operations": "coo",
    "marketing": "cmo",
    "product": "cpo",
    "board": "board_comms",
    "sales": "sales",
}
_SECTION_FOR: dict[str, WorkflowSection] = {
    "strategy": WorkflowSection.GROWTH,
    "finance": WorkflowSection.CAPITAL,
    "hr": WorkflowSection.PEOPLE,
    "legal": WorkflowSection.RISK,
    "operations": WorkflowSection.OPERATING,
    "marketing": WorkflowSection.GROWTH,
    "product": WorkflowSection.PRODUCT,
    "board": WorkflowSection.BOARD,
    "sales": WorkflowSection.GROWTH,
}

# Titles for built-in playbooks whose file names don't read well as one.
_TITLES: dict[str, str] = {
    "exec-1on1-template": "1:1 agenda",
    "post-mortem-template": "Post-mortem",
}


def quick_name(playbook: str) -> str:
    """The Quick workflow name for `playbook`."""
    return QUICK_PREFIX + playbook.replace("-", "_").lower()


def _title(playbook: str) -> str:
    if playbook in _TITLES:
        return _TITLES[playbook]
    words = playbook.replace("_", "-").split("-")
    if len(words) > 1 and words[-1] == "template":
        words = words[:-1]
    text = " ".join(w for w in words if w)
    return text[:1].upper() + text[1:]


class QuickInput(BaseModel):
    """A Quick workflow's one field."""

    request: str = Field(
        ...,
        min_length=3,
        description=(
            "What this is for, and anything it should cover: the situation, "
            "the audience, the facts to use."
        ),
    )


class QuickWorkflow(Workflow):
    """Drafts one document by following one playbook."""

    estimated_minutes = 2

    def __init__(self, skill: Skill) -> None:
        fm = skill.frontmatter
        self.playbook = fm.name
        self.name = quick_name(fm.name)
        self.title = _title(fm.name)
        self.description = fm.description
        self.section = _SECTION_FOR.get(fm.category, WorkflowSection.OPERATING)
        self.specialist = _SPECIALIST_FOR.get(fm.category, "cso")
        self.playbooks = (fm.name,)

    def input_model(self) -> type[BaseModel]:
        return QuickInput

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull the company profile and relevant knowledge.",
            ),
            WorkflowStepDef(
                id="draft",
                title=f"Draft the {self.title[:1].lower() + self.title[1:]}",
                description="Written by following how it's done.",
            ),
        ]

    def meta(self) -> WorkflowMeta:
        return super().meta().model_copy(update={"quick": True})

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, QuickInput)
        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        profile = load_or_create_profile()
        method = load_playbook(self.playbook)
        rag = retrieve(
            query=inputs.request,
            specialist_name=self.specialist,
            n_builtin=4,
            n_company=4,
            store=store,
        )
        if not method:
            # The method is the point of a Quick workflow: without it the
            # draft would be a generic one passed off as following it.
            yield WorkflowEvent(
                type="error",
                message=f"How a {self.title.lower()} is done could not be read; nothing was drafted.",
            )
            return
        yield WorkflowEvent(
            type="step_done",
            step_id="context",
            summary=f"Loaded profile for {profile.name or 'company'}.",
        )

        title = self.steps()[1].title
        yield WorkflowEvent(type="step_start", step_id="draft", step_title=title)
        query = (
            f"Write a {self.title} for this request.\n\n"
            f"Request:\n{inputs.request}\n\n"
            "Output one finished Markdown document that starts with a '# ' title. "
            "Use only facts from the request, the company context and the "
            "knowledge given; where something needed is missing, mark it "
            "[to confirm] rather than inventing it."
        ) + playbook_clause(method, "Follow this method")
        content = await route_to_specialist(
            specialist_name=self.specialist,
            query=query,
            context="" if profile.is_empty() else profile.to_prompt_block(),
            retrieved_knowledge=rag,
        )
        if not content.strip():
            yield WorkflowEvent(type="error", message="The draft came back empty.")
            return
        if not content.lstrip().startswith("#"):
            content = f"# {self.title}\n\n{content.strip()}"
        yield WorkflowEvent(
            type="step_done", step_id="draft", summary=f"Drafted {len(content)} characters."
        )
        yield WorkflowEvent(type="artifact", content=content)


def quick_workflows(strict: bool = False) -> list[QuickWorkflow]:
    """A Quick workflow for every playbook in effect that no built-in or
    custom workflow follows (switched-off custom ones count as following).

    Best-effort by default: a playbook library or custom-workflow store that
    can't be read yields no Quick workflows rather than failing the catalog.
    `strict=True` raises instead.
    """
    from openexecutive.knowledge.skills_repo import list_skills
    from openexecutive.workflows.playbooks import _following_workflows

    try:
        others = _following_workflows(strict=True)
        skills = list_skills()
    except Exception:
        if strict:
            raise
        logger.exception("Could not work out the Quick workflows; offering none")
        return []
    followed = {name for wf in others for name in wf.followed_playbooks()}
    # A custom workflow saved under a `quick_` name before the prefix was
    # reserved keeps that name: no Quick workflow shadows it.
    taken = {wf.name for wf in others}
    candidates = [
        w
        for w in (QuickWorkflow(s) for s in skills if s.frontmatter.name not in followed)
        if w.name not in taken
    ]
    # Distinct playbook names can map to one Quick name (`market-sizing`,
    # `market_sizing`, `Market-Sizing`). Offer none of them rather than let
    # sort order decide which method a name runs.
    counts = Counter(w.name for w in candidates)
    for name, n in counts.items():
        if n > 1:
            logger.warning("%d playbooks map to the Quick workflow %r; offering none", n, name)
    return [w for w in candidates if counts[w.name] == 1]


def get_quick_workflow(name: str) -> QuickWorkflow | None:
    """The Quick workflow called `name`, or None."""
    if not name.startswith(QUICK_PREFIX):
        return None
    return next((w for w in quick_workflows() if w.name == name), None)
