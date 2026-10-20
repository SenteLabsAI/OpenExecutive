"""Board prep workflow — produces a full board-meeting deck as Markdown.

Inputs come from the user (a structured form). The workflow orchestrates
the Board Comms, CFO, and CSO specialists in a defined sequence, grounded
by the `board-prep-deck` playbook and relevant RAG, and assembles the
sections into a single artifact.
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

_BP_EXAMPLE_METRICS = (
    "- ARR ended at $4.1M (+18% QoQ, plan was $4.3M; slipped 4%)\n"
    "- NRR 112% (plan 110%)\n"
    "- New logos: 11 (plan 14)\n"
    "- Cash $9.2M, ~14mo runway at current $660K/mo burn\n"
    "- Headcount 38 (plan 40; two backfills slipped to next quarter)"
)
_BP_EXAMPLE_WINS = (
    "- Closed Acme Corp ($240K ACV) — our largest deal to date\n"
    "- Shipped the v2 reporting suite on time; 60% of paid accounts using it within 3 weeks\n"
    "- Hired Priya as VP Engineering (starts next month)\n"
    "- Cleared SOC 2 Type II audit"
)
_BP_EXAMPLE_CHALLENGES = (
    "- Mid-market pipeline coverage at 2.1x for next quarter vs. 3x target\n"
    "- Churn ticked up to 1.8% (was 1.2%); concentrated in <50-seat accounts\n"
    "- Two finalists declined the AE role over comp band\n"
    "- Platform incident on April 18 caused 90 minutes of write-path downtime"
)
_BP_EXAMPLE_DEEP_DIVE_1 = (
    "Whether to raise a Series B in Q3 2026 at the current growth rate, or "
    "extend runway 6 months and raise in Q1 2027 once we have a second "
    "quarter of mid-market traction to anchor valuation."
)
_BP_EXAMPLE_DEEP_DIVE_2 = (
    "Whether to add a usage-based pricing tier on top of seat-based pricing "
    "to capture more revenue from heavy-usage accounts."
)
_BP_EXAMPLE_DECISIONS = (
    "- Refresh equity grants for the engineering team (proposal attached)\n"
    "- Approve the renewed lease at our SF office (3-year, $42K/mo)"
)


class BoardPrepInput(BaseModel):
    """Inputs for the Board Prep workflow.

    Field descriptions show in the UI form via JSON Schema.
    """

    quarter_label: str = Field(
        ...,
        description="Quarter / period label (e.g., 'Q2 2026')",
        examples=["Q2 2026"],
    )
    meeting_date: str = Field(
        ...,
        description="Date of the board meeting (any human format)",
        examples=["June 15, 2026"],
    )
    headline_metrics: str = Field(
        ...,
        description=(
            "Free-text summary of the key metrics for the period and "
            "variances vs. plan (revenue, ARR, NRR, cash, runway, etc.). "
            "Bullets are fine."
        ),
        min_length=10,
        examples=[_BP_EXAMPLE_METRICS],
    )
    wins: str = Field(
        ...,
        description="2-5 specific wins this period (customers, milestones, hires).",
        min_length=10,
        examples=[_BP_EXAMPLE_WINS],
    )
    challenges: str = Field(
        ...,
        description="2-5 specific things that didn't go well or are at risk.",
        min_length=10,
        examples=[_BP_EXAMPLE_CHALLENGES],
    )
    deep_dive_topic_1: str = Field(
        ...,
        description=(
            "The first strategic topic for board deep-dive (e.g., 'whether to "
            "raise a Series B in Q3 or extend runway and raise in Q1 2027')."
        ),
        min_length=10,
        examples=[_BP_EXAMPLE_DEEP_DIVE_1],
    )
    deep_dive_topic_2: str = Field(
        default="",
        description="Optional second deep-dive topic.",
        examples=[_BP_EXAMPLE_DEEP_DIVE_2],
    )
    decisions_needed: str = Field(
        default="",
        description=(
            "Optional list of items requiring formal board approval this "
            "meeting (equity grants, contracts, financing, etc.)."
        ),
        examples=[_BP_EXAMPLE_DECISIONS],
    )


class BoardPrepWorkflow(Workflow):
    name = "board_prep"
    title = "Board Prep Deck"
    description = (
        "Drafts a complete board meeting deck — executive summary, business "
        "update, two strategic deep-dives, and decisions — using the board "
        "comms, finance, and strategy specialists. Output is Markdown."
    )
    section = WorkflowSection.BOARD
    estimated_minutes = 4
    playbooks = ("board-prep-deck",)

    def input_model(self) -> type[BaseModel]:
        return BoardPrepInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "quarter_label": "Q2 2026",
            "meeting_date": "June 15, 2026",
            "headline_metrics": _BP_EXAMPLE_METRICS,
            "wins": _BP_EXAMPLE_WINS,
            "challenges": _BP_EXAMPLE_CHALLENGES,
            "deep_dive_topic_1": _BP_EXAMPLE_DEEP_DIVE_1,
            "deep_dive_topic_2": _BP_EXAMPLE_DEEP_DIVE_2,
            "decisions_needed": _BP_EXAMPLE_DECISIONS,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile, the board-prep playbook, and relevant knowledge.",
            ),
            WorkflowStepDef(
                id="exec_summary",
                title="Draft executive summary",
                description="The CEO's POV on the state of the business — 1 page.",
            ),
            WorkflowStepDef(
                id="business_update",
                title="Draft business update",
                description="Financials vs. plan, KPI movements, customer / commercial / people.",
            ),
            WorkflowStepDef(
                id="deep_dive_1",
                title="Draft deep-dive section 1",
                description="Strategic topic with options, recommendation, trade-offs.",
            ),
            WorkflowStepDef(
                id="deep_dive_2",
                title="Draft deep-dive section 2",
                description="Second strategic topic (skipped if not provided).",
            ),
            WorkflowStepDef(
                id="decisions",
                title="Draft decisions section",
                description="Items requiring formal board approval (skipped if none).",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble final deck",
                description="Combine all sections into a structured Markdown deck.",
            ),
        ]

    async def run(  # noqa: C901 — orchestration sequence is intentionally linear
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, BoardPrepInput)
        ctx = _BoardPrepContext(inputs)

        # --- Step 1: load context -------------------------------------------------
        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")

        ctx.profile = load_or_create_profile()
        ctx.skill_body = load_playbook("board-prep-deck")
        ctx.rag = retrieve(
            query=f"board meeting preparation {ctx.inputs.quarter_label}",
            specialist_name="board_comms",
            n_builtin=5,
            n_company=3,
            store=store,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="context",
            summary=(
                f"Loaded profile for {ctx.profile.name or 'company'}, "
                f"board-prep skill, and {_chunk_count(ctx.rag)} RAG chunks."
            ),
        )

        # --- Step 2: executive summary -------------------------------------------
        yield WorkflowEvent(
            type="step_start", step_id="exec_summary", step_title="Draft executive summary"
        )
        ctx.exec_summary = await route_to_specialist(
            specialist_name="board_comms",
            query=_build_exec_summary_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="exec_summary",
            summary=_first_line(ctx.exec_summary),
        )

        # --- Step 3: business update ---------------------------------------------
        yield WorkflowEvent(
            type="step_start", step_id="business_update", step_title="Draft business update"
        )
        ctx.business_update = await route_to_specialist(
            specialist_name="cfo",
            query=_build_business_update_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"board business update {ctx.inputs.quarter_label} financial metrics",
                specialist_name="cfo",
                n_builtin=4,
                n_company=3,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="business_update",
            summary=_first_line(ctx.business_update),
        )

        # --- Step 4: deep dive 1 -------------------------------------------------
        yield WorkflowEvent(
            type="step_start", step_id="deep_dive_1", step_title="Draft deep-dive section 1"
        )
        ctx.deep_dive_1 = await route_to_specialist(
            specialist_name="cso",
            query=_build_deep_dive_prompt(ctx, ctx.inputs.deep_dive_topic_1, 1),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=ctx.inputs.deep_dive_topic_1,
                specialist_name="cso",
                n_builtin=5,
                n_company=3,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="deep_dive_1",
            summary=_first_line(ctx.deep_dive_1),
        )

        # --- Step 5: deep dive 2 (optional) --------------------------------------
        if ctx.inputs.deep_dive_topic_2.strip():
            yield WorkflowEvent(
                type="step_start", step_id="deep_dive_2", step_title="Draft deep-dive section 2"
            )
            ctx.deep_dive_2 = await route_to_specialist(
                specialist_name="cso",
                query=_build_deep_dive_prompt(ctx, ctx.inputs.deep_dive_topic_2, 2),
                context=_company_context_block(ctx.profile),
                retrieved_knowledge=retrieve(
                    query=ctx.inputs.deep_dive_topic_2,
                    specialist_name="cso",
                    n_builtin=5,
                    n_company=3,
                    store=store,
                ),
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="deep_dive_2",
                summary=_first_line(ctx.deep_dive_2),
            )
        else:
            # Always emit a paired step_start before step_done so step-
            # progress consumers can rely on the pairing invariant.
            yield WorkflowEvent(
                type="step_start",
                step_id="deep_dive_2",
                step_title="Draft deep-dive section 2",
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="deep_dive_2",
                summary="Skipped — no second deep-dive topic provided.",
            )

        # --- Step 6: decisions (optional) ----------------------------------------
        if ctx.inputs.decisions_needed.strip():
            yield WorkflowEvent(
                type="step_start", step_id="decisions", step_title="Draft decisions section"
            )
            ctx.decisions = await route_to_specialist(
                specialist_name="board_comms",
                query=_build_decisions_prompt(ctx),
                context=_company_context_block(ctx.profile),
                retrieved_knowledge=ctx.rag,
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="decisions",
                summary=_first_line(ctx.decisions),
            )
        else:
            yield WorkflowEvent(
                type="step_start",
                step_id="decisions",
                step_title="Draft decisions section",
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="decisions",
                summary="Skipped — no formal decisions required.",
            )

        # --- Step 7: assemble ----------------------------------------------------
        yield WorkflowEvent(type="step_start", step_id="assemble", step_title="Assemble final deck")
        artifact = _assemble_deck(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across {_section_count(ctx)} sections.",
        )

        yield WorkflowEvent(type="artifact", content=artifact)


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


class _BoardPrepContext:
    """Mutable state accumulated across workflow steps."""

    def __init__(self, inputs: BoardPrepInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.skill_body: str = ""
        self.rag: str = ""
        self.exec_summary: str = ""
        self.business_update: str = ""
        self.deep_dive_1: str = ""
        self.deep_dive_2: str = ""
        self.decisions: str = ""


def _company_context_block(profile: CompanyProfile | None) -> str:
    if profile is None or profile.is_empty():
        return ""
    return profile.to_prompt_block()


def _chunk_count(rag_string: str) -> int:
    if not rag_string:
        return 0
    return rag_string.count("\n[")


def _first_line(text: str, max_len: int = 140) -> str:
    if not text:
        return "(empty)"
    line = text.strip().split("\n", 1)[0].lstrip("# ").strip()
    return line[: max_len - 1] + "…" if len(line) > max_len else line


def _section_count(ctx: _BoardPrepContext) -> int:
    return 3 + int(bool(ctx.deep_dive_2)) + int(bool(ctx.decisions))


def _build_exec_summary_prompt(ctx: _BoardPrepContext) -> str:
    skill_clause = playbook_clause(
        ctx.skill_body, "Follow this playbook for the executive summary section"
    )
    return (
        f"Draft the **Executive Summary** section of the {ctx.inputs.quarter_label} "
        f"board meeting deck (meeting date: {ctx.inputs.meeting_date}). One page max. "
        "Lead with the CEO's POV — what the board most needs to understand "
        "coming into this meeting.\n\n"
        f"Headline metrics for the period:\n{ctx.inputs.headline_metrics}\n\n"
        f"Wins:\n{ctx.inputs.wins}\n\n"
        f"Challenges:\n{ctx.inputs.challenges}\n\n"
        "Output:\n"
        "- A single '## Executive Summary' heading\n"
        "- 3-4 bullets covering: state of the business in one sentence, "
        "most-important development, most-important concern, and the key "
        "question/ask for the board this meeting.\n"
        "- No preamble, no closing meta-commentary. Just the section."
        f"{skill_clause}"
    )


def _build_business_update_prompt(ctx: _BoardPrepContext) -> str:
    return (
        f"Draft the **Business Update** section of the {ctx.inputs.quarter_label} board deck. "
        "This is the financial + KPI review portion.\n\n"
        f"Headline metrics provided by the CEO:\n{ctx.inputs.headline_metrics}\n\n"
        f"Wins:\n{ctx.inputs.wins}\n\n"
        f"Challenges:\n{ctx.inputs.challenges}\n\n"
        "Output (Markdown):\n"
        "- A '## Business Update' heading.\n"
        "- A '### Financial Performance' subsection with a tight prose paragraph "
        "explaining variance vs. plan and what it means. If the headline metrics "
        "include specific numbers, restate them; do NOT invent numbers.\n"
        "- A '### Key Metrics' subsection — bullet list of the most-important "
        "operational metrics and their trends as described.\n"
        "- A '### Customer / Commercial' subsection — wins and losses summarized "
        "with operating implications.\n"
        "- A '### People' subsection — hiring, departures, org notes ONLY if "
        "any of that is in the provided inputs; otherwise omit.\n"
        "- No fabricated metrics. If something wasn't provided, do not invent it. "
        "Use placeholders like '[ARR figure to confirm with CFO]' if a number "
        "is implied but not given."
    )


def _build_deep_dive_prompt(ctx: _BoardPrepContext, topic: str, idx: int) -> str:
    return (
        f"Draft a board **Deep Dive** section on the following strategic topic. "
        "This is one of the 2-3 topics where the board's input is genuinely "
        "valuable, not a status update.\n\n"
        f"**Topic**: {topic}\n\n"
        "Output (Markdown), exactly this structure:\n"
        f"## Deep Dive {idx}: [Topic title in your own words]\n\n"
        "### The Question\n"
        "What specifically does the board need to weigh in on. One paragraph.\n\n"
        "### Context\n"
        "Brief background a board member needs to understand the trade-off. "
        "2-3 short paragraphs.\n\n"
        "### Options\n"
        "2-4 paths, each described in 1-2 sentences. Number them.\n\n"
        "### Recommendation\n"
        "Leadership's recommendation and the rationale. 1-2 paragraphs.\n\n"
        "### Trade-offs\n"
        "What we lose under each option. Short.\n\n"
        "### Decision Needed\n"
        "What we're asking the board to approve, advise, or discuss.\n\n"
        "Constraints: Be specific to the company's stage and situation. "
        "Do not invent numbers. If a number is implied but not provided, "
        "use placeholders like '[X to confirm]'. Keep prose tight."
    )


def _build_decisions_prompt(ctx: _BoardPrepContext) -> str:
    return (
        "Draft the **Decisions and Approvals** section of the board deck. "
        "These are items requiring formal board approval at this meeting.\n\n"
        f"Items requested by the CEO:\n{ctx.inputs.decisions_needed}\n\n"
        "Output (Markdown):\n"
        "- A '## Decisions and Approvals' heading.\n"
        "- For each item: a '### [Item name]' subheading, a 'What is being "
        "approved' line, a 'Terms' line, and a 'Recommendation' line.\n"
        "- Do not invent items not listed. If a term is implied but unspecified "
        "in the inputs, mark it as '[to be confirmed with counsel/CFO]'.\n"
        "- End with one line stating 'Votes will be captured in board minutes.'"
    )


def _assemble_deck(ctx: _BoardPrepContext) -> str:
    """Combine specialist outputs into one structured Markdown deck."""
    parts: list[str] = [
        f"# Board Meeting Deck — {ctx.inputs.quarter_label}",
        f"*Meeting date: {ctx.inputs.meeting_date}*",
        "",
        _ensure_heading(ctx.exec_summary, "## Executive Summary"),
        "",
        _ensure_heading(ctx.business_update, "## Business Update"),
        "",
        ctx.deep_dive_1.strip(),
    ]
    if ctx.deep_dive_2:
        parts.extend(["", ctx.deep_dive_2.strip()])
    if ctx.decisions:
        parts.extend(["", _ensure_heading(ctx.decisions, "## Decisions and Approvals")])
    parts.extend(
        [
            "",
            "## Executive Session",
            "",
            "_The board meeting concludes with an executive session (CEO and other "
            "executives exit) per standard governance practice. The agenda is set "
            "by the board chair._",
            "",
            "---",
            "*Drafted by Open Executive. Review with the CEO and CFO before "
            "distributing to the board.*",
        ]
    )
    return "\n".join(parts).strip() + "\n"


def _ensure_heading(text: str, expected_heading: str) -> str:
    """If the specialist forgot to include the expected H2, prepend it."""
    stripped = text.strip()
    if not stripped:
        return f"{expected_heading}\n\n_(No content generated for this section.)_"
    first_line = stripped.split("\n", 1)[0]
    if first_line.startswith("##"):
        return stripped
    return f"{expected_heading}\n\n{stripped}"
