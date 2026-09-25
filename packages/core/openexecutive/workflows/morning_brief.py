"""Morning brief workflow — a short proactive briefing to the principal.

Renders a one-screen Markdown summary covering:
  • At-risk department goals
  • Proposals awaiting the principal's decision
  • Anything OE acted on since the last brief
  • The top decision the principal needs to make today

The scheduler fires a `principal_brief_morning` action once per day at
the configured time (default 08:00 UTC) which runs this workflow and
DMs the artifact to the principal via their preferred channel. The
workflow can also be triggered manually through the workflow API for
testing or to re-send.

Target audience is hardcoded to the principal — this is a
personal-rhythm artifact, not org-coordination. See the
`## Choosing Who to Tell` section in the persona for the broader
audience-selection rules.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowSection,
    WorkflowStepDef,
)

logger = logging.getLogger(__name__)


class MorningBriefInput(BaseModel):
    """Inputs for a morning brief run.

    Both fields are optional — the workflow defaults the period to
    today's date so the scheduler can fire it with no arguments.
    """

    period_label: str = Field(
        default="",
        description="Human-readable period label (e.g. '2026-05-26'). Auto-filled when blank.",
    )
    force_full: bool = Field(
        default=False,
        description=(
            "Render the full brief even when nothing changed since the last "
            "delivered one (bypasses the one-line 'nothing new' suppression)."
        ),
    )


BRIEF_KIND = "principal_brief_morning"


# Narrative synthesis (system prompt + context render + LLM call) is shared
# with the on-page briefing header via openexecutive.briefing.narrative, so
# both surfaces tell the same story in the same voice.


class MorningBriefWorkflow(Workflow):
    name = "morning_brief"
    title = "Morning Brief"
    description = (
        "A short proactive briefing for the principal: what changed "
        "overnight, what needs them today, what's at risk, the top "
        "decision to make. Fires automatically each morning via the "
        "scheduler; can also be invoked manually."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 1
    background = True

    def input_model(self) -> type[BaseModel]:
        return MorningBriefInput

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="load_context",
                title="Gather today's state",
                description="Pull /today data, recent OE activity, and pending proposals.",
            ),
            WorkflowStepDef(
                id="synthesize",
                title="Synthesize the brief",
                description="Render a ≤200-word morning brief in the Executive's voice.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, MorningBriefInput)
        period = inputs.period_label or datetime.now(UTC).strftime("%Y-%m-%d")

        # ------------------------------------------------------------------ #
        # Step 1: gather context
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="load_context",
            step_title="Gather today's state",
        )

        from openexecutive.api.routes import today as today_route
        from openexecutive.briefing import brief_state
        from openexecutive.memory.workspace_settings import effective_workspace_mode
        from openexecutive.orchestrator.schedule_tools import current_session

        # The window is "since the last brief I actually delivered" (24 h on
        # a cold store), so "what changed" is a real delta, not the latest N.
        since = brief_state.since_for(BRIEF_KIND)
        # Solo / team: the solo brief speaks to one founder (goals by area,
        # no people waiting). A caller's session override (evals) wins.
        mode = effective_workspace_mode(current_session.get())

        try:
            today_response = today_route._build_today()
            today_data = today_response.model_dump()
            # Focus the brief on action items — drop monitoring/watchlist noise
            # so it doesn't land in the DM's "Needs you" section (same exclusion
            # the /today narrative makes).
            today_data["proposals"] = [
                p for p in today_data["proposals"]
                if p.get("category", "action") == "action"
            ]
        except Exception:
            logger.exception("morning_brief: /today aggregation failed")
            today_data = {"departments": [], "people": [], "proposals": []}
        if mode == "solo":
            # What the principal owns that is due this week or overdue — their
            # dated commitments. It lands here even when no channel reaches
            # them for a nudge. Never raises (reads as empty on failure).
            from openexecutive.attunement.open_loops import principal_due_soon

            today_data["due_soon"] = principal_due_soon()

        try:
            activity_response = today_route._build_activity(20, since=since)
            activity = [item.model_dump() for item in activity_response.items]
        except Exception:
            logger.exception("morning_brief: /today/activity aggregation failed")
            activity = []

        handled = brief_state.handled_since(since)
        pending_suggestions = brief_state.pending_watch_suggestions()
        fingerprint = brief_state.build_brief_fingerprint(
            today_data=today_data, activity=activity, handled=handled, since=since,
            pending_watch_suggestions=pending_suggestions, mode=mode,
        )
        previous = brief_state.last_delivered(BRIEF_KIND)
        suppressed = (
            brief_state.suppress_unchanged_enabled()
            and not inputs.force_full
            and previous is not None
            and previous.input_hash == fingerprint
        )

        yield WorkflowEvent(
            type="step_done",
            step_id="load_context",
            summary=(
                f"depts={len(today_data['departments'])} "
                f"proposals={len(today_data['proposals'])} "
                f"activity={len(activity)} handled={len(handled)} "
                f"since={since.isoformat()[:16]}"
            ),
        )
        # Structured payload for the scheduler: it records the fingerprint
        # only after a successful delivery, so "delivered" stays exact.
        yield WorkflowEvent(
            type="result",
            data={
                "brief_fingerprint": fingerprint,
                "suppressed": suppressed,
                "since": since.isoformat(),
            },
        )

        if suppressed:
            artifact_text = brief_state.suppressed_line(len(today_data["proposals"]))
            yield WorkflowEvent(
                type="step_done",
                step_id="synthesize",
                summary="unchanged since last brief — one-liner, no model call",
            )
            yield WorkflowEvent(type="artifact", content=artifact_text)
            yield WorkflowEvent(type="done")
            return

        # ------------------------------------------------------------------ #
        # Step 2: synthesize
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="synthesize",
            step_title="Synthesize the brief",
        )

        from openexecutive.briefing.narrative import (
            QUIET_PRINCIPAL,
            synthesize_briefing_narrative,
        )

        try:
            # standalone=True → the enumerated DM brief (no cards beside it),
            # not the /today header synthesis.
            artifact_text = await synthesize_briefing_narrative(
                today_data=today_data, activity=activity, period_label=period,
                standalone=True, since=since, handled=handled,
                pending_watch_suggestions=pending_suggestions, mode=mode,
            )
        except Exception as exc:
            logger.exception("morning_brief: synthesis failed")
            yield WorkflowEvent(type="error", message=f"Synthesis failed: {exc}")
            return

        if not artifact_text:
            # The shared synthesizer's own quiet-day line, so the empty
            # fallback reads identically to a model-produced quiet brief.
            artifact_text = QUIET_PRINCIPAL

        yield WorkflowEvent(
            type="step_done",
            step_id="synthesize",
            summary=artifact_text.split("\n", 1)[0][:160],
        )

        # ------------------------------------------------------------------ #
        # Artifact + done
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="artifact",
            content=artifact_text,
        )
        yield WorkflowEvent(type="done")

    def sample_inputs(self) -> dict[str, Any] | None:
        return {"period_label": ""}


# Forward-compat: this workflow is used both manually (via the workflow API
# / form) and from the scheduler. The scheduler invocation path lives in
# `openexecutive.scheduler.runner._execute_action` (see the
# `principal_brief_morning` branch).
__all__ = ["BRIEF_KIND", "MorningBriefWorkflow", "MorningBriefInput"]
