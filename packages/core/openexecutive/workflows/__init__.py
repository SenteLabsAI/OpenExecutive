"""Workflows: deterministic orchestrations of specialists + skills + RAG that
produce a structured executive artifact (board prep deck, quarterly plan,
performance review, etc.).

Workflows differ from chat:
- Inputs are structured (a typed form), not free-form
- Steps are explicit and ordered (the user sees progress)
- The output is a rendered artifact (Markdown), not a conversation turn

Each concrete workflow is a `Workflow` subclass registered in
`WORKFLOW_REGISTRY` below. The HTTP layer is in
`openexecutive.api.routes.workflows`.
"""
from __future__ import annotations

from openexecutive.workflows.annual_plan import AnnualPlanWorkflow
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowMeta,
    WorkflowSection,
    WorkflowStepDef,
)
from openexecutive.workflows.board_prep import BoardPrepWorkflow
from openexecutive.workflows.churn_deep_dive import ChurnDeepDiveWorkflow
from openexecutive.workflows.comp_refresh import CompRefreshWorkflow
from openexecutive.workflows.competitive_teardown import CompetitiveTeardownWorkflow
from openexecutive.workflows.crisis_comms import CrisisCommsWorkflow
from openexecutive.workflows.department_check_in import DepartmentCheckInWorkflow
from openexecutive.workflows.end_of_day_digest import EndOfDayDigestWorkflow
from openexecutive.workflows.engagement_value_report import (
    EngagementValueReportWorkflow,
)
from openexecutive.workflows.exec_search_brief import ExecSearchBriefWorkflow
from openexecutive.workflows.executive_reflection import ExecutiveReflectionWorkflow
from openexecutive.workflows.executive_research import ExecutiveResearchWorkflow
from openexecutive.workflows.fundraising_prep import FundraisingPrepWorkflow
from openexecutive.workflows.gtm_launch import GTMLaunchWorkflow
from openexecutive.workflows.investor_update import InvestorUpdateWorkflow
from openexecutive.workflows.ma_evaluation import MAEvaluationWorkflow
from openexecutive.workflows.mbr import MBRWorkflow
from openexecutive.workflows.morning_brief import MorningBriefWorkflow
from openexecutive.workflows.org_design import OrgDesignWorkflow
from openexecutive.workflows.performance_review import PerformanceReviewWorkflow
from openexecutive.workflows.pricing_review import PricingReviewWorkflow
from openexecutive.workflows.product_strategy import ProductStrategyWorkflow
from openexecutive.workflows.quarterly_plan import QuarterlyPlanWorkflow
from openexecutive.workflows.risk_register import RiskRegisterWorkflow

WORKFLOW_REGISTRY: dict[str, Workflow] = {
    "annual_plan": AnnualPlanWorkflow(),
    "department_check_in": DepartmentCheckInWorkflow(),
    "board_prep": BoardPrepWorkflow(),
    "churn_deep_dive": ChurnDeepDiveWorkflow(),
    "comp_refresh": CompRefreshWorkflow(),
    "competitive_teardown": CompetitiveTeardownWorkflow(),
    "crisis_comms": CrisisCommsWorkflow(),
    # `morning_brief` and `end_of_day_digest` are the first workflows in
    # the registry that target the principal directly (not user-invoked
    # report artifacts). The scheduler fires them on a recurring schedule
    # and dispatches the artifact via DM.
    "morning_brief": MorningBriefWorkflow(),
    "end_of_day_digest": EndOfDayDigestWorkflow(),
    # `executive_reflection` is the first workflow OE runs ON ITSELF —
    # not targeting a department or the principal but its own org
    # coordination decisions. Fires ~30 minutes before the morning
    # brief, can invoke real tools (DMs, broadcasts, follow-ups).
    "executive_reflection": ExecutiveReflectionWorkflow(),
    # `exec_search_brief` is an ADVISORY deliverable (a hiring brief /
    # scorecard from the CHRO), not part of the removed talent pipeline —
    # it never touched the candidate/engagement tables.
    "exec_search_brief": ExecSearchBriefWorkflow(),
    "fundraising_prep": FundraisingPrepWorkflow(),
    "gtm_launch": GTMLaunchWorkflow(),
    "engagement_value_report": EngagementValueReportWorkflow(),
    "investor_update": InvestorUpdateWorkflow(),
    "ma_evaluation": MAEvaluationWorkflow(),
    "mbr": MBRWorkflow(),
    "org_design": OrgDesignWorkflow(),
    "performance_review": PerformanceReviewWorkflow(),
    "pricing_review": PricingReviewWorkflow(),
    "product_strategy": ProductStrategyWorkflow(),
    "quarterly_plan": QuarterlyPlanWorkflow(),
    "risk_register": RiskRegisterWorkflow(),
    # `executive_research` drives the 7-specialist council in research
    # mode with web_search, then runs the Executive's tool-use loop
    # (same shape as `executive_reflection`) so findings get routed
    # via the existing outbound toolkit: DMs to heads of departments,
    # DMs to the principal, briefing alerts, watchlist additions,
    # follow-ups, workflow suggestions. Fired manually via the
    # `run_executive_research` chat tool, at end of onboarding, and on
    # a 2h cron gated by a state-hash skip-if-unchanged check.
    "executive_research": ExecutiveResearchWorkflow(),
}


def get_workflow(name: str) -> Workflow:
    """Resolve a workflow by name.

    Built-ins win on a name collision (checked first). User-created
    ("dynamic") definitions are resolved lazily from the dynamic store and
    wrapped in the generic ``DynamicWorkflow`` engine. Imports are deferred
    to avoid a workflows -> orchestrator -> workflows import cycle.
    """
    workflow = WORKFLOW_REGISTRY.get(name)
    if workflow is not None:
        return workflow

    from openexecutive.workflows.dynamic import DynamicWorkflow
    from openexecutive.workflows.dynamic_store import get_definition

    defn = get_definition(name)
    if defn is not None and defn.is_active:
        return DynamicWorkflow(defn)
    raise KeyError(f"Unknown workflow: {name}")


def list_workflows() -> list[Workflow]:
    """All runnable workflows: built-ins followed by active dynamic ones."""
    builtins = list(WORKFLOW_REGISTRY.values())

    from openexecutive.workflows.dynamic import DynamicWorkflow
    from openexecutive.workflows.dynamic_store import list_definitions

    dynamics = [DynamicWorkflow(d) for d in list_definitions(active_only=True)]
    return builtins + dynamics


__all__ = [
    "Workflow",
    "WorkflowEvent",
    "WorkflowMeta",
    "WorkflowSection",
    "WorkflowStepDef",
    "WORKFLOW_REGISTRY",
    "get_workflow",
    "list_workflows",
]
