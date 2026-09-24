"""Quarterly plan workflow — produces a complete quarterly operating plan
(strategic objectives + OKRs + financial frame + per-function priorities +
risks) as a single Markdown artifact.

Uses the CSO and CFO specialists, grounded by the `quarterly-okr-set` and
`quarterly-forecast` skills plus domain RAG.
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
from openexecutive.workflows.playbooks import load_playbook, playbook_clause

_QP_EXAMPLE_RECAP = (
    "- Shipped v2 reporting, on time; adoption ahead of plan\n"
    "- Mid-market motion launched but pipeline coverage stuck at 2x (target 3x)\n"
    "- Slipped: the self-serve onboarding rebuild — moved to this quarter\n"
    "- Lost 2 finalist AE candidates over base comp; band was raised in March\n"
    "- Cleared SOC 2 Type II, unlocking 3 stalled enterprise deals"
)
_QP_EXAMPLE_PRIORITIES = (
    "1. Land 5 mid-market customers (>$50K ACV) by quarter end\n"
    "2. Ship self-serve onboarding so PLG signups can reach 'aha' without sales\n"
    "3. Cut activation drop-off from day 1 to day 7 below 40% (currently 58%)\n"
    "4. Hire 2 AEs + 1 staff backend engineer\n"
    "5. Reach 60 NPS among accounts >6mo old (currently 47)"
)
_QP_EXAMPLE_BETS = (
    "1. Mid-market motion — if 5 wins land, we have proof for a Series B narrative\n"
    "2. Self-serve onboarding — if activation reaches benchmark, CAC drops 30%+"
)
_QP_EXAMPLE_CONSTRAINTS = (
    "- Engineering capacity capped at 3 backend FTEs through July\n"
    "- Marketing budget held flat vs. last quarter\n"
    "- No additional fundraising until Q4"
)
_QP_EXAMPLE_REVENUE_CTX = (
    "Current ARR $4.1M, last quarter +18% QoQ. Board-committed FY end target $7M. "
    "$9.2M cash, 14mo runway at current burn."
)


class QuarterlyPlanInput(BaseModel):
    """Inputs for the Quarterly Plan workflow."""

    quarter_label: str = Field(
        ...,
        description="Quarter being planned (e.g., 'Q3 2026')",
        examples=["Q3 2026"],
    )
    prior_quarter_recap: str = Field(
        ...,
        description=(
            "What happened last quarter — what shipped, what slipped, what "
            "carried over, key wins and losses. Bullets are fine."
        ),
        min_length=20,
        examples=[_QP_EXAMPLE_RECAP],
    )
    top_strategic_priorities: str = Field(
        ...,
        description=(
            "The 3-5 things the company is most focused on this quarter. "
            "Be specific — 'grow revenue' is not a priority, 'land 5 "
            "mid-market customers' is."
        ),
        min_length=20,
        examples=[_QP_EXAMPLE_PRIORITIES],
    )
    biggest_bets: str = Field(
        ...,
        description=(
            "The 1-2 strategic moves that, if they work, change the trajectory "
            "of the company (e.g., 'enter the enterprise segment', 'launch the "
            "platform tier', 'restructure GTM motion')."
        ),
        min_length=15,
        examples=[_QP_EXAMPLE_BETS],
    )
    known_constraints: str = Field(
        default="",
        description=(
            "Optional. Capacity limits, hiring constraints, dependencies on "
            "other teams, budget caps. Helps make the plan realistic."
        ),
        examples=[_QP_EXAMPLE_CONSTRAINTS],
    )
    revenue_target_context: str = Field(
        default="",
        description=(
            "Optional. Current ARR, last quarter growth rate, board-committed "
            "targets, runway. Gives the CFO context for the financial frame."
        ),
        examples=[_QP_EXAMPLE_REVENUE_CTX],
    )


class QuarterlyPlanWorkflow(Workflow):
    name = "quarterly_plan"
    title = "Quarterly Operating Plan"
    description = (
        "Drafts a complete quarterly plan — strategic objectives with OKRs, "
        "financial frame (revenue + hiring + budget), per-function priorities, "
        "and a risk register. Uses the strategy and finance specialists."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 4
    playbooks = ("quarterly-okr-set", "quarterly-forecast")

    def input_model(self) -> type[BaseModel]:
        return QuarterlyPlanInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "quarter_label": "Q3 2026",
            "prior_quarter_recap": _QP_EXAMPLE_RECAP,
            "top_strategic_priorities": _QP_EXAMPLE_PRIORITIES,
            "biggest_bets": _QP_EXAMPLE_BETS,
            "known_constraints": _QP_EXAMPLE_CONSTRAINTS,
            "revenue_target_context": _QP_EXAMPLE_REVENUE_CTX,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile, OKR + forecasting playbooks, and relevant knowledge.",
            ),
            WorkflowStepDef(
                id="okrs",
                title="Draft company OKRs",
                description="3-5 Objectives with 3-5 Key Results each, grounded in the strategic priorities.",
            ),
            WorkflowStepDef(
                id="financial_frame",
                title="Draft financial frame",
                description="Revenue target, hiring plan, budget envelope for the quarter.",
            ),
            WorkflowStepDef(
                id="operating_priorities",
                title="Draft per-function priorities",
                description="What each major function (engineering, GTM, ops) should focus on.",
            ),
            WorkflowStepDef(
                id="risks_and_mitigations",
                title="Draft risks and mitigations",
                description="The 3-5 biggest things that could break the plan, with owners.",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble final plan",
                description="Combine all sections into a structured Markdown plan document.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, QuarterlyPlanInput)
        ctx = _QuarterlyPlanContext(inputs)

        # --- Step 1: context ---
        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        ctx.profile = load_or_create_profile()
        ctx.okr_skill = load_playbook("quarterly-okr-set")
        ctx.forecast_skill = load_playbook("quarterly-forecast")
        ctx.rag = retrieve(
            query=f"quarterly planning OKRs strategic priorities {ctx.inputs.quarter_label}",
            specialist_name="cso",
            n_builtin=5,
            n_company=3,
            store=store,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="context",
            summary=(
                f"Loaded profile for {ctx.profile.name or 'company'}, OKR + "
                f"forecast skills, and {_chunk_count(ctx.rag)} RAG chunks."
            ),
        )

        # --- Step 2: OKRs ---
        yield WorkflowEvent(
            type="step_start", step_id="okrs", step_title="Draft company OKRs"
        )
        ctx.okrs = await route_to_specialist(
            specialist_name="cso",
            query=_build_okrs_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="okrs", summary=_first_line(ctx.okrs)
        )

        # --- Step 3: financial frame ---
        yield WorkflowEvent(
            type="step_start", step_id="financial_frame", step_title="Draft financial frame"
        )
        ctx.financial_frame = await route_to_specialist(
            specialist_name="cfo",
            query=_build_financial_frame_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"quarterly financial forecast hiring plan budget {ctx.inputs.quarter_label}",
                specialist_name="cfo",
                n_builtin=4,
                n_company=3,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="financial_frame",
            summary=_first_line(ctx.financial_frame),
        )

        # --- Step 4: per-function priorities ---
        yield WorkflowEvent(
            type="step_start",
            step_id="operating_priorities",
            step_title="Draft per-function priorities",
        )
        ctx.operating_priorities = await route_to_specialist(
            specialist_name="cso",
            query=_build_operating_priorities_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="operating_priorities",
            summary=_first_line(ctx.operating_priorities),
        )

        # --- Step 5: risks ---
        yield WorkflowEvent(
            type="step_start",
            step_id="risks_and_mitigations",
            step_title="Draft risks and mitigations",
        )
        ctx.risks = await route_to_specialist(
            specialist_name="cso",
            query=_build_risks_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="risks_and_mitigations", summary=_first_line(ctx.risks)
        )

        # --- Step 6: assemble ---
        yield WorkflowEvent(type="step_start", step_id="assemble", step_title="Assemble final plan")
        artifact = _assemble_plan(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across 5 sections.",
        )
        yield WorkflowEvent(type="artifact", content=artifact)


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


class _QuarterlyPlanContext:
    def __init__(self, inputs: QuarterlyPlanInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.okr_skill: str = ""
        self.forecast_skill: str = ""
        self.rag: str = ""
        self.okrs: str = ""
        self.financial_frame: str = ""
        self.operating_priorities: str = ""
        self.risks: str = ""


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


def _build_okrs_prompt(ctx: _QuarterlyPlanContext) -> str:
    skill_clause = playbook_clause(ctx.okr_skill, "Follow this playbook for OKR setting")
    constraints = (
        f"\n\nKnown constraints:\n{ctx.inputs.known_constraints}"
        if ctx.inputs.known_constraints.strip()
        else ""
    )
    return (
        f"Draft the **Company OKRs** section of the {ctx.inputs.quarter_label} "
        "quarterly plan.\n\n"
        f"Last quarter recap:\n{ctx.inputs.prior_quarter_recap}\n\n"
        f"Top strategic priorities this quarter:\n{ctx.inputs.top_strategic_priorities}\n\n"
        f"Biggest bets:\n{ctx.inputs.biggest_bets}"
        f"{constraints}\n\n"
        "Output (Markdown), exactly this structure:\n"
        "## Company OKRs\n\n"
        "For each Objective (3-5 total):\n"
        "### Objective N: [Qualitative inspirational title]\n"
        "_Why it matters this quarter:_ 1-2 sentences linking to strategy.\n"
        "**Key Results:**\n"
        "- KR N.1: [measurable outcome with specific number and deadline]\n"
        "- KR N.2: ...\n"
        "(3-5 KRs per Objective; each with a named owner placeholder like "
        "'[owner: <role>]' if not specified in inputs)\n\n"
        "Discipline:\n"
        "- 3-5 Objectives max. Force trade-offs.\n"
        "- KRs are outcomes, not tasks. 'Launched X' is not a KR.\n"
        "- Target 70% achievement, not 100%. Raise the bar.\n"
        "- Do NOT invent specific numbers (revenue targets, customer counts) "
        "that weren't in the inputs. Use placeholders like '[target: TBD]' "
        "where a number is needed but not provided."
        f"{skill_clause}"
    )


def _build_financial_frame_prompt(ctx: _QuarterlyPlanContext) -> str:
    skill_clause = playbook_clause(ctx.forecast_skill, "Reference this forecasting playbook")
    revenue_context = (
        f"\n\nRevenue context:\n{ctx.inputs.revenue_target_context}"
        if ctx.inputs.revenue_target_context.strip()
        else ""
    )
    return (
        f"Draft the **Financial Frame** section of the {ctx.inputs.quarter_label} "
        "quarterly plan. This section sets the financial boundary conditions "
        "the rest of the plan must operate within.\n\n"
        f"Strategic priorities:\n{ctx.inputs.top_strategic_priorities}\n\n"
        f"Biggest bets:\n{ctx.inputs.biggest_bets}"
        f"{revenue_context}\n\n"
        "Output (Markdown), exactly this structure:\n"
        "## Financial Frame\n\n"
        "### Revenue Target\n"
        "1-2 paragraphs. State the new ARR target for the quarter (or "
        "placeholder '[target: TBD with CFO]'). Connect it to the strategic "
        "priorities.\n\n"
        "### Hiring Plan\n"
        "Bullet list of roles being hired this quarter, with rough start "
        "timing. If the inputs don't specify roles, suggest 3-5 roles "
        "consistent with the strategic priorities and flag as '[to be confirmed]'.\n\n"
        "### Budget Envelope\n"
        "Prose paragraph on operating budget assumptions — burn rate, "
        "one-time spend, marketing investment. Use placeholders where "
        "numbers aren't provided.\n\n"
        "### Cash Position and Runway\n"
        "1 paragraph on starting cash, projected burn, ending runway. "
        "Use placeholders for unknowns.\n\n"
        "Do NOT invent dollar amounts. Use '[X]' placeholders if a number "
        "is needed but wasn't provided. The CFO will fill them in."
        f"{skill_clause}"
    )


def _build_operating_priorities_prompt(ctx: _QuarterlyPlanContext) -> str:
    return (
        f"Draft the **Operating Priorities** section of the {ctx.inputs.quarter_label} "
        "quarterly plan. For each major function, state what they should "
        "focus on this quarter — derived from the company OKRs and strategic bets.\n\n"
        f"Strategic priorities:\n{ctx.inputs.top_strategic_priorities}\n\n"
        f"Biggest bets:\n{ctx.inputs.biggest_bets}\n\n"
        "Output (Markdown):\n"
        "## Operating Priorities\n\n"
        "Cover the functions relevant to the company stage and priorities — "
        "typically Engineering, Product, GTM (Sales + Marketing + CS), and "
        "Operations. For each:\n\n"
        "### [Function name]\n"
        "**Top priorities:**\n"
        "- 2-4 specific things this function should focus on\n"
        "**Explicit NOT priorities:**\n"
        "- 1-2 things this function should NOT do this quarter (to free up capacity)\n"
        "**Owner:** [function head name or placeholder]\n\n"
        "Discipline:\n"
        "- Operating priorities must trace back to the company OKRs.\n"
        "- Force explicit 'not priorities' — the best plans say no to things.\n"
        "- Keep it concrete; avoid generic statements like 'execute well'."
    )


def _build_risks_prompt(ctx: _QuarterlyPlanContext) -> str:
    return (
        f"Draft the **Risks and Mitigations** section of the {ctx.inputs.quarter_label} "
        "quarterly plan. The 3-5 things most likely to break the plan, "
        "with how we'd see them and what we'd do.\n\n"
        f"Strategic priorities:\n{ctx.inputs.top_strategic_priorities}\n\n"
        f"Biggest bets:\n{ctx.inputs.biggest_bets}\n\n"
        f"Last quarter recap:\n{ctx.inputs.prior_quarter_recap}\n\n"
        "Output (Markdown):\n"
        "## Risks and Mitigations\n\n"
        "For each of 3-5 risks:\n"
        "### Risk N: [Short name]\n"
        "- **What could happen:** 1-2 sentences\n"
        "- **Probability:** High / Medium / Low\n"
        "- **Impact if it happens:** 1 sentence\n"
        "- **Leading indicator:** what we'd watch to see it forming\n"
        "- **Mitigation:** what we'd do, with owner placeholder\n\n"
        "Discipline:\n"
        "- Real risks, not hypotheticals. Things the team would actually worry about.\n"
        "- Internal risks (execution, hiring, dependencies) AND external "
        "(competitor moves, market, regulatory).\n"
        "- Each risk needs a leading indicator — something measurable that "
        "would tell us it's becoming real."
    )


def _assemble_plan(ctx: _QuarterlyPlanContext) -> str:
    parts: list[str] = [
        f"# Quarterly Operating Plan — {ctx.inputs.quarter_label}",
        "",
        "## Recap From Last Quarter",
        "",
        ctx.inputs.prior_quarter_recap.strip(),
        "",
        _ensure_heading(ctx.okrs, "## Company OKRs"),
        "",
        _ensure_heading(ctx.financial_frame, "## Financial Frame"),
        "",
        _ensure_heading(ctx.operating_priorities, "## Operating Priorities"),
        "",
        _ensure_heading(ctx.risks, "## Risks and Mitigations"),
        "",
        "---",
        "*Drafted by Open Executive. Review with the leadership team and "
        "validate every '[placeholder]' before circulating.*",
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
