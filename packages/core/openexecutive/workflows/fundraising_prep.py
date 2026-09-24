"""Fundraising prep workflow — produces a starting pitch narrative,
metrics-page outline, anticipated investor Q&A, and a data-room checklist
as a single Markdown artifact.

Uses the CFO, CSO, and Board Comms specialists, grounded by domain RAG.
Output is a draft for the founder to sharpen before going on-cycle.
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

_FR_EXAMPLE_ROUND = (
    "Series B, $25M target, $100M pre-money. Lead TBD; current top-of-funnel "
    "is two crossover funds and one Series B-focused fund."
)
_FR_EXAMPLE_TRACTION = (
    "- ARR $4.1M, +18% QoQ, ~135% YoY\n"
    "- NRR 112%, gross retention 94%\n"
    "- 11 new logos this quarter; 5 mid-market (>$50K ACV)\n"
    "- Sales cycle 42 days mid-market (was 71 last year)\n"
    "- Burn $660K/mo, 14mo runway, gross margin 78%"
)
_FR_EXAMPLE_NARRATIVE = (
    "The category is shifting from 'analytics for engineers' to 'an "
    "operational reporting layer the whole company can act on.' We have "
    "the only product where business teams can self-serve without IT, and "
    "we just crossed the threshold where mid-market adoption proves it."
)
_FR_EXAMPLE_USE_OF_FUNDS = (
    "Roughly 50% GTM (build a 12-AE org, scale demand gen), 35% R&D "
    "(platform tier + AI features), 15% G&A / runway buffer. Targeting "
    "$15M ARR within 18 months of close."
)
_FR_EXAMPLE_RISKS_KNOWN = (
    "Two big-name competitors raised growth rounds in the last 6 months. "
    "Our SMB churn ticked up to 1.8% last quarter. We have not yet hired "
    "a CRO; sales org reports to the founder."
)


class FundraisingPrepInput(BaseModel):
    """Inputs for the Fundraising Prep workflow."""

    round_label: str = Field(
        ...,
        description="Which round are you raising? Include target size and stage where possible.",
        min_length=4,
        examples=[_FR_EXAMPLE_ROUND],
    )
    traction_metrics: str = Field(
        ...,
        description=(
            "The metrics you'd put on the 'why now' slide — ARR, growth rate, "
            "NRR, gross margin, burn, runway. Be precise; investors will pull "
            "on every number. Bullets are fine."
        ),
        min_length=20,
        examples=[_FR_EXAMPLE_TRACTION],
    )
    narrative_thesis: str = Field(
        ...,
        description=(
            "One paragraph: the why-now market shift you're riding, and why "
            "your company specifically wins. The pitch starts here."
        ),
        min_length=20,
        examples=[_FR_EXAMPLE_NARRATIVE],
    )
    use_of_funds: str = Field(
        ...,
        description=(
            "How you'd deploy the round, and what milestones it buys you "
            "(rough % allocation across GTM / R&D / G&A is fine)."
        ),
        min_length=15,
        examples=[_FR_EXAMPLE_USE_OF_FUNDS],
    )
    known_risks: str = Field(
        default="",
        description=(
            "Optional. The objections you know smart investors will raise — "
            "competitive moves, churn pockets, exec gaps, regulatory. Better "
            "we surface them now than be ambushed in a partner meeting."
        ),
        examples=[_FR_EXAMPLE_RISKS_KNOWN],
    )


class FundraisingPrepWorkflow(Workflow):
    name = "fundraising_prep"
    title = "Fundraising Prep"
    description = (
        "Drafts a pitch narrative, metrics-page outline, anticipated "
        "investor Q&A, and data-room checklist. Uses the CFO, CSO, and "
        "Board Comms specialists."
    )
    section = WorkflowSection.CAPITAL
    estimated_minutes = 4
    playbooks = ("fundraise-narrative",)

    def input_model(self) -> type[BaseModel]:
        return FundraisingPrepInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "round_label": _FR_EXAMPLE_ROUND,
            "traction_metrics": _FR_EXAMPLE_TRACTION,
            "narrative_thesis": _FR_EXAMPLE_NARRATIVE,
            "use_of_funds": _FR_EXAMPLE_USE_OF_FUNDS,
            "known_risks": _FR_EXAMPLE_RISKS_KNOWN,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile and fundraising/strategy RAG.",
            ),
            WorkflowStepDef(
                id="narrative",
                title="Sharpen pitch narrative",
                description="Tighten the why-now / why-us into 3-4 paragraphs.",
            ),
            WorkflowStepDef(
                id="metrics_page",
                title="Draft metrics page",
                description="The numbers slide: what to lead with and what to call out.",
            ),
            WorkflowStepDef(
                id="qa_prep",
                title="Anticipated Q&A",
                description="The 10-15 questions investors will ask, with starting answers.",
            ),
            WorkflowStepDef(
                id="data_room",
                title="Data-room checklist",
                description="What to assemble before partner meetings start.",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble final packet",
                description="Combine sections into a structured Markdown packet.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, FundraisingPrepInput)
        ctx = _FundraisingPrepContext(inputs)

        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        ctx.profile = load_or_create_profile()
        ctx.playbook = load_playbook("fundraise-narrative")
        ctx.rag = retrieve(
            query=f"venture fundraising pitch narrative {ctx.inputs.round_label}",
            specialist_name="cso",
            n_builtin=5,
            n_company=3,
            store=store,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="context",
            summary=(
                f"Loaded profile for {ctx.profile.name or 'company'} "
                f"and {_chunk_count(ctx.rag)} RAG chunks."
            ),
        )

        yield WorkflowEvent(
            type="step_start", step_id="narrative", step_title="Sharpen pitch narrative"
        )
        ctx.narrative = await route_to_specialist(
            specialist_name="board_comms",
            query=_build_narrative_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="narrative", summary=_first_line(ctx.narrative)
        )

        yield WorkflowEvent(
            type="step_start", step_id="metrics_page", step_title="Draft metrics page"
        )
        ctx.metrics_page = await route_to_specialist(
            specialist_name="cfo",
            query=_build_metrics_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"venture metrics for {ctx.inputs.round_label}",
                specialist_name="cfo",
                n_builtin=4,
                n_company=3,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done", step_id="metrics_page", summary=_first_line(ctx.metrics_page)
        )

        yield WorkflowEvent(
            type="step_start", step_id="qa_prep", step_title="Anticipated Q&A"
        )
        ctx.qa = await route_to_specialist(
            specialist_name="cso",
            query=_build_qa_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(type="step_done", step_id="qa_prep", summary=_first_line(ctx.qa))

        yield WorkflowEvent(
            type="step_start", step_id="data_room", step_title="Data-room checklist"
        )
        ctx.data_room = await route_to_specialist(
            specialist_name="cfo",
            query=_build_data_room_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge="",
        )
        yield WorkflowEvent(
            type="step_done", step_id="data_room", summary=_first_line(ctx.data_room)
        )

        yield WorkflowEvent(
            type="step_start", step_id="assemble", step_title="Assemble final packet"
        )
        artifact = _assemble_packet(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across 4 sections.",
        )
        yield WorkflowEvent(type="artifact", content=artifact)


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


class _FundraisingPrepContext:
    def __init__(self, inputs: FundraisingPrepInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.playbook: str = ""
        self.rag: str = ""
        self.narrative: str = ""
        self.metrics_page: str = ""
        self.qa: str = ""
        self.data_room: str = ""


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


def _build_narrative_prompt(ctx: _FundraisingPrepContext) -> str:
    risks_block = (
        f"\n\nKnown risks / objections to pre-empt:\n{ctx.inputs.known_risks}"
        if ctx.inputs.known_risks.strip()
        else ""
    )
    return (
        f"Draft the **Pitch Narrative** for the {ctx.inputs.round_label} raise. "
        "This is the 3-4 paragraph why-now / why-us that the deck and the "
        "first-meeting voiceover both pull from. It must be specific to this "
        "company — generic 'AI is hot' framing is a fail.\n\n"
        f"Founder's starting thesis:\n{ctx.inputs.narrative_thesis}\n\n"
        f"Traction:\n{ctx.inputs.traction_metrics}{risks_block}\n\n"
        "Output (Markdown), this exact structure:\n"
        "## Pitch Narrative\n\n"
        "### Why Now\n"
        "1 paragraph: the market shift that makes this the right moment. "
        "Reference specific evidence — adoption inflection, regulatory change, "
        "platform shift. No buzzwords.\n\n"
        "### Why Us\n"
        "1 paragraph: what we have that's hard to replicate. Concrete: "
        "product differentiation, distribution edge, team unfair advantage.\n\n"
        "### What This Round Buys\n"
        "1 paragraph: the next milestone we're funding. Be specific about "
        "the proof point investors should hold us to.\n\n"
        "Do not invent metrics or customer names not in the inputs. Use "
        "placeholders like '[ARR figure]' when needed."
    ) + playbook_clause(
        ctx.playbook,
        "Follow this fundraise-narrative playbook for the narrative arc",
    )


def _build_metrics_prompt(ctx: _FundraisingPrepContext) -> str:
    return (
        f"Draft the **Metrics Page** outline for the {ctx.inputs.round_label} "
        "pitch. Goal: the page investors screenshot. Lead with the metric "
        "that best tells the story; bury the ones we don't want a partner "
        "fixated on (but don't omit them — investors hate omissions).\n\n"
        f"Founder-supplied numbers:\n{ctx.inputs.traction_metrics}\n\n"
        "Output (Markdown):\n"
        "## Metrics Page\n\n"
        "### Hero Metric\n"
        "1 line: the metric to lead with and the comparable benchmark "
        "(median for SaaS at this stage, or a peer's recent disclosed number).\n\n"
        "### Supporting Metrics\n"
        "Bullet list of 4-6 metrics that round out the story (NRR, gross "
        "margin, burn multiple, CAC payback, sales cycle, magic number). "
        "For each: the number, the trend, and one-line interpretation.\n\n"
        "### Caveats To Pre-Disclose\n"
        "Any metric where the number alone misleads (e.g., NRR including "
        "a one-time expansion, or growth rate including a price increase). "
        "Better we frame it than a partner does.\n\n"
        "Do NOT invent numbers. Use placeholders like '[CAC payback months — "
        "need from finance]' where founder didn't provide a value."
    )


def _build_qa_prompt(ctx: _FundraisingPrepContext) -> str:
    risks_block = (
        f"\n\nKnown risks / objections:\n{ctx.inputs.known_risks}"
        if ctx.inputs.known_risks.strip()
        else ""
    )
    return (
        "Draft the **Anticipated Q&A** section. List the 10-15 questions a "
        "sharp investor will ask in a first or second partner meeting, with "
        "a starting answer for each. Order by likelihood, not difficulty.\n\n"
        f"Round: {ctx.inputs.round_label}\n"
        f"Traction:\n{ctx.inputs.traction_metrics}\n"
        f"Narrative:\n{ctx.inputs.narrative_thesis}\n"
        f"Use of funds:\n{ctx.inputs.use_of_funds}{risks_block}\n\n"
        "Output (Markdown):\n"
        "## Anticipated Q&A\n\n"
        "For each question:\n"
        "### Q: [the question, as a partner would phrase it]\n"
        "**A (draft):** 2-4 sentences. Honest, specific. If we don't have "
        "the answer yet, say what we'd need to know.\n\n"
        "Mix categories: market / competition / business model / unit economics / "
        "team / use of funds / exit. Include at least 2 hard ones — the "
        "objection that, if left unanswered, kills the round."
    )


def _build_data_room_prompt(ctx: _FundraisingPrepContext) -> str:
    return (
        "Draft the **Data-Room Checklist**. What every investor will request "
        "in diligence and we should have ready before going on-cycle.\n\n"
        f"Round: {ctx.inputs.round_label}\n\n"
        "Output (Markdown):\n"
        "## Data-Room Checklist\n\n"
        "Group as subsections: Financial, Commercial, Product, Legal, People, "
        "Compliance. Under each, a checklist:\n"
        "- [ ] Item — one-line note about format expected\n\n"
        "Cover at minimum: monthly P&L (3yr historical + 2yr forecast), "
        "cohort retention, customer concentration, top-20 customer list with "
        "ACV / start date / status, sales pipeline by stage, churn breakdown, "
        "cap table, option pool status, key contracts, IP assignment, "
        "employee NDAs, security/compliance certs. Stage-appropriate — don't "
        "list items that don't apply at this round size."
    )


def _assemble_packet(ctx: _FundraisingPrepContext) -> str:
    parts = [
        f"# Fundraising Prep — {ctx.inputs.round_label}",
        "",
        "## Round Overview",
        "",
        ctx.inputs.round_label.strip(),
        "",
        _ensure_heading(ctx.narrative, "## Pitch Narrative"),
        "",
        _ensure_heading(ctx.metrics_page, "## Metrics Page"),
        "",
        _ensure_heading(ctx.qa, "## Anticipated Q&A"),
        "",
        _ensure_heading(ctx.data_room, "## Data-Room Checklist"),
        "",
        "---",
        "*Drafted by Open Executive. Sharpen with the CFO and a trusted "
        "investor before sending the deck out — the first partner meeting "
        "will pull on every number.*",
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
