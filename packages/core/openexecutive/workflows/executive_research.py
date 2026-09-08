"""ExecutiveResearchWorkflow — specialists research, Executive routes.

Mirrors the architecture of ``executive_reflection.py``: a research
phase produces structured findings, then the Executive enters a tool-
use loop with its full outbound toolkit (``_ALL_SKILL_HANDLERS``) and
decides per finding whether to:

  - DM a specific Person via ``message_person`` (pass the ``person_id``
    from ``lookup_person``; the server resolves their real channel id)
  - Message a department channel via ``send_department_message``
  - Surface as a briefing card via ``create_alert``
  - Add to the external monitor via ``add_watchlist_entry`` (only when
    the finding is a "watch this going forward" signal — most aren't)
  - Schedule a follow-up via ``schedule_followup``
  - Propose a deeper workflow via ``suggest_workflow``
  - Ignore (passive — no tool call)

The Executive — not the workflow — owns routing decisions. The
specialists surface, the Executive acts. Same pattern executive_reflection
already uses on org-state signals; the only new piece is the upstream
research phase that produces the findings.

Pipeline:
  1. ``gather_context`` — load company profile, active initiatives,
     existing watchlist (so specialists don't re-propose what's already
     monitored).
  2. ``research_specialists`` — fan out via asyncio.gather to seven
     specialists (cso, cfo, cmo, coo, chro, cpo, gc). Each runs with
     web_search + the ``emit_research_findings`` tool.
  3. ``dedup`` — light cross-specialist dedup so the Executive doesn't
     see the same fact twice.
  4. ``executive_synthesis`` — Executive's tool-use loop. Reads
     findings, fires the right tools per finding.
  5. ``emit_artifact`` — short Markdown summary of "what I researched
     + what I did about it".
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.monitoring.research.dedup import dedup_findings
from openexecutive.monitoring.research.models import (
    ResearchFinding,
    ResearchRunSummary,
)
from openexecutive.monitoring.research.specialist_research import (
    research_one_specialist,
)
from openexecutive.providers.translator import reasoning_replay_block
from openexecutive.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowSection,
    WorkflowStepDef,
)

logger = logging.getLogger(__name__)

# Specialists the workflow fans out to. Kept module-scoped so tests can
# patch / shrink without monkeypatching half the workflow internals.
RESEARCH_SPECIALISTS: tuple[str, ...] = (
    "cso", "cfo", "cmo", "coo", "chro", "cpo", "gc",
)

# Synthesis loop budget. A real routing run needs at least three turns:
# (1) look recipients up (lookup_person), (2) fire the resolved DMs /
# alerts, (3) emit the wrap-up summary. Two iterations starved that
# middle step — the model spent iteration 1 on lookups and never reached
# the routing turn. Beyond three, the model is usually spiralling.
_MAX_SYNTHESIS_ITERATIONS = 3
_MAX_SYNTHESIS_TOKENS = 2000

# Hard ceiling on how many outbound tools the Executive synthesis can
# fire per run. Without this cap a single run can blast 50+ DMs +
# alerts at the principal — the original failure mode (52 alerts from
# one research run on round_2). The cap is enforced both via the
# system prompt instruction and as a runtime gate in the synthesis
# loop — once the model exceeds it, subsequent tool_use blocks are
# rejected with an "over budget" tool_result and the loop terminates.
_MAX_ROUTING_TOOLS_PER_RUN = 5

# Read-only context tools the Executive calls to PREPARE a routing
# decision (resolve a department head, scan the roster, recall what it
# knows about a person). These are NOT outbound actions, so they must
# not count against the routing budget above and must never be refused
# for being "over budget" — otherwise the Executive spends its whole
# budget looking people up and never fires the DM the lookup was for
# (the "looks up people but never routes" bug). Passed to
# ``execute_tool_calls`` as ``free_tools`` and excluded from the
# budget-accounting counts via ``_routing_ok``.
_NON_ROUTING_TOOLS: frozenset[str] = frozenset({
    "lookup_person",
    "list_people",
    "ask_about_person",
})


def _routing_ok(calls: list[dict[str, Any]]) -> int:
    """Count successful OUTBOUND routing calls — the ones the budget
    governs. Read-only lookups (``_NON_ROUTING_TOOLS``) are excluded so
    they neither consume nor terminate the routing budget."""
    return sum(
        1
        for t in calls
        if t.get("ok") and t.get("tool") not in _NON_ROUTING_TOOLS
    )


# Dedicated watchlist-analysis pass (runs AFTER routing). Its own budget,
# separate from the routing budget above, so adding monitors never competes
# with urgent DMs/alerts — the routing pass treats watchlist adds as rare,
# this pass treats them as the whole point.
_MAX_WATCHLIST_ADDS_PER_RUN = 4
_MAX_WATCHLIST_ITERATIONS = 2


def _routing_option_a(configured: set[str], has_roster: bool = True) -> str:
    """Option (a) of the routing menu.

    DMs go through ``message_person(person_id, text)`` — the server resolves
    the recipient's real channel id, so the model never supplies (or
    fabricates) a channel id. Available whenever ANY DM channel is configured.
    When the YOUR TEAM roster is present in the turn (``has_roster``), the
    model takes a person_id directly and must NOT call lookup_person; when it
    is absent (empty roster), lookup_person is the only way to get a person_id,
    so it is allowed.
    """
    if configured & {"slack", "discord", "telegram"}:
        if has_roster:
            return (
                "  (a) **DM a single named Person** via "
                "message_person(person_id, text) — use a person_id listed "
                "under YOUR TEAM in the findings turn and pass ONLY that "
                "person_id. Do NOT call lookup_person; the roster is already "
                "provided. The system routes to their real channel; never pass "
                "a channel id, handle, or snowflake yourself. PREFERRED for "
                "tactical items.\n"
            )
        return (
            "  (a) **DM a single named Person** via "
            "message_person(person_id, text) — call lookup_person to resolve "
            "the person's person_id first, then pass ONLY that person_id. The "
            "system routes to their real channel; never pass a channel id, "
            "handle, or snowflake yourself. PREFERRED for tactical items.\n"
        )
    # No DM channel configured — don't advertise one. Steer to the
    # channel-free surfaces so the model doesn't try (and narrate) a DM
    # it can't send.
    return (
        "  (a) **DM a single named Person** — UNAVAILABLE: no "
        "direct-message channel is configured on this deployment. Do "
        "NOT attempt a DM; use a department message (b) or briefing "
        "card (c) instead.\n"
    )


def _build_synthesis_system(configured: set[str], has_roster: bool = True) -> str:
    """Build the synthesis system prompt for the channels actually
    configured, so option (a) never names an unavailable DM channel.
    ``has_roster`` controls whether option (a) tells the model to take a
    person_id from the in-turn roster or to fall back to lookup_person."""
    return (
        "You are the user's Executive reviewing research findings from "
        "your specialist council.\n\n"
        "## DEFAULT IS IGNORE\n\n"
        "Most findings DO NOT warrant a tool call. Quiet is the right "
        "answer for the majority of findings. Only route a finding when "
        "all three are true:\n"
        "  (i)   it is specifically actionable for this company today,\n"
        "  (ii)  there is a verifiable next step a named human can take "
        "in <15 minutes, AND\n"
        "  (iii) the audience would be worse off without seeing it now.\n"
        "If any one of those is false, IGNORE — log it in your summary "
        "as quiet.\n\n"
        f"## ROUTING BUDGET: {_MAX_ROUTING_TOOLS_PER_RUN}\n\n"
        f"You may fire AT MOST {_MAX_ROUTING_TOOLS_PER_RUN} outbound tool "
        "calls in this run combined (DMs, alerts, watchlist adds, "
        "follow-ups, workflow suggestions). The runtime enforces this "
        "ceiling — calls beyond the budget will be rejected. Choose the "
        "few that genuinely deserve the cost. Read-only lookups "
        "(`lookup_person`, `list_people`) do NOT count against this "
        "budget — resolve a recipient freely, then spend the budget on "
        "the message itself.\n\n"
        "## ROUTING OPTIONS\n\n"
        "When you do act, pick the SMALLEST audience that owns the "
        "matter (persona rule). PREFER department-head DMs over briefing "
        "cards — the principal's briefing is the most expensive surface "
        "and a finding ending up there means it warrants their direct "
        "attention.\n\n"
        + _routing_option_a(configured, has_roster)
        + "  (b) **Message a department channel** via "
        "send_department_message when no single human owns it.\n"
        "  (c) **Surface as briefing card** via create_alert — RARE. Only "
        "when the principal personally must decide / react and the matter "
        "is materially company-wide. Default away from this.\n"
        "  (d) **Add to the watchlist** via add_watchlist_entry — ONLY "
        "for ongoing-monitor signals (a new competitor ticker, a vendor "
        "status page not yet watched). Most findings are NOT watchlist "
        "material.\n"
        "  (e) **Schedule a follow-up** via schedule_followup for "
        "time-shifted chases.\n"
        "  (f) **Suggest a deeper workflow** via suggest_workflow only "
        "for major events (M&A, fundraising, crisis comms).\n\n"
        "## INVARIANTS\n\n"
        "  - Privacy: board / comp / legal stay per-Person. NEVER "
        "broadcast.\n"
        "  - Cross-finding synthesis: when two findings tell one story, "
        "combine into a SINGLE message — they count as ONE routing "
        "against the budget.\n"
        "  - Cite relevant_urls in any outbound message.\n\n"
        "## SUMMARY\n\n"
        "After your tool calls, emit a SHORT Markdown summary (<=150 "
        "words). Format:\n\n"
        "  **Acted on:** (bullets — one per tool call)\n"
        "  **Quiet:** (one line — N findings reviewed, K ignored as "
        "below the bar)\n\n"
        "Skip headers with no content. Be terse."
    )


_WATCHLIST_SYSTEM = (
    "You are the user's Executive deciding what the company should "
    "MONITOR going forward, based on this run's research findings.\n\n"
    "This is a deliberate, forward-looking pass — distinct from routing "
    "urgent items to people. Your ONLY tool is `add_watchlist_entry`.\n\n"
    "## WHAT BELONGS ON THE WATCHLIST\n\n"
    "An ongoing, externally-observable condition we'd want flagged when it "
    "next changes — e.g. a public competitor's stock, a regulator's or "
    "competitor's RSS/Atom feed, a vendor status page. A one-off event that "
    "is already fully known is NOT watchlist material; the *ongoing source* "
    "behind it might be.\n\n"
    "## RULES\n\n"
    f"  - Add AT MOST {_MAX_WATCHLIST_ADDS_PER_RUN} entries this run. It is "
    "normal to add several; it is also fine to add none.\n"
    "  - `signal_type` MUST be one of: `stock` (a ticker), `rss` (a real "
    "Atom/RSS feed URL), `vendor_status` (a real status-page feed URL).\n"
    "  - `target` MUST be concrete and real: a ticker symbol, or a URL. "
    "PREFER a URL taken from a finding's `urls` list — do NOT invent feed "
    "URLs. If you cannot give a real target, do not add the entry.\n"
    "  - `slug` must be unique kebab-case (e.g. `stock-tsla`, "
    "`rss-acme-blog`).\n"
    "  - SKIP anything already on the current watchlist (listed below).\n"
    "  - Set `route_to_specialist` to the owning specialist when obvious.\n\n"
    "After your tool calls, emit ONE short line: what you added and what you "
    "deliberately skipped. Be terse."
)


class ExecutiveResearchInput(BaseModel):
    """Inputs for a research run. Both optional — the workflow loads
    everything from the company profile by default."""

    note: str = Field(
        default="",
        description=(
            "Optional one-line note added to the research context — "
            "useful for narrowing the run (e.g. 'focus on Series-B "
            "competitor signals')."
        ),
    )


class ExecutiveResearchWorkflow(Workflow):
    name = "executive_research"
    title = "Executive Research"
    description = (
        "Specialists research what's worth knowing for the company; "
        "the Executive routes findings through its outbound toolkit "
        "(DM heads of department, DM principal, create alert, add to "
        "watchlist, schedule follow-up, suggest workflow). Fires "
        "manually via the `run_executive_research` chat tool, "
        "automatically at the end of onboarding, and on a periodic "
        "cron (every WATCHLIST_RESEARCH_INTERVAL_MINUTES — default 120 "
        "— gated by a state-hash skip-if-unchanged check)."
    )
    section = WorkflowSection.OPERATING
    estimated_minutes = 3

    def input_model(self) -> type[BaseModel]:
        return ExecutiveResearchInput

    def steps(self) -> list[WorkflowStepDef]:
        return [
            WorkflowStepDef(
                id="gather_context",
                title="Gather company context",
                description=(
                    "Load company profile, active initiatives, and the "
                    "existing watchlist."
                ),
            ),
            WorkflowStepDef(
                id="research_specialists",
                title="Fan out to specialists",
                description=(
                    "Each of the seven specialists researches their "
                    "domain in parallel — web_search enabled, "
                    "structured findings emitted via tool call."
                ),
            ),
            WorkflowStepDef(
                id="dedup",
                title="Dedup findings",
                description=(
                    "Collapse near-identical findings across "
                    "specialists; consensus boosts severity / confidence."
                ),
            ),
            WorkflowStepDef(
                id="verify",
                title="Verify findings against sources",
                description=(
                    "Scrape each surviving finding's cited URL and confirm "
                    "the page supports the claim; demote / drop findings "
                    "whose source is dead or doesn't back them. No-op unless "
                    "xcrawl + verification are enabled."
                ),
            ),
            WorkflowStepDef(
                id="executive_synthesis",
                title="Executive routes findings",
                description=(
                    "Executive reviews findings with its outbound "
                    "toolkit and fires the right tool per finding "
                    "(DM, message, alert, watchlist add, follow-up, "
                    "workflow suggestion)."
                ),
            ),
            WorkflowStepDef(
                id="emit_artifact",
                title="Emit summary",
                description=(
                    "Short Markdown — what was researched, what was "
                    "routed, what was ignored."
                ),
            ),
        ]

    async def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        assert isinstance(inputs, ExecutiveResearchInput)

        # ------------------------------------------------------------------
        # Step 1: gather_context
        # ------------------------------------------------------------------
        yield WorkflowEvent(
            type="step_start",
            step_id="gather_context",
            step_title="Gather company context",
        )

        from openexecutive.memory.episodic import get_active_initiatives
        from openexecutive.monitoring import store as monitoring_store
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        try:
            profile = load_or_create_profile()
        except Exception:
            logger.exception("research: load_or_create_profile failed")
            yield WorkflowEvent(
                type="error", message="Could not load company profile",
            )
            return

        try:
            initiatives = get_active_initiatives()
        except Exception:
            logger.exception("research: get_active_initiatives failed")
            initiatives = []

        try:
            existing_watchlist = monitoring_store.list_watchlist()
        except Exception:
            logger.exception("research: list_watchlist failed")
            existing_watchlist = []

        research_context = _render_research_context(
            profile=profile,
            initiatives=initiatives,
            existing_watchlist=existing_watchlist,
            note=inputs.note,
        )

        yield WorkflowEvent(
            type="step_done",
            step_id="gather_context",
            summary=(
                f"profile_loaded={profile is not None and not profile.is_empty()} "
                f"initiatives={len(initiatives)} "
                f"existing_watchlist={len(existing_watchlist)}"
            ),
        )

        # ------------------------------------------------------------------
        # Step 2: research_specialists (parallel)
        # ------------------------------------------------------------------
        yield WorkflowEvent(
            type="step_start",
            step_id="research_specialists",
            step_title="Fan out to specialists",
        )

        from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

        async def _run_one(slug: str) -> tuple[str, list[ResearchFinding], str]:
            agent = SPECIALIST_REGISTRY.get(slug)
            if agent is None:
                logger.warning("research: no agent registered for %r", slug)
                return slug, [], "no_agent"
            try:
                findings = await research_one_specialist(
                    slug, agent, research_context,
                )
                return slug, findings, ""
            except Exception as exc:
                logger.exception(
                    "research: specialist %s raised", slug,
                )
                return slug, [], str(exc)[:200]

        results = await asyncio.gather(
            *(_run_one(s) for s in RESEARCH_SPECIALISTS),
            return_exceptions=True,
        )

        findings_by_specialist: dict[str, list[ResearchFinding]] = {}
        per_specialist: list[ResearchRunSummary] = []
        for idx, item in enumerate(results):
            if isinstance(item, BaseException):
                fallback_slug = (
                    RESEARCH_SPECIALISTS[idx]
                    if idx < len(RESEARCH_SPECIALISTS)
                    else f"unknown-{idx}"
                )
                findings_by_specialist[fallback_slug] = []
                per_specialist.append(ResearchRunSummary(
                    specialist=fallback_slug,
                    findings_emitted=0,
                    error=str(item)[:200],
                ))
                continue
            slug, findings, error = item
            findings_by_specialist[slug] = findings
            per_specialist.append(ResearchRunSummary(
                specialist=slug,
                findings_emitted=len(findings),
                error=error,
            ))

        total_emitted = sum(len(v) for v in findings_by_specialist.values())
        yield WorkflowEvent(
            type="step_done",
            step_id="research_specialists",
            summary=f"specialists={len(results)} emitted={total_emitted}",
        )

        # ------------------------------------------------------------------
        # Step 3: dedup
        # ------------------------------------------------------------------
        yield WorkflowEvent(
            type="step_start",
            step_id="dedup",
            step_title="Dedup findings",
        )

        deduped_all: list[ResearchFinding] = dedup_findings(findings_by_specialist)

        yield WorkflowEvent(
            type="step_done",
            step_id="dedup",
            summary=f"after_dedup={len(deduped_all)}",
        )

        # ------------------------------------------------------------------
        # Step 3b: verify findings against their cited source
        # ------------------------------------------------------------------
        # Runs BEFORE the low-confidence filter so a finding whose source is
        # dead or doesn't support it is demoted (often to 'low' → dropped)
        # here, instead of being routed to a human on snippet-level trust.
        # No-op unless xcrawl + verification are enabled; best-effort (a
        # verify failure leaves the finding unchanged).
        yield WorkflowEvent(
            type="step_start",
            step_id="verify",
            step_title="Verify findings against sources",
        )
        from openexecutive.monitoring.research.verification import (
            verify_findings,
        )

        deduped_all = await verify_findings(deduped_all)
        verified = [f for f in deduped_all if f.verification is not None]

        # Pre-synthesis quality filter — now reflects verify demotions.
        # Low-confidence findings are dropped before the Executive ever sees
        # them — the round_2 post-mortem showed they make up most of the
        # noise, and the synthesis prompt cannot be trusted to ignore them
        # consistently. Specialists are warned about this in their addendum
        # so the threshold is part of the contract.
        deduped: list[ResearchFinding] = [
            f for f in deduped_all if f.confidence != "low"
        ]
        low_conf_dropped = len(deduped_all) - len(deduped)
        if low_conf_dropped:
            logger.info(
                "research: dropped %d low-confidence finding(s) pre-synthesis "
                "(after verify)",
                low_conf_dropped,
            )

        # step_done carries the full post-verify accounting (the dedup step
        # now only reports after_dedup), so the low_conf_dropped / to_synthesis
        # signals observers relied on are preserved here.
        yield WorkflowEvent(
            type="step_done",
            step_id="verify",
            summary=(
                f"verified={len(verified)} "
                f"confirmed={sum(1 for f in verified if f.verification == 'confirmed')} "
                f"demoted={sum(1 for f in verified if f.verification in ('contradicted', 'unsupported', 'source_unreachable'))} "
                f"low_conf_dropped={low_conf_dropped} "
                f"to_synthesis={len(deduped)}"
            ),
        )

        # If no findings made it through, skip synthesis and emit a
        # quiet artifact. No outbound tool calls, no Alert, no DMs.
        if not deduped:
            yield WorkflowEvent(
                type="result",
                data={"findings": [], "tool_calls": [], "narrative": ""},
            )
            yield WorkflowEvent(
                type="artifact",
                content=_render_artifact(
                    deduped=[],
                    per_specialist=per_specialist,
                    tool_calls=[],
                    narrative="No findings surfaced this run.",
                    note=inputs.note,
                ),
            )
            yield WorkflowEvent(type="done")
            return

        # ------------------------------------------------------------------
        # Step 4: executive_synthesis (LLM + tool-use loop)
        # ------------------------------------------------------------------
        yield WorkflowEvent(
            type="step_start",
            step_id="executive_synthesis",
            step_title="Executive routes findings",
        )

        narrative, tool_calls = await _executive_synthesis_loop(deduped)

        # Dedicated watchlist-analysis pass — an explicit, forward-looking
        # decision about what to MONITOR going forward, with its own budget
        # so it never competes with urgent routing above. Best-effort: a
        # failure here must not sink the run. Merge its adds into tool_calls
        # so they surface in the artifact + result event + audit count.
        try:
            watchlist_calls = await _watchlist_analysis_loop(
                deduped, existing_watchlist,
            )
        except Exception:
            logger.exception("research: watchlist-analysis pass failed")
            watchlist_calls = []
        tool_calls.extend(watchlist_calls)

        yield WorkflowEvent(
            type="step_done",
            step_id="executive_synthesis",
            summary=(
                f"tool_calls={len(tool_calls)} "
                f"ok={sum(1 for t in tool_calls if t['ok'])} "
                f"watchlist_adds={sum(1 for t in watchlist_calls if t['ok'])}"
            ),
        )

        # ------------------------------------------------------------------
        # Step 5: emit_artifact
        # ------------------------------------------------------------------
        yield WorkflowEvent(
            type="step_start",
            step_id="emit_artifact",
            step_title="Emit summary",
        )

        artifact = _render_artifact(
            deduped=deduped,
            per_specialist=per_specialist,
            tool_calls=tool_calls,
            narrative=narrative,
            note=inputs.note,
        )

        # Persist this run's artifact into the recent-research knowledge
        # collection (keep-latest), so the next run + the chat Executive can
        # recall it via RAG. Separate collection, clearly labelled at
        # retrieval — never blended into curated company docs. Best-effort.
        try:
            from datetime import UTC, datetime

            from openexecutive.knowledge.loader import ingest_text
            from openexecutive.knowledge.store import ChromaDBStore

            now = datetime.now(UTC)
            store.delete_documents(
                ChromaDBStore.RESEARCH_COLLECTION,
                where={"type": "recent_research"},
            )
            await ingest_text(
                artifact,
                store,
                source_name=f"recent_research_{now.date().isoformat()}",
                collection=ChromaDBStore.RESEARCH_COLLECTION,
                extra_metadata={
                    "type": "recent_research",
                    "created_at": now.isoformat(),
                },
            )
        except Exception:
            logger.exception("research: persist artifact to knowledge failed")

        yield WorkflowEvent(
            type="step_done",
            step_id="emit_artifact",
            summary=f"artifact_chars={len(artifact)}",
        )

        # Structured result event for programmatic consumers (the cron
        # + chat tool both read this directly rather than grepping the
        # artifact).
        yield WorkflowEvent(
            type="result",
            data={
                "findings": [
                    {
                        "title": f.title,
                        "summary": f.summary,
                        "severity_hint": f.severity_hint.value,
                        "suggested_audience": f.suggested_audience,
                        "suggested_action": f.suggested_action,
                        "confidence": f.confidence,
                        "source_specialist": f.source_specialist,
                        "relevant_urls": f.relevant_urls,
                    }
                    for f in deduped
                ],
                "tool_calls": tool_calls,
                "narrative": narrative,
            },
        )
        yield WorkflowEvent(type="artifact", content=artifact)
        yield WorkflowEvent(type="done")

    def sample_inputs(self) -> dict[str, Any] | None:
        return {"note": ""}


# --------------------------------------------------------------------- #
# Executive synthesis loop — mirrors executive_reflection's pattern
# --------------------------------------------------------------------- #


async def _executive_synthesis_loop(
    findings: list[ResearchFinding],
) -> tuple[str, list[dict[str, Any]]]:
    """Run the Executive's tool-use loop over the deduped findings.

    Returns (narrative, tool_calls). The narrative is the final
    Markdown summary the model emitted; tool_calls is one summary
    entry per fired tool: {tool, input_preview, result_preview, ok}.

    Imports are deferred so this module doesn't pull the whole
    orchestrator stack into anywhere it's referenced at import time.
    """
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
    from openexecutive.workflows._synthesis import (
        execute_tool_calls,
        extract_artifact_from_response,
    )

    # Load the roster once: it both seeds the anti-spam guard AND is rendered
    # into the synthesis turn so the Executive can DM by person_id directly
    # (no lookup_person round-trips — the "looks up, never routes" failure).
    try:
        people = list_people()
    except Exception:
        logger.exception(
            "research.synthesis: list_people failed — guard runs with empty seen"
        )
        people = []

    # Derive has_roster from the SAME render the turn uses (which caps at 50),
    # so the system prompt's "use YOUR TEAM / don't look up" can never disagree
    # with whether the roster block is actually present in the turn.
    has_roster = bool(_render_team_roster(people))
    user_content = _render_synthesis_turn(findings, people)

    # Seed a synthetic Session so send_slack_dm / send_discord_dm etc.
    # don't trip the anti-spam guard the chat path enforces. Identical
    # mechanism to executive_reflection's setup — channels visible
    # only when they correspond to a real Person.
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

    synth_session = Session(seen_channel_refs=seen)
    ctx_token = current_session.set(synth_session)

    # Synthesis tools = full toolkit MINUS the research tools themselves.
    # The Executive should not call `run_executive_research` from
    # within its own synthesis pass (recursion risk + waste).
    settings = get_settings()
    tools = sorted(
        (t for t in _ALL_SKILL_TOOLS if t["name"] not in _SYNTHESIS_EXCLUDED_TOOLS),
        key=lambda t: t["name"],
    )
    # Only offer channels that are actually configured, so the Executive
    # can't route a finding into e.g. send_slack_dm when Slack has no token.
    configured = configured_integrations(settings)
    tools = filter_tools_for_configured_channels(tools, settings)
    # Build the system prompt for the SAME configured set, so the routing
    # menu never names a DM channel the model can't actually use.
    synthesis_system = _build_synthesis_system(configured, has_roster)
    model = settings.routing_model
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_content},
    ]
    tool_calls: list[dict[str, Any]] = []
    final_text = ""
    iteration = 0

    try:
        for iteration in range(1, _MAX_SYNTHESIS_ITERATIONS + 1):
            try:
                response = await get_provider(model).messages_create(
                    model=model,
                    max_tokens=_MAX_SYNTHESIS_TOKENS,
                    system=synthesis_system,
                    tools=tools,  # type: ignore[arg-type]
                    messages=messages,  # type: ignore[arg-type]
                )
            except Exception:
                logger.exception(
                    "research.synthesis: provider call failed iter=%d",
                    iteration,
                )
                break

            ok_so_far = _routing_ok(tool_calls)
            budget_remaining = max(0, _MAX_ROUTING_TOOLS_PER_RUN - ok_so_far)
            iter_calls = await execute_tool_calls(
                response, _ALL_SKILL_HANDLERS,
                budget_remaining=budget_remaining,
                free_tools=_NON_ROUTING_TOOLS,
            )
            tool_calls.extend(iter_calls)

            text = extract_artifact_from_response(response)
            if text:
                final_text = text

            stop_reason = getattr(response, "stop_reason", "")
            if stop_reason != "tool_use" or not iter_calls:
                # Model emitted text-only summary — done.
                break

            # If we exhausted the routing budget and the model still
            # wants more tools, terminate the loop — further iterations
            # would just collect more "over budget" stubs. The remaining
            # tool_use blocks are already echoed back as
            # "over budget — skipped" via execute_tool_calls. Counts only
            # OUTBOUND routing calls — a lookups-only iteration must NOT
            # terminate the loop before the model gets to route.
            new_ok = _routing_ok(iter_calls)
            if (ok_so_far + new_ok) >= _MAX_ROUTING_TOOLS_PER_RUN:
                logger.info(
                    "research.synthesis: routing budget exhausted "
                    "(%d/%d); terminating loop",
                    ok_so_far + new_ok, _MAX_ROUTING_TOOLS_PER_RUN,
                )
                break

            # No forward progress this iteration — every tool call failed
            # or was skipped. Stop rather than spend another turn on the
            # same stuck state (e.g. a lookup that keeps raising, or
            # outbound calls that keep failing). A successful free lookup
            # counts as progress, so the normal lookup→route flow is
            # unaffected; only a fully-failed iteration terminates here.
            # This also stops a fully-failed outbound iteration from
            # resetting its budget on the next round.
            if not any(t.get("ok") for t in iter_calls):
                logger.info(
                    "research.synthesis: no successful tool call in "
                    "iteration %d; terminating loop", iteration,
                )
                break

            # Build the next user turn carrying tool_result blocks so
            # the model sees what fired and can summarize.
            assistant_blocks: list[dict[str, Any]] = []
            for block in response.content:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    assistant_blocks.append({
                        "type": "text",
                        "text": getattr(block, "text", ""),
                    })
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

            tool_results: list[dict[str, Any]] = []
            summary_iter = iter(iter_calls)
            for block in response.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                summary = next(summary_iter, None)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": getattr(block, "id", ""),
                    "content": (
                        summary["result_preview"]
                        if summary else "no result captured"
                    ),
                })
            messages.append({"role": "user", "content": tool_results})
    finally:
        current_session.reset(ctx_token)

    return final_text, tool_calls


async def _watchlist_analysis_loop(
    findings: list[ResearchFinding],
    existing_watchlist: list[Any],
) -> list[dict[str, Any]]:
    """Dedicated forward-looking pass: decide what to MONITOR going forward
    and call ``add_watchlist_entry`` for each.

    Separate from ``_executive_synthesis_loop`` (which routes urgent items
    to people) and from its budget, so adding monitors is the explicit goal
    here rather than a deprioritized afterthought. Returns the tool-call
    summaries (same shape as the synthesis loop) so they merge into the
    run's ``tool_calls``. Only ``add_watchlist_entry`` is offered — no
    channel tools, so no synthetic Session is needed.
    """
    if not findings:
        return []

    from openexecutive.config import get_settings
    from openexecutive.orchestrator.watchlist_tools import (
        ADD_WATCHLIST_ENTRY_TOOL,
        handle_add_watchlist_entry,
    )
    from openexecutive.providers import get_provider
    from openexecutive.workflows._synthesis import execute_tool_calls

    handlers = {"add_watchlist_entry": handle_add_watchlist_entry}
    tools = [ADD_WATCHLIST_ENTRY_TOOL]
    settings = get_settings()
    model = settings.routing_model
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _render_watchlist_turn(findings, existing_watchlist),
        },
    ]
    tool_calls: list[dict[str, Any]] = []

    for _iteration in range(1, _MAX_WATCHLIST_ITERATIONS + 1):
        try:
            response = await get_provider(model).messages_create(
                model=model,
                max_tokens=_MAX_SYNTHESIS_TOKENS,
                system=_WATCHLIST_SYSTEM,
                tools=tools,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception("research.watchlist: provider call failed")
            break

        ok_so_far = sum(1 for t in tool_calls if t.get("ok"))
        budget_remaining = max(0, _MAX_WATCHLIST_ADDS_PER_RUN - ok_so_far)
        iter_calls = await execute_tool_calls(
            response, handlers, budget_remaining=budget_remaining,
        )
        tool_calls.extend(iter_calls)

        stop_reason = getattr(response, "stop_reason", "")
        if stop_reason != "tool_use" or not iter_calls:
            break

        new_ok = sum(1 for t in iter_calls if t.get("ok"))
        if (ok_so_far + new_ok) >= _MAX_WATCHLIST_ADDS_PER_RUN:
            logger.info(
                "research.watchlist: budget exhausted (%d/%d); stopping",
                ok_so_far + new_ok, _MAX_WATCHLIST_ADDS_PER_RUN,
            )
            break

        # Carry assistant tool_use + tool_result blocks forward so the model
        # can continue or wrap up on the next iteration.
        assistant_blocks: list[dict[str, Any]] = []
        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                assistant_blocks.append(
                    {"type": "text", "text": getattr(block, "text", "")}
                )
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

        tool_results: list[dict[str, Any]] = []
        summary_iter = iter(iter_calls)
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            summary = next(summary_iter, None)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": getattr(block, "id", ""),
                "content": (
                    summary["result_preview"] if summary else "no result captured"
                ),
            })
        messages.append({"role": "user", "content": tool_results})

    return tool_calls


# Tools the Executive synthesis pass MUST NOT call. Two reasons:
#  - `run_executive_research` — recursion risk (and waste).
#  - `send_company_broadcast` — research output is exploratory and
#    rarely warrants a whole-company message. When something does
#    warrant broadcast, the principal can decide that from the
#    briefing card after seeing the research-driven alert. Keeping
#    broadcast out of the synthesis toolkit defends against a
#    misfiring run blasting noise to the entire team.
_SYNTHESIS_EXCLUDED_TOOLS = frozenset({
    "run_executive_research",
    "send_company_broadcast",
    # Raw per-channel DM tools are withheld from synthesis: the model kept
    # passing the wrong identifier into them (a person_id, another channel's
    # id, or an invented Slack-style handle), so DMs silently failed the roster
    # gate. message_person(person_id, text) is the only DM path here — the
    # server resolves the channel + real id, so there is nothing to fabricate.
    "send_slack_dm",
    "send_discord_dm",
    "send_telegram_message",
})


# --------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------- #


def _render_research_context(
    *,
    profile: Any,
    initiatives: list[Any],
    existing_watchlist: list[Any],
    note: str,
) -> str:
    """Build the user-turn block passed to every specialist call."""
    from datetime import UTC, datetime, timedelta

    parts: list[str] = []

    # Date anchor. Without an explicit "today", the model cannot judge
    # recency at all — it does not reliably know the current date, and
    # web_search result dates are meaningless without a "now" to compare
    # against. This is what makes the prompt's "last 30 days" rule
    # enforceable. Lives in the user turn (not the cached system block),
    # so the changing date never busts prompt caching.
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=30)
    parts.append(
        f"TODAY'S DATE: {today.isoformat()} (UTC). You are scanning for "
        "what changed RECENTLY: only surface developments dated on or "
        f"after {cutoff.isoformat()} (the last 30 days). Treat anything "
        "older — or undated/unverifiable — as out of scope for this "
        "run.\n"
    )

    if note:
        parts.append(f"USER NOTE: {note}\n")

    try:
        profile_block = profile.to_prompt_block() if profile else ""
    except Exception:
        logger.exception("research: profile.to_prompt_block failed")
        profile_block = ""
    if profile_block:
        parts.append("COMPANY PROFILE:\n")
        parts.append(profile_block)
        parts.append("")

    if initiatives:
        parts.append("ACTIVE INITIATIVES:")
        for i in initiatives[:20]:
            title = getattr(i, "title", "")
            status = getattr(i, "status", "")
            summary = getattr(i, "summary", "")
            if title:
                line = f"- {title} ({status})"
                if summary:
                    line = f"{line}: {summary[:120]}"
                parts.append(line)
        parts.append("")

    if existing_watchlist:
        parts.append("ALREADY ON THE WATCHLIST:")
        for item in existing_watchlist[:50]:
            slug = getattr(item, "slug", "")
            signal_type = getattr(item, "signal_type", "")
            target = getattr(item, "target", "")
            if slug:
                parts.append(f"- {slug} [{signal_type}] target={target}")
        parts.append("")

    parts.append(
        "Research within your domain and emit findings via the "
        "`emit_research_findings` tool. Use web_search to FIND and "
        "date-confirm recent developments — do NOT answer from memory. "
        "If you cannot confirm an item is recent (within the window "
        "above) and real via search, omit it: zero findings beats a "
        "stale one. The Executive will decide what to do with each "
        "finding."
    )
    return "\n".join(parts)


def _render_team_roster(people: list[Any]) -> str:
    """Render the active roster so the Executive can DM by person_id directly,
    without spending synthesis turns on lookup_person (which it otherwise burns
    on names that aren't on the roster — the "looks up, never routes" failure).
    """
    lines: list[str] = []
    for p in people[:50]:
        pid = getattr(p, "id", None)
        if pid is None:
            continue
        name = getattr(p, "full_name", "") or "(unnamed)"
        role = getattr(p, "role", "") or ""
        depts = getattr(p, "department_slugs", None) or []
        suffix = f", {role}" if role else ""
        dept = f" [{', '.join(depts)}]" if depts else ""
        lines.append(f"- person_id={pid} — {_inline(name)}{_inline(suffix)}{_inline(dept)}")
    if not lines:
        return ""
    return (
        "YOUR TEAM (DM with message_person — pass the person_id, nothing else):\n"
        + "\n".join(lines)
        + "\n"
    )


def _render_synthesis_turn(findings: list[ResearchFinding], people: list[Any]) -> str:
    """Pack the deduped findings + the active roster into a single user-turn
    for the Executive's synthesis pass."""
    parts: list[str] = []
    roster = _render_team_roster(people)
    if roster:
        parts.append(roster)
    parts.append("FINDINGS FROM YOUR SPECIALIST COUNCIL:\n")
    for i, f in enumerate(findings, start=1):
        urls = (
            "  urls: " + ", ".join(f.relevant_urls[:3])
            if f.relevant_urls else ""
        )
        suggested_action = (
            f"  suggested_action: {f.suggested_action}"
            if f.suggested_action else ""
        )
        block = (
            f"#{i} [{f.severity_hint.value} | {f.confidence}] "
            f"({f.source_specialist}) {f.title}\n"
            f"  {f.summary}\n"
            f"  suggested_audience: {f.suggested_audience}\n"
        )
        if suggested_action:
            block += suggested_action + "\n"
        if urls:
            block += urls + "\n"
        parts.append(block)

    dm_howto = (
        "To DM a person, call message_person(person_id, text) using a "
        "person_id from YOUR TEAM above — do NOT call lookup_person, the "
        "roster is already here."
        if roster else
        "To DM a person, call lookup_person to resolve their person_id, then "
        "message_person(person_id, text)."
    )
    parts.append(
        "Route each finding via the right tool. " + dm_howto + " If no one "
        "owns a finding, use create_alert or leave it quiet. When two findings "
        "tell one story, combine them into a single message. Skip low-signal "
        "findings rather than firing noisy tools. Cite relevant_urls in "
        "outbound messages."
    )
    return "\n".join(parts)


def _render_watchlist_turn(
    findings: list[ResearchFinding],
    existing_watchlist: list[Any],
) -> str:
    """User-turn for the dedicated watchlist-analysis pass: the findings
    (with their URLs) plus the current watchlist for dedup."""
    parts: list[str] = ["FINDINGS FROM THIS RESEARCH RUN:\n"]
    for i, f in enumerate(findings, start=1):
        block = (
            f"#{i} [{f.severity_hint.value} | {f.confidence}] "
            f"({f.source_specialist}) {f.title}\n"
            f"  {f.summary}\n"
        )
        if f.relevant_urls:
            block += "  urls: " + ", ".join(f.relevant_urls[:3]) + "\n"
        parts.append(block)

    parts.append("ALREADY ON THE WATCHLIST (do not re-add):")
    if existing_watchlist:
        for item in existing_watchlist[:50]:
            slug = getattr(item, "slug", "")
            signal_type = getattr(item, "signal_type", "")
            target = getattr(item, "target", "")
            if slug:
                parts.append(f"- {slug} [{signal_type}] target={target}")
    else:
        parts.append("- (nothing yet)")

    parts.append(
        "\nDecide which ongoing sources are worth monitoring and call "
        "add_watchlist_entry for each — real ticker/feed targets only, "
        "preferring URLs from the findings above."
    )
    return "\n".join(parts)


def _render_artifact(
    *,
    deduped: list[ResearchFinding],
    per_specialist: list[ResearchRunSummary],
    tool_calls: list[dict[str, Any]],
    narrative: str,
    note: str,
) -> str:
    """Render the workflow artifact — short Markdown summary of what
    was researched + what the Executive routed."""
    lines: list[str] = ["# Executive research"]
    if note:
        lines.append(f"\n_Note: {note}_\n")

    if narrative:
        lines.append("\n" + narrative.strip())
    else:
        lines.append(
            "\n_(Executive synthesis returned no narrative.)_"
        )

    if deduped:
        lines.append("\n\n## Findings reviewed")
        lines.append(
            f"_{len(deduped)} finding(s) after dedup; the Executive's "
            f"actions are listed above._\n"
        )
        for f in deduped[:25]:
            lines.append(
                f"- [{f.severity_hint.value} | {f.confidence}] "
                f"({f.source_specialist}) **{_inline(f.title)}** — "
                f"{_inline(f.summary)}"
            )

    routed = [t for t in tool_calls if t.get("tool") != "add_watchlist_entry"]
    if routed:
        lines.append("\n## Actions routed")
        for t in routed[:25]:
            mark = "✓" if t.get("ok") else "✗"
            lines.append(
                f"- {mark} `{t.get('tool', '')}` — {t.get('result_preview', '')}"
            )

    watchlist_calls = [
        t for t in tool_calls if t.get("tool") == "add_watchlist_entry"
    ]
    if watchlist_calls:
        lines.append("\n## Now watching")
        for t in watchlist_calls[:25]:
            # Show failures too (e.g. slug-dedup rejections) so an add that
            # didn't take isn't silently dropped from the artifact.
            mark = "✓" if t.get("ok") else "✗"
            lines.append(f"- {mark} {_inline(t.get('result_preview', ''))}")

    lines.append("\n## Per-specialist activity")
    for s in per_specialist:
        marker = "✓" if s.findings_emitted else "·"
        err = f" — error: {s.error}" if s.error else ""
        lines.append(
            f"- {marker} `{s.specialist}` — {s.findings_emitted} finding(s){err}"
        )

    return "\n".join(lines)


def _inline(s: str) -> str:
    """Collapse newlines + backticks in user-supplied text so a prompt-
    injected finding can't smuggle a fenced block into the artifact."""
    if not s:
        return ""
    out = s
    for ch in ("\n", "\r", "`"):
        out = out.replace(ch, " ")
    return out.strip()


__all__ = [
    "RESEARCH_SPECIALISTS",
    "ExecutiveResearchInput",
    "ExecutiveResearchWorkflow",
]
