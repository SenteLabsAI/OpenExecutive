"""Competitive teardown workflow — produces a structured analysis of a
named competitor: positioning, product gaps both ways, GTM motion, the
narrative they use, our counter-narrative, and battle-card content.

Uses the CSO, CMO, and CPO specialists.
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

_CT_EXAMPLE_COMPETITOR = (
    "Hex — series-C-funded data tool, ~$150M ARR, strong in the "
    "data-engineering segment. Recently announced 'workspaces' feature."
)
_CT_EXAMPLE_WHERE = (
    "Show up in mid-market (10-100 seat) deals where the data team has "
    "to sign off. We currently lose ~40% of these to Hex when the "
    "deciding buyer is a data engineer or analytics engineer."
)
_CT_EXAMPLE_OUR_PERCEPTION = (
    "Sales team's read: Hex wins on 'real notebooks for technical users' "
    "and a richer Python/SQL combo. Customers tell us we lose when the "
    "evaluator is hands-on technical; we win when the evaluator is a "
    "RevOps / ops lead who values self-serve."
)
_CT_EXAMPLE_RECENT = (
    "They raised $70M Series C six weeks ago at a $1.4B valuation. "
    "Hired a new CMO last month. Announced 'AI notebooks' beta in their "
    "user conference keynote two weeks ago."
)


class CompetitiveTeardownInput(BaseModel):
    """Inputs for the Competitive Teardown workflow."""

    competitor: str = Field(
        ...,
        description="Competitor name + a 1-2 line description (segment, scale, recent moves).",
        min_length=10,
        examples=[_CT_EXAMPLE_COMPETITOR],
    )
    where_we_compete: str = Field(
        ...,
        description=(
            "The specific segment / deal type / buyer where we and they "
            "actually show up against each other. 'Mid-market' isn't enough — "
            "specify who's deciding and what they're deciding."
        ),
        min_length=20,
        examples=[_CT_EXAMPLE_WHERE],
    )
    our_perception: str = Field(
        ...,
        description=(
            "What our sales / CS / product team think makes the competitor "
            "win (or lose). Be honest; this is for our own use."
        ),
        min_length=20,
        examples=[_CT_EXAMPLE_OUR_PERCEPTION],
    )
    recent_moves: str = Field(
        default="",
        description=(
            "Optional. Funding, hires, launches, pricing changes the "
            "competitor has made in the last ~6 months. Helps spot the "
            "story they're telling."
        ),
        examples=[_CT_EXAMPLE_RECENT],
    )


class CompetitiveTeardownWorkflow(Workflow):
    name = "competitive_teardown"
    title = "Competitive Teardown"
    description = (
        "Drafts a structured competitive teardown — positioning, product "
        "differences both ways, GTM read, the narrative they push, our "
        "counter-narrative, and starter battle-card content. Uses the "
        "CSO, CMO, and CPO specialists."
    )
    section = WorkflowSection.GROWTH
    estimated_minutes = 4
    playbooks = ("competitive-teardown",)

    def input_model(self) -> type[BaseModel]:
        return CompetitiveTeardownInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "competitor": _CT_EXAMPLE_COMPETITOR,
            "where_we_compete": _CT_EXAMPLE_WHERE,
            "our_perception": _CT_EXAMPLE_OUR_PERCEPTION,
            "recent_moves": _CT_EXAMPLE_RECENT,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile and competitive-strategy RAG.",
            ),
            WorkflowStepDef(
                id="positioning",
                title="Position the competitor",
                description="The story they tell the market and the segment they own.",
            ),
            WorkflowStepDef(
                id="product",
                title="Product comparison",
                description="Concrete strengths / gaps in both directions.",
            ),
            WorkflowStepDef(
                id="counter_narrative",
                title="Our counter-narrative",
                description="The story WE tell when this competitor is in the deal.",
            ),
            WorkflowStepDef(
                id="battlecard",
                title="Battle card",
                description="Discovery questions, traps, talking points for sales.",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble teardown",
                description="Combine into one Markdown document.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, CompetitiveTeardownInput)
        ctx = _CTCtx(inputs)

        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        ctx.profile = load_or_create_profile()
        ctx.playbook = load_playbook("competitive-teardown")
        ctx.rag = retrieve(
            query=f"competitive positioning differentiation {ctx.inputs.competitor}",
            specialist_name="cso",
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
            type="step_start", step_id="positioning", step_title="Position the competitor"
        )
        ctx.positioning = await route_to_specialist(
            specialist_name="cso",
            query=_build_positioning_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="positioning", summary=_first_line(ctx.positioning)
        )

        yield WorkflowEvent(
            type="step_start", step_id="product", step_title="Product comparison"
        )
        ctx.product = await route_to_specialist(
            specialist_name="cpo",
            query=_build_product_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"product differentiation {ctx.inputs.competitor}",
                specialist_name="cpo",
                n_builtin=4,
                n_company=3,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done", step_id="product", summary=_first_line(ctx.product)
        )

        yield WorkflowEvent(
            type="step_start", step_id="counter_narrative", step_title="Our counter-narrative"
        )
        ctx.counter = await route_to_specialist(
            specialist_name="cmo",
            query=_build_counter_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="counter_narrative", summary=_first_line(ctx.counter)
        )

        yield WorkflowEvent(
            type="step_start", step_id="battlecard", step_title="Battle card"
        )
        ctx.battlecard = await route_to_specialist(
            specialist_name="cmo",
            query=_build_battlecard_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge="",
        )
        yield WorkflowEvent(
            type="step_done", step_id="battlecard", summary=_first_line(ctx.battlecard)
        )

        yield WorkflowEvent(
            type="step_start", step_id="assemble", step_title="Assemble teardown"
        )
        artifact = _assemble_doc(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across 4 sections.",
        )
        yield WorkflowEvent(type="artifact", content=artifact)


class _CTCtx:
    def __init__(self, inputs: CompetitiveTeardownInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.playbook: str = ""
        self.rag: str = ""
        self.positioning: str = ""
        self.product: str = ""
        self.counter: str = ""
        self.battlecard: str = ""


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


def _build_positioning_prompt(ctx: _CTCtx) -> str:
    recent_block = (
        f"\n\nRecent moves:\n{ctx.inputs.recent_moves}"
        if ctx.inputs.recent_moves.strip()
        else ""
    )
    return (
        f"Analyze the **Positioning** of {ctx.inputs.competitor} — how they "
        "tell the market about themselves and which segment they own.\n\n"
        f"Where we compete with them:\n{ctx.inputs.where_we_compete}\n\n"
        f"Our team's read:\n{ctx.inputs.our_perception}"
        f"{recent_block}\n\n"
        "Output (Markdown):\n"
        "## Their Positioning\n\n"
        "### Category & frame\n"
        "1 paragraph: how they want buyers to categorize them. Reference "
        "their website / keynote / launch language if it's distinctive.\n\n"
        "### Segment they actually own\n"
        "1 paragraph: where they're winning today and why. Avoid "
        "speculation about new segments; focus on observed traction.\n\n"
        "### What they're moving toward\n"
        "1-2 paragraphs: based on recent moves (hires, launches, funding), "
        "what's the segment shift you'd predict in the next 6-12 months? "
        "Be explicit about the inference chain.\n\n"
        "Discipline: cite specific product / launch / hire details. Do "
        "NOT invent press quotes or revenue figures. If the inputs don't "
        "support a claim, say 'unclear from public data'."
    ) + playbook_clause(
        ctx.playbook,
        "Apply the parts of this competitive-teardown playbook that cover this section",
    )


def _build_product_prompt(ctx: _CTCtx) -> str:
    return (
        f"Compare our product with {ctx.inputs.competitor} concretely. The "
        "goal isn't to declare a winner; it's to map the differences "
        "buyers actually feel.\n\n"
        f"Where we compete:\n{ctx.inputs.where_we_compete}\n\n"
        f"Our team's read on why they win/lose:\n{ctx.inputs.our_perception}\n\n"
        "Output (Markdown):\n"
        "## Product Comparison\n\n"
        "### Where they're stronger\n"
        "Bullet list, each item:\n"
        "- **[Feature / capability]:** how it shows up in their product, "
        "what buyer use case it serves, how much it matters in our segment.\n\n"
        "### Where we're stronger\n"
        "Same shape, our advantages.\n\n"
        "### Where they have parity (and we tell ourselves we don't)\n"
        "1 paragraph. Common sales-team blind spot — assuming we're "
        "uniquely better at something we actually only marginally are.\n\n"
        "### Pricing & packaging differences\n"
        "1 paragraph. If their pricing is opaque, say so.\n\n"
        "Discipline: name specific product capabilities, not abstractions. "
        "If you're not sure about a capability, mark it '[verify in next "
        "competitive review]' rather than guess."
    ) + playbook_clause(
        ctx.playbook,
        "Apply the parts of this competitive-teardown playbook that cover this section",
    )


def _build_counter_prompt(ctx: _CTCtx) -> str:
    return (
        f"Draft our **Counter-Narrative** for when {ctx.inputs.competitor} "
        "is in the deal. This is the story our team tells — not in slide-"
        "form, just the 3-5 sentences that flow when a buyer says "
        "'we're also looking at them'.\n\n"
        f"Their positioning (your prior step output mirrors this):\n"
        f"{ctx.inputs.our_perception}\n\n"
        "Output (Markdown):\n"
        "## Our Counter-Narrative\n\n"
        "### The 3-line response\n"
        "Three short sentences a rep can deliver verbatim when the "
        "competitor's name comes up. The first acknowledges them, the "
        "second reframes, the third sets the bar.\n\n"
        "### Discovery questions to anchor\n"
        "5-7 discovery questions that, if asked, surface the situations "
        "where we win. (We don't win by trashing them; we win by "
        "surfacing the buyer needs we serve better.)\n\n"
        "### When NOT to compete\n"
        "1 paragraph. The buyer profile / deal shape where we should "
        "actually walk away or no-bid. Saying this out loud saves "
        "everyone's time.\n\n"
        "Discipline: do NOT badmouth the competitor. Reps repeating "
        "trash-talk in customer meetings makes us look insecure. "
        "Frame around buyer outcomes, not competitor flaws."
    ) + playbook_clause(
        ctx.playbook,
        "Apply the parts of this competitive-teardown playbook that cover this section",
    )


def _build_battlecard_prompt(ctx: _CTCtx) -> str:
    return (
        f"Draft a starter **Battle Card** for {ctx.inputs.competitor}, ready "
        "for the sales team. Concise and actionable.\n\n"
        "Output (Markdown):\n"
        "## Battle Card\n\n"
        "### Quick reference\n"
        "- **Their pitch in one line:** ...\n"
        "- **Where they win against us:** ...\n"
        "- **Where we win against them:** ...\n"
        "- **Deal segments to disqualify:** ...\n\n"
        "### Common buyer objections we hear\n"
        "Bullet list of 3-5 objections the competitor's pitch creates "
        "for us, each with a 1-2 sentence response.\n\n"
        "### Traps to set in discovery\n"
        "3-5 questions whose answers, if positive, indicate our deal. "
        "Examples (template, not literal): 'How important is it that "
        "non-technical users can build reports without IT?' If the answer "
        "is 'very', we win.\n\n"
        "Discipline: every line must be usable in a live sales conversation. "
        "If a line would only be useful with internal explanation, cut it."
    ) + playbook_clause(
        ctx.playbook,
        "Apply the parts of this competitive-teardown playbook that cover this section",
    )


def _assemble_doc(ctx: _CTCtx) -> str:
    parts = [
        f"# Competitive Teardown — {ctx.inputs.competitor.splitlines()[0][:80]}",
        "",
        "## Where We Compete",
        "",
        ctx.inputs.where_we_compete.strip(),
        "",
        _ensure_heading(ctx.positioning, "## Their Positioning"),
        "",
        _ensure_heading(ctx.product, "## Product Comparison"),
        "",
        _ensure_heading(ctx.counter, "## Our Counter-Narrative"),
        "",
        _ensure_heading(ctx.battlecard, "## Battle Card"),
        "",
        "---",
        "*Drafted by Open Executive. Stress-test with the sales and CS "
        "teams; their pattern-match on this competitor is the source of "
        "truth, not this document.*",
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
