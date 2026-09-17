"""Exec search brief workflow — produces a hiring brief / scorecard for
an executive search: role profile, must-haves vs. nice-to-haves, ideal-
candidate sourcing thesis, interview plan, and deal-breakers.

Uses the CHRO specialist primarily, with input from a function-relevant
specialist when the user specifies one.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.knowledge.retriever import retrieve
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding.profile_builder import load_or_create_profile
from openexecutive.orchestrator.router import route_to_specialist
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowSection,
    WorkflowStepDef,
)

# Map of human-friendly function names → specialist registry keys.
_FUNCTION_TO_SPECIALIST = {
    "engineering": "cpo",  # closest available — no CTO specialist
    "product": "cpo",
    "marketing": "cmo",
    "sales": "cmo",  # closest available — no CRO specialist
    "operations": "coo",
    "finance": "cfo",
    "strategy": "cso",
    "legal": "gc",
    "people": "chro",
}

_ES_EXAMPLE_ROLE = "VP Engineering"
_ES_EXAMPLE_FUNCTION = "engineering"
_ES_EXAMPLE_BUSINESS = (
    "Company at 38 people, $4M ARR, raising Series B in Q3. Current "
    "engineering team is 12 ICs flat to a VP we're transitioning out. "
    "Need to ship workspaces GA in July and stand up the platform tier "
    "by year-end while scaling team to 22 by mid-next-year."
)
_ES_EXAMPLE_OUTCOMES = (
    "Year 1 outcomes the new VP must own:\n"
    "1. Hire and integrate 8+ engineers (mix of senior ICs and 2 directors)\n"
    "2. Ship platform tier on time (Q4) without breaking workspaces velocity\n"
    "3. Reduce p99 latency 30%+ through platform investments\n"
    "4. Establish performance / promotion processes — none exist today"
)
_ES_EXAMPLE_NON_NEGOTIABLES = (
    "Required: 5+ years managing 20+ engineers, has shipped a horizontally-"
    "scaling data product, is in a US time zone, has built and integrated "
    "two manager hires before. Comp band $300-350K + equity."
)


class ExecSearchBriefInput(BaseModel):
    """Inputs for the Exec Search Brief workflow."""

    role_title: str = Field(
        ...,
        description="Role title we're hiring for (e.g., 'VP Engineering').",
        min_length=3,
        examples=[_ES_EXAMPLE_ROLE],
    )
    function: str = Field(
        ...,
        description=(
            "Function this role sits in. Used to pull the right specialist "
            "into the brief. One of: engineering, product, marketing, sales, "
            "operations, finance, strategy, legal, people."
        ),
        min_length=3,
        examples=[_ES_EXAMPLE_FUNCTION],
    )
    business_context: str = Field(
        ...,
        description=(
            "Where the business is and where it's going — stage, headcount, "
            "growth rate, big bets the new hire shapes."
        ),
        min_length=30,
        examples=[_ES_EXAMPLE_BUSINESS],
    )
    year_one_outcomes: str = Field(
        ...,
        description=(
            "The 3-5 outcomes the new hire MUST own in year one. Force "
            "yourself to be specific — vague mandates hire vague execs."
        ),
        min_length=30,
        examples=[_ES_EXAMPLE_OUTCOMES],
    )
    non_negotiables: str = Field(
        default="",
        description=(
            "Optional. Hard requirements: years of experience, scale managed, "
            "specific domain depth, comp band, location."
        ),
        examples=[_ES_EXAMPLE_NON_NEGOTIABLES],
    )


class ExecSearchBriefWorkflow(Workflow):
    name = "exec_search_brief"
    title = "Exec Search Brief"
    description = (
        "Drafts a hiring brief and scorecard for an executive search — "
        "role profile, year-one outcomes, sourcing thesis, interview "
        "plan, and deal-breakers. Uses the CHRO specialist plus the "
        "function-specific specialist."
    )
    section = WorkflowSection.PEOPLE
    estimated_minutes = 4

    def input_model(self) -> type[BaseModel]:
        return ExecSearchBriefInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "role_title": _ES_EXAMPLE_ROLE,
            "function": _ES_EXAMPLE_FUNCTION,
            "business_context": _ES_EXAMPLE_BUSINESS,
            "year_one_outcomes": _ES_EXAMPLE_OUTCOMES,
            "non_negotiables": _ES_EXAMPLE_NON_NEGOTIABLES,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile and people/leadership RAG.",
            ),
            WorkflowStepDef(
                id="profile",
                title="Role profile & scorecard",
                description="The must-haves vs. nice-to-haves the search will be tested against.",
            ),
            WorkflowStepDef(
                id="function_view",
                title="Function-specific lens",
                description="What the function specialist would interview for.",
            ),
            WorkflowStepDef(
                id="sourcing",
                title="Sourcing thesis",
                description="Where the candidate likely is today, and how to reach them.",
            ),
            WorkflowStepDef(
                id="interview",
                title="Interview plan",
                description="The interview loop, ordered, with what each step is testing.",
            ),
            WorkflowStepDef(
                id="dealbreakers",
                title="Deal-breakers",
                description="Patterns that should kill a finalist late in the process.",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble brief",
                description="Combine into one Markdown brief.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, ExecSearchBriefInput)
        ctx = _ESCtx(inputs)

        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        ctx.profile = load_or_create_profile()
        ctx.rag = retrieve(
            query=f"executive hiring scorecard interview {ctx.inputs.role_title}",
            specialist_name="chro",
            n_builtin=5,
            n_company=3,
            store=store,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="context",
            summary=(
                f"Loaded profile for {ctx.profile.name or 'company'} and "
                f"{_chunk_count(ctx.rag)} RAG chunks."
            ),
        )

        yield WorkflowEvent(
            type="step_start", step_id="profile", step_title="Role profile & scorecard"
        )
        ctx.profile_section = await route_to_specialist(
            specialist_name="chro",
            query=_build_profile_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="profile", summary=_first_line(ctx.profile_section)
        )

        yield WorkflowEvent(
            type="step_start", step_id="function_view", step_title="Function-specific lens"
        )
        function_specialist = _resolve_function_specialist(ctx.inputs.function)
        ctx.function_view = await route_to_specialist(
            specialist_name=function_specialist,
            query=_build_function_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"executive {ctx.inputs.function} leadership skills evaluation",
                specialist_name=function_specialist,
                n_builtin=4,
                n_company=2,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done", step_id="function_view", summary=_first_line(ctx.function_view)
        )

        yield WorkflowEvent(
            type="step_start", step_id="sourcing", step_title="Sourcing thesis"
        )
        ctx.sourcing = await route_to_specialist(
            specialist_name="chro",
            query=_build_sourcing_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="sourcing", summary=_first_line(ctx.sourcing)
        )

        yield WorkflowEvent(
            type="step_start", step_id="interview", step_title="Interview plan"
        )
        ctx.interview = await route_to_specialist(
            specialist_name="chro",
            query=_build_interview_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="interview", summary=_first_line(ctx.interview)
        )

        yield WorkflowEvent(
            type="step_start", step_id="dealbreakers", step_title="Deal-breakers"
        )
        ctx.dealbreakers = await route_to_specialist(
            specialist_name="chro",
            query=_build_dealbreakers_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge="",
        )
        yield WorkflowEvent(
            type="step_done", step_id="dealbreakers", summary=_first_line(ctx.dealbreakers)
        )

        yield WorkflowEvent(
            type="step_start", step_id="assemble", step_title="Assemble brief"
        )
        artifact = _assemble_brief(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across 5 sections.",
        )
        yield WorkflowEvent(type="artifact", content=artifact)


class _ESCtx:
    def __init__(self, inputs: ExecSearchBriefInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.rag: str = ""
        self.profile_section: str = ""
        self.function_view: str = ""
        self.sourcing: str = ""
        self.interview: str = ""
        self.dealbreakers: str = ""


def _company_context_block(profile: CompanyProfile | None) -> str:
    if profile is None or profile.is_empty():
        return ""
    return profile.to_prompt_block()


def _chunk_count(rag_string: str) -> int:
    return rag_string.count("\n[") if rag_string else 0


def _first_line(text: str, max_len: int = 140) -> str:
    if not text:
        return "(empty)"
    line = text.strip().split("\n", 1)[0].lstrip("# ").strip()
    return line[: max_len - 1] + "…" if len(line) > max_len else line


def _resolve_function_specialist(function: str) -> str:
    """Map a free-text function name to a specialist registry key.

    Falls back to CHRO if the function is unrecognized — better to get a
    generic-but-thoughtful section than to crash the workflow.
    """
    key = function.strip().lower()
    return _FUNCTION_TO_SPECIALIST.get(key, "chro")


def _build_profile_prompt(ctx: _ESCtx) -> str:
    non_neg_block = (
        f"\n\nNon-negotiables:\n{ctx.inputs.non_negotiables}"
        if ctx.inputs.non_negotiables.strip()
        else ""
    )
    return (
        f"Draft the **Role Profile and Scorecard** for the "
        f"{ctx.inputs.role_title} search.\n\n"
        f"Business context:\n{ctx.inputs.business_context}\n\n"
        f"Year-one outcomes:\n{ctx.inputs.year_one_outcomes}{non_neg_block}\n\n"
        "Output (Markdown):\n"
        "## Role Profile & Scorecard\n\n"
        "### One-paragraph role thesis\n"
        "What this hire actually has to be to deliver the year-one outcomes. "
        "Avoid 'we want a rockstar' framing.\n\n"
        "### Must-haves\n"
        "5-7 bullets. Specific: scale managed, domain depth, situations "
        "they've navigated. For each, write the evidence we'd expect "
        "from the candidate's CV / past work.\n\n"
        "### Nice-to-haves\n"
        "3-5 bullets. Things that would make a strong hire stronger but "
        "we won't pass on absence.\n\n"
        "### Scorecard\n"
        "Bullet list of 5-7 dimensions (e.g., 'has scaled an org from 10 "
        "to 30 engineers', 'has integrated 2+ external manager hires'). "
        "For each: how we score 1-5 from the loop.\n\n"
        "Discipline: must-haves and nice-to-haves must be drawn from the "
        "year-one outcomes, not from a generic role template."
    )


def _build_function_prompt(ctx: _ESCtx) -> str:
    return (
        f"You're the functional leader closest to the {ctx.inputs.role_title} "
        f"role we're hiring for in our {ctx.inputs.function} function. Draft "
        "the **Function-Specific Lens** — what you specifically would test "
        "for in the interview process that the general CHRO scorecard "
        "wouldn't catch.\n\n"
        f"Business context:\n{ctx.inputs.business_context}\n\n"
        f"Year-one outcomes:\n{ctx.inputs.year_one_outcomes}\n\n"
        "Output (Markdown):\n"
        "## Function-Specific Lens\n\n"
        "### The 3-5 hardest decisions this hire will make in year one\n"
        "Bullet list. For each: the decision, why it's hard, and what "
        "kind of judgment would tell us they're qualified.\n\n"
        "### Domain depth questions\n"
        "5-7 specific technical / functional questions you'd ask. Real "
        "questions, not 'tell me about your management style'.\n\n"
        "### Red-flag patterns\n"
        "3-4 candidate-behavior patterns that, in your domain, indicate "
        "this person looks the part but won't deliver.\n\n"
        "Discipline: write as the function leader, not as HR. Use "
        "domain vocabulary."
    )


def _build_sourcing_prompt(ctx: _ESCtx) -> str:
    return (
        f"Draft the **Sourcing Thesis** for the {ctx.inputs.role_title} "
        "search. Where is this candidate today, and how do we get in "
        "front of them?\n\n"
        f"Business context:\n{ctx.inputs.business_context}\n\n"
        f"Must-haves implied by year-one outcomes:\n{ctx.inputs.year_one_outcomes}\n\n"
        "Output (Markdown):\n"
        "## Sourcing Thesis\n\n"
        "### Ideal candidate background\n"
        "1 paragraph: the kind of company / stage / function they're in "
        "today, and the trigger that would make them move (e.g., 'senior "
        "leader at a stage-too-late company who's blocked from the next "
        "promotion').\n\n"
        "### Companies / talent pools to target\n"
        "Bullet list of 5-10 specific company types (and where possible, "
        "named companies) where this candidate is likely to be. Don't "
        "just say 'unicorn startups'.\n\n"
        "### Outreach approach\n"
        "1 paragraph: how we should reach this candidate, who from our "
        "side, and what the pitch is.\n\n"
        "### Hiring against the comp band\n"
        "1 paragraph: realistic assessment of whether our comp will pull "
        "the level we need. If not, what we trade on (equity, mission, "
        "scope, founder access)."
    )


def _build_interview_prompt(ctx: _ESCtx) -> str:
    return (
        "Design the **Interview Plan** for this exec search. The loop must "
        "actually test the must-haves, not just feel rigorous.\n\n"
        f"Role: {ctx.inputs.role_title}\n"
        f"Year-one outcomes:\n{ctx.inputs.year_one_outcomes}\n\n"
        "Output (Markdown):\n"
        "## Interview Plan\n\n"
        "### Loop overview\n"
        "Numbered list of 5-7 interview rounds in order, each with:\n"
        "- **Round:** [name]\n"
        "- **Interviewer:** [role, e.g., 'founder' / 'function head' / 'CHRO']\n"
        "- **What it tests:** the must-have / scorecard dimension\n"
        "- **Length:** minutes\n\n"
        "### Working session\n"
        "1 paragraph: the deliverable-based working session we'd run with "
        "finalists. Be specific about what they produce and how we evaluate it.\n\n"
        "### Reference check rubric\n"
        "5-7 questions to ask every reference. For each: what answer would "
        "concern us."
    )


def _build_dealbreakers_prompt(ctx: _ESCtx) -> str:
    return (
        "List the **Deal-Breakers** — patterns that should kill a finalist "
        "no matter how good the rest of the loop looked. The more honest "
        "this list, the fewer post-hire regrets.\n\n"
        f"Role: {ctx.inputs.role_title}\n"
        f"Year-one outcomes:\n{ctx.inputs.year_one_outcomes}\n\n"
        "Output (Markdown):\n"
        "## Deal-Breakers\n\n"
        "Bullet list of 4-6 specific deal-breakers. Each:\n"
        "- **[Short name]:** the behavior or pattern, how we'd notice it "
        "in the loop, and why we walk away.\n\n"
        "Examples (template, not literal): 'cannot name a time they fired "
        "someone they liked', 'blamed every past hard year on external "
        "factors', 'cannot articulate the technical trade-offs in their "
        "last 2 architecture decisions'.\n\n"
        "Discipline: deal-breakers must be observable in the loop. "
        "'Lacks integrity' is not a deal-breaker; it's a hope."
    )


def _assemble_brief(ctx: _ESCtx) -> str:
    parts = [
        f"# Exec Search Brief — {ctx.inputs.role_title}",
        "",
        "## Business Context",
        "",
        ctx.inputs.business_context.strip(),
        "",
        _ensure_heading(ctx.profile_section, "## Role Profile & Scorecard"),
        "",
        _ensure_heading(ctx.function_view, "## Function-Specific Lens"),
        "",
        _ensure_heading(ctx.sourcing, "## Sourcing Thesis"),
        "",
        _ensure_heading(ctx.interview, "## Interview Plan"),
        "",
        _ensure_heading(ctx.dealbreakers, "## Deal-Breakers"),
        "",
        "---",
        "*Drafted by Open Executive. Review with the hiring manager and "
        "the executive search partner before kicking off the search.*",
    ]
    return "\n".join(parts).strip() + "\n"


def _ensure_heading(text: str, expected_heading: str) -> str:
    stripped = text.strip()
    if not stripped:
        return f"{expected_heading}\n\n_(No content generated for this section.)_"
    first_line = stripped.split("\n", 1)[0]
    if first_line.startswith("##"):
        return stripped
    return f"{expected_heading}\n\n{stripped}"
