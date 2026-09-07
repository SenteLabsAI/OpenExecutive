"""Executive reflection workflow — OE's daily standup with itself.

Once a day (before the morning brief), the Executive walks its own
org state and decides what to act on. This operationalizes the
``## When You Notice Something on Your Own`` rule from the persona
(Shift 1) and the ``## Choosing Who to Tell`` audience-selection
rule (Shift 3a) — by giving OE actual tools it can fire WITHOUT a
user prompt.

Pipeline:
  1. ``gather_signals`` — pull /today, /today/activity, nudge-engine
     candidates, and recent alerts.
  2. ``decide`` — single LLM call with the full outbound toolkit
     (schedule_followup, suggest_workflow, send_*_dm,
     send_department_message, send_company_broadcast, create_alert,
     people-roster mutations). The LLM decides per signal:
       (a) act now within authority by calling a tool,
       (b) notify the right audience (chooses the tool that hits the
           smallest correct audience per persona rules),
       (c) raise it in the morning brief (passive — no tool call),
       (d) ignore (passive).
  3. ``emit_artifact`` — render a Markdown summary of what was
     decided and what was done.

The artifact is stored via ``complete_run`` but NOT delivered to the
principal — the morning brief picks up reflection-emitted scheduled
follow-ups via its existing ``/today/activity`` query, so the
principal sees the effects of reflection through the brief.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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


class ExecutiveReflectionInput(BaseModel):
    """Inputs for an executive reflection run.

    Both fields are optional — the scheduler fires this with no args.
    """

    period_label: str = Field(
        default="",
        description="Human-readable period label. Auto-filled when blank.",
    )


# Maximum iterations of the tool-use loop INSIDE the reflection workflow.
# The reflection model can call a few tools, see the results, then
# emit a final summary. 4 iterations is enough for "DM Sara + schedule
# follow-up + flag an alert + summarize" without spiralling into a
# multi-turn agentic loop.
_MAX_REFLECTION_ITERATIONS = 4
# Cap the LLM response size — the reflection prompt is structured so
# the model emits one summary turn after all tool calls.
_MAX_REFLECTION_TOKENS = 2000


# Per-channel DM tools, in display order. The "single human owns it"
# audience rule is built per-run — see ``_build_reflection_system``. DMs go
# through ``message_person(person_id, text)`` (the server resolves the
# recipient's real channel id), so the prompt never names a raw per-channel
# send tool and the model never supplies — or fabricates — a channel id. The
# raw send_*_dm tools are withheld from the reflection toolkit too (see the
# exclusion in ``run``), matching the research synthesis path.
def _reflection_dm_rule(configured: set[str], has_roster: bool = True) -> str:
    """The "single human owns it" bullet — routes DMs via message_person when
    any channel is configured, or states none is. ``has_roster`` controls
    whether to take a person_id from the in-turn roster or fall back to
    lookup_person (when the roster is empty)."""
    if configured & {"slack", "discord", "telegram"}:
        if has_roster:
            return (
                "  • Single human owns it → DM that Person via "
                "`message_person(person_id, text)` using a person_id listed "
                "under YOUR TEAM in the turn. Do NOT call `lookup_person` — the "
                "roster is already provided; the system routes to their real "
                "channel, so never pass a channel id yourself.\n"
            )
        return (
            "  • Single human owns it → DM that Person via "
            "`message_person(person_id, text)` — call `lookup_person` to "
            "resolve their person_id first; the system routes to their real "
            "channel, so never pass a channel id yourself.\n"
        )
    # No DM channel configured — don't advertise one.
    return (
        "  • Single human owns it → no direct-message channel is "
        "configured; use `send_department_message` (it falls back to "
        "DMing the head) or raise it in the morning brief. Do NOT "
        "attempt a DM.\n"
    )


def _build_reflection_system(configured: set[str], has_roster: bool = True) -> str:
    """Build the reflection system prompt for the channels actually
    configured, so the audience rule never names an unavailable DM
    channel. ``has_roster`` controls whether the DM rule takes a person_id
    from the in-turn roster or falls back to lookup_person."""
    return (
        "You are the user's Executive on your morning solo standup. You "
        "are reviewing your own org state — what departments did "
        "overnight, what's pending decisions, who's been waiting on "
        "someone, what alerts are open, and what external conditions "
        "moved (stock, news, vendor status, competitor changelogs) — and "
        "deciding what to act on BEFORE the principal opens their "
        "morning brief.\n\n"
        "Audience choice rule (from `## Choosing Who to Tell` in your "
        "persona): smallest audience that owns the matter.\n"
        + _reflection_dm_rule(configured, has_roster)
        + "  • Department-scoped, no single owner → `send_department_message` "
        "with the dept slug + integration. Falls back to DMing the head "
        "when no channel is configured.\n"
        "  • Company-wide, no clear owner → `send_company_broadcast`.\n"
        "  • Board / comp / legal → DM the scope-holder ONLY. Never "
        "broadcast.\n\n"
        "Decision rule (from `## When You Notice Something on Your "
        "Own`): act on small things within authority; propose to a "
        "Person for big calls (spend, hire, board); say so plainly when "
        "you don't know.\n\n"
        "Cross-signal synthesis: look for patterns that connect signals "
        "from different sources before notifying. A competitor "
        "announcement + own stock move + support-ticket spike likely "
        "tell ONE story, not three — synthesize first, then send one "
        "message rather than three. When you cite an EXTERNAL signal, "
        "include the source's provenance_url in the message so the "
        "principal can verify in one click.\n\n"
        "Per signal in the input, decide one of:\n"
        "  (a) ACT NOW — call a tool to execute a follow-up, a DM, a "
        "broadcast, a workflow suggestion, or an alert.\n"
        "  (b) NOTIFY the right audience by calling the right outbound "
        "tool.\n"
        "  (c) RAISE IN MORNING BRIEF — passive: don't call a tool, just "
        "note in your final summary that the principal should see this.\n"
        "  (d) IGNORE — quiet signals don't need action.\n\n"
        "When you're done calling tools, emit a SHORT Markdown summary "
        "(≤200 words) of what you did and what you flagged for the "
        "morning brief. Format:\n\n"
        "  **Acted on:** (bullets — one per tool call you made)\n"
        "  **Flagged for the brief:** (bullets — anything to surface "
        "without acting on it yet)\n"
        "  **Quiet:** (one line — how much you ignored, e.g. 'Nothing "
        "else worth acting on this morning.')\n\n"
        "Skip headers for sections with no content. Be terse — this is "
        "an internal note, not a board memo."
    )


def _render_reflection_context(
    *,
    period_label: str,
    today_data: dict[str, Any],
    activity: list[dict[str, Any]],
    recent_alerts: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
) -> str:
    """Pack /today + activity + open alerts + external signals into a
    single user-turn block for the LLM to reason over.

    Research output is NOT a separate block — the executive_research
    workflow routes findings via DMs / alerts / watchlist additions
    in real time, so they already show up in `activity`, `recent_alerts`,
    or `proposals` once they've fired. There is no "awaiting" state to
    surface here.
    """
    parts: list[str] = [f"PERIOD: {period_label}\n"]

    depts = today_data.get("departments", [])
    at_risk = [d for d in depts if d.get("at_risk_count", 0) or d.get("off_track_count", 0)]
    if at_risk:
        parts.append("DEPARTMENTS WITH RISK:")
        for d in at_risk:
            parts.append(
                f"- {d['title']} (slug={d['slug']}): "
                f"at_risk={d.get('at_risk_count', 0)} "
                f"off_track={d.get('off_track_count', 0)} "
                f"awaiting={d.get('awaiting_count', 0)} "
                f"authority={d.get('authority_level', 'propose_only')}"
            )
        parts.append("")

    proposals = today_data.get("proposals", [])
    if proposals:
        parts.append("PROPOSALS AWAITING DECISION:")
        for p in proposals[:10]:
            parts.append(f"- alert_id={p.get('alert_id')}: {p.get('headline', '')[:160]}")
        parts.append("")

    people = today_data.get("people", [])
    awaiting = [p for p in people if p.get("awaiting_count", 0)]
    if awaiting:
        parts.append("PEOPLE WAITING ON SOMEONE:")
        for p in awaiting:
            parts.append(
                f"- {p.get('full_name', '')} (id={p.get('id')}, "
                f"role={p.get('role', '')}): "
                f"{p.get('awaiting_count', 0)} awaiting, "
                f"SLA {p.get('soonest_sla_at', 'unset')}"
            )
        parts.append("")

    if activity:
        parts.append("RECENT EXECUTIVE ACTIVITY (most recent first):")
        for item in activity[:15]:
            parts.append(
                f"- [{item.get('at', '')[:10]}] {item.get('kind', 'action')}: "
                f"{item.get('summary', '')[:140]}"
            )
        parts.append("")

    if recent_alerts:
        parts.append("RECENT OPEN ALERTS:")
        for a in recent_alerts[:10]:
            parts.append(
                f"- [{a.get('severity', '?')}] {a.get('headline', '')[:140]} "
                f"tags={','.join(a.get('topic_tags') or [])}"
            )
        parts.append("")

    if external_signals:
        parts.append("EXTERNAL SIGNALS OVERNIGHT (last 24h):")
        for s in external_signals[:10]:
            # Outcome is None for unprocessed rows, otherwise one of
            # the OUTCOME_* constants. Showing it lets the model know
            # which signals actually surfaced (alerted) vs were
            # suppressed (dry_run / below_floor / dup) — it should
            # mostly care about the alerted ones.
            outcome = s.get("processed_outcome") or "pending"
            parts.append(
                f"- [{s.get('severity_hint', '?')} {s.get('source_kind', '?')}] "
                f"{(s.get('normalized_summary') or '')[:160]} "
                f"-> {outcome}"
            )
        parts.append("")

    if len(parts) == 1:
        parts.append("(No signals worth acting on this period.)")

    return "\n".join(parts)


# Re-export the synthesis helpers under the private aliases this module
# used to define. Both helpers now live in `workflows/_synthesis.py` so
# `executive_research` can reuse them without an underscore-import
# tripwire; this re-export keeps any callers that still import the
# private names from this module working.
from openexecutive.providers.translator import reasoning_replay_block  # noqa: E402
from openexecutive.workflows._synthesis import (  # noqa: E402
    execute_tool_calls as _execute_tool_calls,
)
from openexecutive.workflows._synthesis import (  # noqa: E402
    extract_artifact_from_response as _extract_artifact_from_response,
)


class ExecutiveReflectionWorkflow(Workflow):
    name = "executive_reflection"
    title = "Executive Reflection"
    description = (
        "OE's daily solo standup. Walks the org state — at-risk goals, "
        "pending proposals, awaiting humans, recent activity, open "
        "alerts — and decides per signal: act now, notify the right "
        "audience, raise in the morning brief, or ignore. Fires "
        "automatically via the scheduler ~30 minutes before the "
        "morning brief; can be invoked manually for testing. The "
        "first workflow in the registry that OE runs ON ITSELF, not "
        "on a department or for the principal."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 2

    def input_model(self) -> type[BaseModel]:
        return ExecutiveReflectionInput

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="gather_signals",
                title="Gather org signals",
                description="Pull /today, /today/activity, and recent open alerts.",
            ),
            WorkflowStepDef(
                id="decide",
                title="Decide per signal",
                description=(
                    "Walk the signals and pick: act now via a tool, "
                    "notify the right audience, raise in morning "
                    "brief, or ignore."
                ),
            ),
            WorkflowStepDef(
                id="emit_artifact",
                title="Emit summary",
                description="Render a short Markdown summary for the audit trail.",
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, ExecutiveReflectionInput)
        period = inputs.period_label or datetime.now(UTC).strftime("%Y-%m-%d")

        # ------------------------------------------------------------------ #
        # Step 1: gather signals
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="gather_signals",
            step_title="Gather org signals",
        )

        from openexecutive.alerts import store as alert_store
        from openexecutive.api.routes import today as today_route

        try:
            today_data = today_route._build_today().model_dump()
        except Exception:
            logger.exception("reflection: /today aggregation failed")
            today_data = {"departments": [], "people": [], "proposals": []}

        try:
            activity = [
                item.model_dump()
                for item in today_route._build_activity(limit=20).items
            ]
        except Exception:
            logger.exception("reflection: /today/activity aggregation failed")
            activity = []

        try:
            recent_alerts_objs = alert_store.list_alerts(status="unread", limit=10)
            recent_alerts = [
                {
                    "severity": a.severity,
                    "headline": a.headline,
                    "topic_tags": a.topic_tags,
                }
                for a in recent_alerts_objs
            ]
        except Exception:
            logger.exception("reflection: alerts list failed")
            recent_alerts = []

        # External signals (last 24h, alerted only) — surface alongside
        # org state so the model can synthesize across own-system and
        # outside-world signals on the same pass. Restricting to
        # `processed_outcome='alerted'` keeps the prompt budget on
        # signals that actually surfaced; dry_run / below_floor / dup
        # rows would otherwise dominate the section on quiet days.
        try:
            from openexecutive.monitoring import store as monitoring_store
            external_signals = monitoring_store.list_recent_signals(
                since=datetime.now(UTC) - timedelta(hours=24),
                outcomes=["alerted"],
                limit=10,
            )
        except Exception:
            logger.exception("reflection: external signals fetch failed")
            external_signals = []

        yield WorkflowEvent(
            type="step_done",
            step_id="gather_signals",
            summary=(
                f"depts={len(today_data.get('departments', []))} "
                f"proposals={len(today_data.get('proposals', []))} "
                f"activity={len(activity)} "
                f"alerts={len(recent_alerts)} "
                f"external={len(external_signals)}"
            ),
        )

        # ------------------------------------------------------------------ #
        # Step 2: decide (LLM + tool use)
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="decide",
            step_title="Decide per signal",
        )

        # Reuse the same toolkit the chat path exposes — the Executive's
        # full outbound surface, minus specialist_consult (the reflection
        # workflow doesn't analyse, it acts). We avoid an import-time
        # cycle by deferring this.
        from openexecutive.config import get_settings
        from openexecutive.orchestrator.executive import (
            _ALL_SKILL_HANDLERS,
            _ALL_SKILL_TOOLS,
        )
        from openexecutive.orchestrator.schedule_tools import (
            configured_integrations,
            current_session,
            filter_tools_for_configured_channels,
        )
        from openexecutive.orchestrator.session import Session
        from openexecutive.people.store import list_people
        from openexecutive.providers import get_provider
        from openexecutive.workflows.executive_research import _render_team_roster

        # Load the roster once: it seeds the anti-spam guard AND is rendered
        # into the turn so reflection DMs by person_id directly (no
        # lookup_person round-trips — the "looks up, never routes" failure).
        try:
            people = list_people()
        except Exception:
            logger.exception("reflection: list_people failed — guard runs with empty seen set")
            people = []

        user_content = _render_reflection_context(
            period_label=period,
            today_data=today_data,
            activity=activity,
            recent_alerts=recent_alerts,
            external_signals=external_signals,
        )
        roster = _render_team_roster(people)
        if roster:
            user_content = roster + "\n" + user_content

        # Seed a synthetic Session into the `current_session` ContextVar
        # so schedule_followup's anti-spam guard accepts the reflection's
        # tool calls. Without this, the guard reads `seen=None` and falls
        # through (allowing any (channel, channel_ref)). Seeding from the
        # People roster means reflection can only schedule follow-ups to
        # channels that correspond to a real Person — same invariant the
        # chat path enforces via session.seen_channel_refs.
        seen: set[tuple[str, str]] = set()
        for person in people:
            if person.slack_user_id:
                seen.add(("slack_dm", person.slack_user_id))
            if person.discord_user_id:
                seen.add(("discord_dm", person.discord_user_id))
            if person.telegram_chat_id:
                seen.add(("telegram", person.telegram_chat_id))
            if person.email:
                seen.add(("email", person.email))

        reflection_session = Session(seen_channel_refs=seen)
        ctx_token = current_session.set(reflection_session)

        # Sort tools by name for prompt-cache stability (same convention
        # as the chat orchestrator). Reflection runs ad-hoc so prompt
        # caching is unlikely to hit, but the sort keeps tool ordering
        # deterministic for any cache that does land.
        # Withhold the raw per-channel DM tools: DMs go through message_person
        # (server resolves the channel id), so the model can't pass — or
        # fabricate — a channel id. Mirrors executive_research's synthesis.
        _excluded_dm = {"send_slack_dm", "send_discord_dm", "send_telegram_message"}
        tools = sorted(
            (t for t in _ALL_SKILL_TOOLS if t["name"] not in _excluded_dm),
            key=lambda t: t["name"],
        )

        settings = get_settings()
        # Only offer channels that are actually configured, so reflection
        # can't route into a DM tool whose integration has no token.
        configured = configured_integrations(settings)
        tools = filter_tools_for_configured_channels(tools, settings)
        # Build the system prompt for the SAME configured set, so the
        # audience rule never names a DM channel the model can't use.
        reflection_system = _build_reflection_system(configured, bool(roster))
        model = settings.routing_model
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_content}
        ]
        tool_call_summaries: list[dict[str, Any]] = []
        final_text = ""

        try:
            for iteration in range(1, _MAX_REFLECTION_ITERATIONS + 1):
                try:
                    response = await get_provider(model).messages_create(
                        model=model,
                        max_tokens=_MAX_REFLECTION_TOKENS,
                        system=reflection_system,
                        tools=tools,  # type: ignore[arg-type]
                        messages=messages,  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    logger.exception("reflection: provider call failed iter=%d", iteration)
                    yield WorkflowEvent(
                        type="error", message=f"Reflection LLM call failed: {exc}"
                    )
                    return

                iter_summaries = await _execute_tool_calls(response, _ALL_SKILL_HANDLERS)
                tool_call_summaries.extend(iter_summaries)

                text = _extract_artifact_from_response(response)
                # Capture the latest non-empty text on every iteration so the
                # iteration-cap-exhaustion path still gets the model's
                # mid-iteration narrative, not just the synth fallback.
                if text:
                    final_text = text

                stop_reason = getattr(response, "stop_reason", "")
                if stop_reason != "tool_use" or not iter_summaries:
                    # Model is done — either it emitted text and no tools, or
                    # it returned a text-only summary on a follow-up turn.
                    break

                # Build the next user turn carrying tool_result blocks so the
                # model sees what it called succeeded/failed and can summarize.
                assistant_blocks: list[dict[str, Any]] = []
                for block in response.content:
                    block_type = getattr(block, "type", "")
                    if block_type == "text":
                        assistant_blocks.append({"type": "text", "text": getattr(block, "text", "")})
                    elif block_type == "tool_use":
                        assistant_blocks.append({
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": getattr(block, "input", {}) or {},
                        })
                    elif (replay := reasoning_replay_block(block)) is not None:
                        # OpenRouter reasoning continuity across tool iterations.
                        assistant_blocks.append(replay)
                messages.append({"role": "assistant", "content": assistant_blocks})

                # Pair each tool_use with the matching summary by index.
                # Both lists are derived from the same response.content
                # traversal so the alignment is stable.
                tool_results: list[dict[str, Any]] = []
                summary_iter = iter(iter_summaries)
                for block in response.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    summary = next(summary_iter, None)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": getattr(block, "id", ""),
                        "content": (
                            summary["result_preview"] if summary
                            else "no result captured"
                        ),
                    })
                messages.append({"role": "user", "content": tool_results})
        finally:
            # Always release the reflection session ContextVar so the
            # tool guard goes back to its default for any subsequent
            # caller in this process (tests, other workflow runs).
            current_session.reset(ctx_token)

        yield WorkflowEvent(
            type="step_done",
            step_id="decide",
            summary=(
                f"iter={iteration} tools_fired={len(tool_call_summaries)} "
                f"ok={sum(1 for s in tool_call_summaries if s['ok'])}"
            ),
        )

        # ------------------------------------------------------------------ #
        # Step 3: emit artifact
        # ------------------------------------------------------------------ #
        yield WorkflowEvent(
            type="step_start",
            step_id="emit_artifact",
            step_title="Emit summary",
        )

        if not final_text:
            # Model exhausted iterations without a final summary. Synth
            # a minimal artifact so the run record isn't empty.
            ok_count = sum(1 for s in tool_call_summaries if s["ok"])
            final_text = (
                f"_Reflection complete — {ok_count}/{len(tool_call_summaries)} "
                f"tool calls succeeded; no narrative summary returned._"
            )

        # Append a structured tool-call log so the audit trail shows
        # exactly what was fired during this reflection.
        if tool_call_summaries:
            log_lines = ["", "---", "_Tool calls this run:_"]
            for s in tool_call_summaries:
                mark = "✓" if s["ok"] else "✗"
                log_lines.append(
                    f"- {mark} `{s['tool']}` — {s['result_preview']}"
                )
            final_text = final_text + "\n".join(log_lines) if final_text.endswith("\n") else final_text + "\n" + "\n".join(log_lines)

        yield WorkflowEvent(
            type="step_done",
            step_id="emit_artifact",
            summary=f"{len(tool_call_summaries)} actions logged",
        )

        yield WorkflowEvent(type="artifact", content=final_text)
        yield WorkflowEvent(type="done")

    def sample_inputs(self) -> dict[str, Any] | None:
        return {"period_label": ""}


__all__ = ["ExecutiveReflectionWorkflow", "ExecutiveReflectionInput"]
