"""Monthly Business Review workflow — produces an MBR packet:
financial actuals vs. plan, KPI dashboard narrative, function-by-function
status, and escalations needing leadership attention.

Lighter-weight than board_prep — meant to run every month, surface what
moved, and pull escalations early. Uses the CFO, COO, and CSO specialists.
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

_MBR_EXAMPLE_FINANCIALS = (
    "- Revenue: $1.42M vs. $1.50M plan (-5%). MoM +6%. ARR ended at $4.1M.\n"
    "- Burn: $610K vs. $680K plan (favorable; one delayed hire).\n"
    "- Gross margin: 77% (plan 78%).\n"
    "- Cash: $9.2M, 15mo runway.\n"
    "- New ARR: $180K (plan $220K); churn $35K (plan $30K)."
)
_MBR_EXAMPLE_KPIS = (
    "- Activation rate (signup → first report): 42% (was 38%, target 50%)\n"
    "- DAU/MAU: 38% (flat)\n"
    "- Mid-market pipeline coverage: 2.3x (target 3x)\n"
    "- Customer NPS: 51 (was 48)\n"
    "- p99 latency: 280ms (was 310ms)"
)
_MBR_EXAMPLE_FUNCTIONS = (
    "Engineering: shipped workspaces beta to 30 accounts; one P1 incident "
    "(90 min downtime, RCA done).\n\n"
    "Product: workspaces beta NPS 62 from beta cohort; planning GA for July.\n\n"
    "GTM: pipeline soft for next month — 2.1x coverage. Outbound team is "
    "1 AE short after a backfill slipped.\n\n"
    "CS: renewal rate 96%; one mid-market account at risk (Acme — exec "
    "sponsor change). Two case studies in legal review.\n\n"
    "People: two open roles still searching (Staff backend, AE)."
)
_MBR_EXAMPLE_ESCALATIONS = (
    "1. Mid-market pipeline gap for next 60 days — request: 1 contract SDR "
    "or paid demand-gen push.\n"
    "2. Workspaces GA date risk — security review of access-control layer "
    "still pending; if it slips, July GA becomes August.\n"
    "3. AE backfill — re-open at higher comp band or pull from a recruiter?"
)


class MBRInput(BaseModel):
    """Inputs for the Monthly Business Review workflow."""

    month_label: str = Field(
        ...,
        description="Month being reviewed (e.g., 'May 2026' or 'M5 FY26').",
        min_length=3,
        examples=["May 2026"],
    )
    financial_actuals: str = Field(
        ...,
        description=(
            "Revenue, burn, margin, cash, runway — actuals vs. plan. "
            "Bullets are fine. Include MoM trend where you have it."
        ),
        min_length=20,
        examples=[_MBR_EXAMPLE_FINANCIALS],
    )
    kpi_movements: str = Field(
        ...,
        description=(
            "The 4-6 operating KPIs you watch most closely, with this "
            "month's number and trend. Pick the ones leadership actually "
            "argues about, not the vanity ones."
        ),
        min_length=20,
        examples=[_MBR_EXAMPLE_KPIS],
    )
    function_updates: str = Field(
        ...,
        description=(
            "Brief status per function (Engineering, Product, GTM, CS, People). "
            "Bullets or short paragraphs. Highlight what shipped, what "
            "slipped, what's at risk."
        ),
        min_length=40,
        examples=[_MBR_EXAMPLE_FUNCTIONS],
    )
    escalations: str = Field(
        default="",
        description=(
            "Optional. Items needing CEO / leadership-team decision this "
            "month. If anything blocks progress, list it here."
        ),
        examples=[_MBR_EXAMPLE_ESCALATIONS],
    )


class MBRWorkflow(Workflow):
    name = "mbr"
    title = "Monthly Business Review"
    description = (
        "Drafts a monthly MBR packet — financials vs. plan, KPI narrative, "
        "function-by-function status, and escalations needing leadership "
        "decision. Uses the CFO, COO, and CSO specialists."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 3
    playbooks = ("monthly-business-review",)

    def input_model(self) -> type[BaseModel]:
        return MBRInput

    def sample_inputs(self) -> dict[str, Any]:
        return {
            "month_label": "May 2026",
            "financial_actuals": _MBR_EXAMPLE_FINANCIALS,
            "kpi_movements": _MBR_EXAMPLE_KPIS,
            "function_updates": _MBR_EXAMPLE_FUNCTIONS,
            "escalations": _MBR_EXAMPLE_ESCALATIONS,
        }

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="context",
                title="Load context",
                description="Pull company profile and operating RAG.",
            ),
            WorkflowStepDef(
                id="financial_summary",
                title="Financial vs. plan summary",
                description="Crisp paragraph on the financial picture this month.",
            ),
            WorkflowStepDef(
                id="kpi_narrative",
                title="KPI narrative",
                description="What's moving, what isn't, and what to do about it.",
            ),
            WorkflowStepDef(
                id="function_status",
                title="Function-by-function status",
                description="Clean rollup of each function's progress and risks.",
            ),
            WorkflowStepDef(
                id="escalations_block",
                title="Escalations & decisions",
                description="What leadership needs to decide this month (skipped if none).",
            ),
            WorkflowStepDef(
                id="assemble",
                title="Assemble MBR packet",
                description="Combine sections into a structured Markdown packet.",
            ),
        ]

    async def run(  # noqa: C901 — linear orchestration
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, MBRInput)
        ctx = _MBRContext(inputs)

        yield WorkflowEvent(type="step_start", step_id="context", step_title="Load context")
        ctx.profile = load_or_create_profile()
        ctx.playbook = load_playbook("monthly-business-review")
        ctx.rag = retrieve(
            query=f"monthly business review operating metrics {ctx.inputs.month_label}",
            specialist_name="coo",
            n_builtin=4,
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
            type="step_start",
            step_id="financial_summary",
            step_title="Financial vs. plan summary",
        )
        ctx.financial_summary = await route_to_specialist(
            specialist_name="cfo",
            query=_build_financial_summary_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=retrieve(
                query=f"monthly financial review variance plan {ctx.inputs.month_label}",
                specialist_name="cfo",
                n_builtin=4,
                n_company=2,
                store=store,
            ),
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="financial_summary",
            summary=_first_line(ctx.financial_summary),
        )

        yield WorkflowEvent(
            type="step_start", step_id="kpi_narrative", step_title="KPI narrative"
        )
        ctx.kpi_narrative = await route_to_specialist(
            specialist_name="cso",
            query=_build_kpi_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done", step_id="kpi_narrative", summary=_first_line(ctx.kpi_narrative)
        )

        yield WorkflowEvent(
            type="step_start",
            step_id="function_status",
            step_title="Function-by-function status",
        )
        ctx.function_status = await route_to_specialist(
            specialist_name="coo",
            query=_build_function_prompt(ctx),
            context=_company_context_block(ctx.profile),
            retrieved_knowledge=ctx.rag,
        )
        yield WorkflowEvent(
            type="step_done",
            step_id="function_status",
            summary=_first_line(ctx.function_status),
        )

        if ctx.inputs.escalations.strip():
            yield WorkflowEvent(
                type="step_start",
                step_id="escalations_block",
                step_title="Escalations & decisions",
            )
            ctx.escalations_block = await route_to_specialist(
                specialist_name="coo",
                query=_build_escalations_prompt(ctx),
                context=_company_context_block(ctx.profile),
                retrieved_knowledge="",
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="escalations_block",
                summary=_first_line(ctx.escalations_block),
            )
        else:
            # Always emit a paired step_start before step_done so that
            # UI step-progress consumers can rely on the invariant
            # "every step_done has a matching step_start".
            yield WorkflowEvent(
                type="step_start",
                step_id="escalations_block",
                step_title="Escalations & decisions",
            )
            yield WorkflowEvent(
                type="step_done",
                step_id="escalations_block",
                summary="Skipped — no escalations provided.",
            )

        yield WorkflowEvent(
            type="step_start", step_id="assemble", step_title="Assemble MBR packet"
        )
        artifact = _assemble_packet(ctx)
        yield WorkflowEvent(
            type="step_done",
            step_id="assemble",
            summary=f"Assembled {len(artifact)} characters across {_section_count(ctx)} sections.",
        )
        yield WorkflowEvent(type="artifact", content=artifact)


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


class _MBRContext:
    def __init__(self, inputs: MBRInput) -> None:
        self.inputs = inputs
        self.profile: CompanyProfile | None = None
        self.playbook: str = ""
        self.rag: str = ""
        self.financial_summary: str = ""
        self.kpi_narrative: str = ""
        self.function_status: str = ""
        self.escalations_block: str = ""


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


def _section_count(ctx: _MBRContext) -> int:
    return 3 + int(bool(ctx.escalations_block))


def _build_financial_summary_prompt(ctx: _MBRContext) -> str:
    return (
        f"Draft the **Financial Summary** for the {ctx.inputs.month_label} "
        "MBR. Crisp prose, not a restatement of the bullets.\n\n"
        f"Actuals provided:\n{ctx.inputs.financial_actuals}\n\n"
        "Output (Markdown):\n"
        "## Financial Summary\n\n"
        "### Headline\n"
        "1-2 sentences — the most important thing about this month's "
        "financials. Lead with variance vs. plan if it's material.\n\n"
        "### Variances\n"
        "Bullet list of every variance >5% from plan, with a one-line "
        "interpretation (is it timing, is it structural, is it noise?).\n\n"
        "### Forward Look\n"
        "1 paragraph: implications for the next 1-2 months. If burn or "
        "revenue is trending off-plan, say what we'd watch to know whether "
        "it's correcting.\n\n"
        "Do not invent numbers. Use placeholders like '[need from FP&A]' "
        "where a figure is implied but not provided."
    ) + playbook_clause(
        ctx.playbook,
        "Follow the part of this MBR playbook that covers this section (structure, tone, discipline)",
    )


def _build_kpi_prompt(ctx: _MBRContext) -> str:
    return (
        f"Draft the **KPI Narrative** for the {ctx.inputs.month_label} MBR. "
        "Goal: tell the operating story behind the numbers — not just "
        "restate them.\n\n"
        f"KPIs:\n{ctx.inputs.kpi_movements}\n\n"
        "Output (Markdown):\n"
        "## KPI Narrative\n\n"
        "### What's Working\n"
        "1-2 paragraphs on KPIs moving in the right direction and "
        "what we'd attribute it to (avoid taking credit for noise).\n\n"
        "### What's Stuck\n"
        "1-2 paragraphs on KPIs flat or going wrong, with a hypothesis "
        "for why and what we'd test to confirm it.\n\n"
        "### One Thing To Watch\n"
        "1 paragraph: the leading indicator that, if it moves next month, "
        "tells us whether our diagnosis was right.\n\n"
        "Discipline: cite the actual numbers from the inputs. No "
        "fabrication. If a KPI's direction is ambiguous, say so."
    ) + playbook_clause(
        ctx.playbook,
        "Follow the part of this MBR playbook that covers this section (structure, tone, discipline)",
    )


def _build_function_prompt(ctx: _MBRContext) -> str:
    return (
        "Draft the **Function-by-Function Status** section. Per-function "
        "rollup so a board observer can read it in 90 seconds.\n\n"
        f"Updates from the team:\n{ctx.inputs.function_updates}\n\n"
        "Output (Markdown):\n"
        "## Function Status\n\n"
        "For each function with content in the inputs:\n"
        "### [Function name]\n"
        "**Shipped:** 1-2 things — concrete, not 'progress'.\n"
        "**Slipped:** 1-2 things, with 1-line cause.\n"
        "**At risk:** 1 thing the function head is worried about, or "
        "'(nothing flagged)' if none mentioned.\n\n"
        "Cover only functions named in the inputs. Do not invent activity. "
        "If the inputs are thin for a function, keep that function's "
        "section thin — don't pad."
    ) + playbook_clause(
        ctx.playbook,
        "Follow the part of this MBR playbook that covers this section (structure, tone, discipline)",
    )


def _build_escalations_prompt(ctx: _MBRContext) -> str:
    return (
        "Draft the **Escalations & Decisions** section. These need "
        "leadership-team or CEO decision this month.\n\n"
        f"Items raised by the team:\n{ctx.inputs.escalations}\n\n"
        "Output (Markdown):\n"
        "## Escalations & Decisions\n\n"
        "For each item:\n"
        "### [Short title]\n"
        "**Ask:** what specifically the team needs decided or unblocked.\n"
        "**Why now:** what changes if we decide this month vs. next.\n"
        "**Recommendation:** the leader's preferred path (or 'no "
        "recommendation — need leadership input' if genuinely open).\n\n"
        "Do not invent escalations not in the inputs. Order by urgency."
    )


def _assemble_packet(ctx: _MBRContext) -> str:
    parts = [
        f"# Monthly Business Review — {ctx.inputs.month_label}",
        "",
        _ensure_heading(ctx.financial_summary, "## Financial Summary"),
        "",
        _ensure_heading(ctx.kpi_narrative, "## KPI Narrative"),
        "",
        _ensure_heading(ctx.function_status, "## Function Status"),
    ]
    if ctx.escalations_block:
        parts.extend(
            ["", _ensure_heading(ctx.escalations_block, "## Escalations & Decisions")]
        )
    parts.extend(
        [
            "",
            "---",
            "*Drafted by Open Executive. Review with the leadership team "
            "before circulating — every variance call should be verified "
            "against the actual numbers in FP&A's tooling.*",
        ]
    )
    return "\n".join(parts).strip() + "\n"


def _ensure_heading(text: str, expected_heading: str) -> str:
    stripped = text.strip()
    if not stripped:
        return f"{expected_heading}\n\n_(No content generated for this section.)_"
    first_line = stripped.split("\n", 1)[0]
    if first_line.startswith("##"):
        return stripped
    return f"{expected_heading}\n\n{stripped}"
