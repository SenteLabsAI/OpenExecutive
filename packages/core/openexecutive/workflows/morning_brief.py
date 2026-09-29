"""Morning brief workflow — a short proactive briefing to the principal.

Renders a one-screen Markdown summary covering:
  • At-risk department goals
  • Proposals awaiting the principal's decision
  • Anything OE acted on since the last brief
  • The top decision the principal needs to make today
  • In solo mode: what is due this week, and the top three to focus on today
    (``briefing.top_three`` — with a free slot for each when a calendar can
    be read)

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

import asyncio
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, ClassVar

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

# Set by the scheduler around a run it will deliver to the principal alone
# (`scheduler.runner._run_principal_brief`). Only then — or on the principal's
# own verified chat turn in a conversation only they can read (the web chat,
# a DM, a private Telegram chat — never a shared channel, where the reply is
# posted for everyone) — does the brief read what is private to them: their
# contacts' mail and alerts, their drafts, their chats, their calendar.
# A run anyone else starts (a teammate in chat, the workflow API) reads what
# everyone may see, as before.
PRINCIPAL_DELIVERY: ContextVar[bool] = ContextVar("morning_brief_principal_delivery", default=False)


def _differs(own: Any, shared: Any) -> bool:
    """Whether the principal's read of their live world says anything the
    shared read does not — the keys (top groups) and every count the brief
    renders, so a private row in the tail ("…and N more") counts too."""
    return (
        own.keys != shared.keys
        or own.inbound_total != shared.inbound_total
        or len(own.stuck) != len(shared.stuck)
        or own.drafts != shared.drafts
        or len(own.conversations) != len(shared.conversations)
    )


def _private_ok() -> bool:
    if PRINCIPAL_DELIVERY.get():
        return True
    try:
        from openexecutive.delegation.settings import private_conversation
        from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
        return (
            session is not None
            and is_principal_on_verified_surface(session)
            and private_conversation(session)
        )
    except Exception:
        logger.exception("morning_brief: principal check failed — private rows stay out")
        return False


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
    # Solo reads the principal's commitments and calendar (top three today),
    # so only they may run it from chat there. Team is unchanged.
    principal_only_modes: ClassVar[frozenset[str]] = frozenset({"solo"})

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
        from openexecutive.briefing.narrative_cache import local_today

        now = datetime.now(UTC)
        # The principal's local date, not UTC's — east of UTC an 08:00 brief
        # was dated yesterday.
        period = inputs.period_label or local_today(now)

        # ------------------------------------------------------------------ #
        # Step 1: gather context
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="load_context",
            step_title="Gather today's state",
        )

        from openexecutive.alerts.models import PRIVATE_ALERT_TAG
        from openexecutive.api.routes import today as today_route
        from openexecutive.briefing import brief_state
        from openexecutive.briefing.live_signals import gather_live_signals, refresh_calendar
        from openexecutive.memory.workspace_settings import effective_workspace_mode
        from openexecutive.orchestrator.schedule_tools import current_session

        # The window is "since the last brief I actually delivered" (24 h on
        # a cold store), so "what changed" is a real delta, not the latest N.
        since = brief_state.since_for(BRIEF_KIND)
        # Solo / team: the solo brief speaks to the principal (goals by area,
        # no people waiting). A caller's session override (evals) wins.
        mode = effective_workspace_mode(current_session.get())
        private_ok = _private_ok()

        try:
            today_response = today_route._build_today(include_private=private_ok)
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
        top_three_calendar = False
        if mode == "solo":
            # What the principal owns that is due this week or overdue — their
            # dated commitments. It lands here even when no channel reaches
            # them for a nudge. Never raises (reads as empty on failure).
            from openexecutive.attunement.open_loops import principal_due_soon
            from openexecutive.briefing.top_three import build_top_three

            today_data["due_soon"] = principal_due_soon()
            # Top three today: picked from goals at risk, commitments due and
            # active projects, each with a free slot when a calendar can be
            # read (one short call). Never raises; without a calendar there
            # are no slots and no calendar block.
            top_three, calendar = await build_top_three(today_data["due_soon"])
            if top_three:
                today_data["top_three"] = top_three
            if calendar is not None:
                today_data["today_calendar"] = calendar
                top_three_calendar = True

        # The principal's world since the last brief: who wrote, what got
        # stuck, their chats, and — unless the solo top three already listed
        # it — the day's calendar, read fresh (team mode never read one). The
        # calendar is the principal's own, so only on a run for them.
        events = None
        if private_ok and not top_three_calendar:
            events = await refresh_calendar(now, max_age=0)
        live = gather_live_signals(
            since, now=now, include_private=private_ok,
            calendar=events, use_cached_calendar=False,
        )
        # The reflection's notes are written from what everyone may see (it
        # reads the board without private rows), so any run may carry them.
        reflection_flags = brief_state.reflection_flags_since(since)
        # Did this brief actually draw on anything private to the principal?
        # Then its text stays out of the shared run history (the principal
        # gets it where it is delivered).
        private_used = private_ok and (
            live.calendar is not None
            or any(
                str(t).lower() == PRIVATE_ALERT_TAG
                for p in today_data["proposals"] for t in p.get("topic_tags") or []
            )
            or _differs(live, gather_live_signals(
                since, now=now, include_private=False, use_cached_calendar=False,
            ))
        )

        try:
            activity_response = today_route._build_activity(
                20, since=since, exclude_workflows=today_route.RHYTHM_WORKFLOWS,
            )
            activity = [item.model_dump() for item in activity_response.items]
        except Exception:
            logger.exception("morning_brief: /today/activity aggregation failed")
            activity = []

        handled = brief_state.handled_since(since)
        pending_suggestions = brief_state.pending_watch_suggestions()
        # Corrections teammates made since the last brief: the principal's
        # FYI (memory.facts), in force now or waiting for their approval.
        from openexecutive.memory.facts import render_teammate_changes

        teammate_changes = await asyncio.to_thread(render_teammate_changes, since)
        fingerprint = brief_state.build_brief_fingerprint(
            today_data=today_data, activity=activity, handled=handled, since=since,
            pending_watch_suggestions=pending_suggestions, mode=mode,
            live_keys=live.keys, reflection_flags=reflection_flags,
            teammate_changes=teammate_changes,
        )
        previous = brief_state.last_delivered(BRIEF_KIND)
        suppressed = (
            brief_state.suppress_unchanged_enabled()
            and not inputs.force_full
            and previous is not None
            and previous.input_hash == fingerprint
        )
        logger.info(
            "morning_brief: context since=%s proposals=%d activity=%d handled=%d "
            "inbound=%d stuck=%d drafts=%d conversations=%d calendar=%s "
            "reflection_flags=%s private=%s private_used=%s suppressed=%s",
            since.isoformat()[:16], len(today_data["proposals"]), len(activity),
            len(handled), live.inbound_total, len(live.stuck), live.drafts,
            len(live.conversations),
            "none" if live.calendar is None else len(live.calendar),
            bool(reflection_flags), private_ok, private_used, suppressed,
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
                "private_to_principal": private_used,
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

        from openexecutive.briefing.grounding import ground_brief
        from openexecutive.briefing.narrative import (
            QUIET_PRINCIPAL,
            STANDALONE_BRIEF_SOLO_SYSTEM,
            STANDALONE_BRIEF_SYSTEM,
            render_briefing_context,
            synthesize_briefing_narrative,
        )

        try:
            # Rendered here (not inside the synthesizer) so the grounding pass
            # checks the brief against exactly the text the model read.
            rendered = render_briefing_context(
                period_label=period, today_data=today_data, activity=activity,
                since=since, handled=handled,
                pending_watch_suggestions=pending_suggestions, mode=mode,
                live=live, live_window="since the last brief",
                reflection_flags=reflection_flags, teammate_changes=teammate_changes,
            )
            # standalone=True → the enumerated DM brief (no cards beside it),
            # not the /today header synthesis.
            artifact_text = await synthesize_briefing_narrative(
                today_data=today_data, activity=activity, period_label=period,
                standalone=True, since=since, handled=handled,
                pending_watch_suggestions=pending_suggestions, mode=mode,
                live=live, live_window="since the last brief",
                reflection_flags=reflection_flags, rendered_context=rendered,
            )
        except Exception as exc:
            logger.exception("morning_brief: synthesis failed")
            yield WorkflowEvent(type="error", message=f"Synthesis failed: {exc}")
            return

        if not artifact_text:
            # The shared synthesizer's own quiet-day line, so the empty
            # fallback reads identically to a model-produced quiet brief.
            artifact_text = QUIET_PRINCIPAL

        # Nobody reads this before it ships: hold back any line naming a
        # person or figure the context doesn't hold, and cite the figures.
        artifact_text, grounding = await ground_brief(
            artifact_text, context=rendered, kind=BRIEF_KIND, private=private_used,
            system=STANDALONE_BRIEF_SOLO_SYSTEM if mode == "solo" else STANDALONE_BRIEF_SYSTEM,
            surface="morning brief",
        )
        held = len(grounding.held) if grounding and grounding.mode == "enforce" else 0

        yield WorkflowEvent(
            type="step_done",
            step_id="synthesize",
            summary=artifact_text.split("\n", 1)[0][:160]
            + (f" (grounding: {held} line(s) held back)" if held else ""),
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
