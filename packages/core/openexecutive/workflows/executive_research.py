"""ExecutiveResearchWorkflow — specialists research, Executive routes.

Mirrors the architecture of ``executive_reflection.py``: a research
phase produces structured findings, then the Executive enters a tool-
use loop with its full outbound toolkit (``_ALL_SKILL_HANDLERS``) and
decides per finding whether to:

  - DM a specific Person via ``message_person`` (pass the ``person_id``
    from ``lookup_person``; the server resolves their real channel id)
  - Message a department channel via ``send_department_message``
  - Surface as a briefing card via ``create_alert``
  - Propose an ongoing source to monitor via ``propose_watch`` (a dedicated
    pass after routing; deterministic policy in
    ``monitoring.research.watch_policy`` decides whether a proposal is added
    on its own or shown to the principal as a suggestion)
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
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.audit.usage import UsageRollup, bind_research_run, log_model_usage
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


def active_research_specialists() -> tuple[str, ...]:
    """The specialists this run fans out to: ``RESEARCH_SPECIALISTS``
    filtered by the ``RESEARCH_SPECIALISTS`` setting when it is set.
    Unknown slugs are logged and dropped; an empty or all-unknown setting
    means every specialist, so a typo cannot silently run nothing."""
    from openexecutive.config import get_settings

    wanted = [s.strip().lower() for s in get_settings().research_specialists if s.strip()]
    if not wanted:
        return RESEARCH_SPECIALISTS
    unknown = [s for s in wanted if s not in RESEARCH_SPECIALISTS]
    if unknown:
        logger.warning(
            "research: RESEARCH_SPECIALISTS names unknown specialist(s) %s — ignored",
            ", ".join(unknown),
        )
    chosen = tuple(s for s in RESEARCH_SPECIALISTS if s in wanted)
    return chosen or RESEARCH_SPECIALISTS

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
# separate from the routing budget above, so proposing monitors never
# competes with urgent DMs/alerts. The pass only PROPOSES: the model may
# queue up to _MAX_WATCHLIST_PROPOSALS_PER_RUN candidates per run and
# `watch_policy.apply_proposals` decides add / suggest / reject under the
# settings-driven per-run budgets (direct adds + suggestions).
_MAX_WATCHLIST_PROPOSALS_PER_RUN = 6
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


def _solo_routing_options(configured: set[str]) -> str:
    """The ROUTING OPTIONS section in solo mode: the founder is the only
    person this pass routes to. Nobody else is contacted unattended."""
    if configured & {"slack", "discord", "telegram"}:
        option_a = (
            "  (a) **DM the founder** via message_person(person_id, text), "
            "with the person_id under THE FOUNDER in the findings turn — only "
            "for something they should see today, before the morning brief.\n"
        )
    else:
        option_a = (
            "  (a) **DM the founder** — UNAVAILABLE: no direct-message channel "
            "is configured. Use a briefing card (b) instead.\n"
        )
    return (
        "## ROUTING OPTIONS\n\n"
        "The founder runs this business on their own and is the only person "
        "you route to. There is no team, no department channel and no company "
        "broadcast. Never message anyone else from this pass — clients, "
        "contractors and vendors hear from you only when the founder asks.\n\n"
        + option_a
        + "  (b) **Surface as briefing card** via create_alert — when the "
        "founder must decide or react.\n"
        "  (c) **Schedule a follow-up** to the founder via schedule_followup "
        "for time-shifted chases.\n"
        "  (d) **Suggest a deeper workflow** via suggest_workflow only "
        "for major events (M&A, fundraising, crisis comms).\n"
        "  Watchlist changes are NOT yours to make here — a dedicated pass "
        "after this one proposes monitors under policy.\n\n"
    )


def _build_synthesis_system(
    configured: set[str], has_roster: bool = True, mode: str = "team"
) -> str:
    """Build the synthesis system prompt for the channels actually
    configured, so option (a) never names an unavailable DM channel.
    ``has_roster`` controls whether option (a) tells the model to take a
    person_id from the in-turn roster or to fall back to lookup_person.
    ``mode="solo"`` swaps the routing menu for the founder-only one."""
    if mode == "solo":
        routing = _solo_routing_options(configured)
    else:
        routing = (
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
            "  (d) **Schedule a follow-up** via schedule_followup for "
            "time-shifted chases.\n"
            "  (e) **Suggest a deeper workflow** via suggest_workflow only "
            "for major events (M&A, fundraising, crisis comms).\n"
            "  Watchlist changes are NOT yours to make here — a dedicated pass "
            "after this one proposes monitors under policy.\n\n"
        )
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
        "calls in this run combined (DMs, alerts, "
        "follow-ups, workflow suggestions). The runtime enforces this "
        "ceiling — calls beyond the budget will be rejected. Choose the "
        "few that genuinely deserve the cost. Read-only lookups "
        "(`lookup_person`, `list_people`) do NOT count against this "
        "budget — resolve a recipient freely, then spend the budget on "
        "the message itself.\n\n"
        + routing
        + "## INVARIANTS\n\n"
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


def _build_watchlist_system(max_direct: int, max_suggest: int) -> str:
    """System prompt for the watchlist pass. Built per call because the
    budgets come from settings; static per deployment, so it caches."""
    return (
        "You are the user's Executive deciding what the company should "
        "MONITOR going forward, based on this run's research findings.\n\n"
        "This is a deliberate, forward-looking pass — distinct from routing "
        "urgent items to people. Your ONLY tool is `propose_watch`. You do "
        "not add anything yourself: policy code adds a proposal on its own "
        "when it is grounded in company data and corroborated, and files "
        "anything less certain as a SUGGESTION the principal approves on the "
        "watch list. Being honest about certainty is what keeps the watch "
        "list trusted.\n\n"
        "## WHAT BELONGS ON THE WATCHLIST\n\n"
        "An ongoing, externally-observable source tied to a NAMED company "
        "entity — a competitor, vendor, ticker or initiative from the company "
        "context, or an entity a department watches or a recent decision "
        "names — that we'd want flagged when it next changes: a public "
        "competitor's ticker or filings, a vendor's status page, a competitor's "
        "own blog / changelog feed. A one-off event already fully known is NOT "
        "watchlist material; the *ongoing source* behind it might be. "
        "Industry news feeds and generic searches are almost never worth it.\n\n"
        "## RULES\n\n"
        "  - Propose at most a handful of sources; most runs warrant zero or "
        f"one. Policy adds at most {max_direct} on its own and files at most "
        f"{max_suggest} suggestions per run — extra proposals are dropped.\n"
        "  - `grounding_entity` MUST name the competitor / vendor / ticker / "
        "initiative from the company context this source is about. For a "
        "stock / edgar watch the entity is the ticker itself (e.g. 'ACME').\n"
        "  - `target` MUST be concrete and real: a ticker symbol, or a URL "
        "taken from a finding's `urls` list — do NOT invent feed URLs. If you "
        "cannot give a real target, do not propose.\n"
        "  - `finding_index` is REQUIRED: the #N of the finding whose urls or "
        "text this source comes from. A proposal with no finding behind it is "
        "rejected as having no evidence, however well grounded.\n"
        "  - `certainty` = 'confident' ONLY when the entity is in the company "
        "context AND the target is the entity's own source (its ticker, its "
        "site, its status page). Otherwise 'unsure'.\n"
        "  - `rationale` is the one line the principal reads: why this source, "
        "for this company, now.\n"
        "  - SKIP anything already on the current watchlist and NEVER propose "
        "a target listed under DECLINED.\n"
        "  - `slug` must be unique kebab-case (e.g. `stock-tsla`, "
        "`rss-acme-blog`). Set `route_to_specialist` when obvious.\n\n"
        "After your tool calls, emit ONE short line: what you proposed and "
        "what you deliberately skipped. Be terse."
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
    run_id: str | None = Field(
        default=None,
        description=(
            "Optional id the caller already recorded for this run (the "
            "periodic scheduler's workflow_runs id). Every model-usage row "
            "the run writes carries it; generated when omitted."
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
    background = True

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
        run_id = inputs.run_id or f"research-{uuid.uuid4().hex[:12]}"
        # Every model call the run makes (specialists, synthesis, watchlist
        # pass) records a cache_event row tagged with the run id and summed
        # into a rollup the result event reports as `usage`. The body runs in
        # its own task so the binding lives in that task's context: set
        # inside this generator it would land in the consumer's context and
        # outlive an abandoned run.
        queue: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()

        async def _produce() -> None:
            try:
                with bind_research_run(run_id) as rollup:
                    async for event in self._run_events(inputs, store, rollup, run_id):
                        queue.put_nowait(event)
            finally:
                queue.put_nowait(None)

        producer = asyncio.create_task(_produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
            await producer  # surface a crash inside the run to the consumer
        finally:
            if not producer.done():
                producer.cancel()

    async def _run_events(
        self,
        inputs: ExecutiveResearchInput,
        store: ChromaDBStore,
        rollup: UsageRollup,
        run_id: str,
    ) -> AsyncIterator[WorkflowEvent]:
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

        # Departments (watched entities, scope, goals) and recent decisions
        # are current company intent the static profile lacks; both feed
        # the watch policy's grounding and routing.
        from openexecutive.monitoring.research.watch_policy import (
            load_departments,
            recent_decisions,
        )

        departments = load_departments()
        decisions = recent_decisions()

        research_context = _render_research_context(
            profile=profile,
            initiatives=initiatives,
            existing_watchlist=existing_watchlist,
            note=inputs.note,
            decisions=decisions,
            departments=departments,
        )

        yield WorkflowEvent(
            type="step_done",
            step_id="gather_context",
            summary=(
                f"profile_loaded={profile is not None and not profile.is_empty()} "
                f"initiatives={len(initiatives)} "
                f"existing_watchlist={len(existing_watchlist)} "
                f"departments={len(departments)} decisions={len(decisions)}"
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

        specialists = active_research_specialists()
        results = await asyncio.gather(
            *(_run_one(s) for s in specialists),
            return_exceptions=True,
        )

        findings_by_specialist: dict[str, list[ResearchFinding]] = {}
        per_specialist: list[ResearchRunSummary] = []
        for idx, item in enumerate(results):
            if isinstance(item, BaseException):
                fallback_slug = (
                    specialists[idx]
                    if idx < len(specialists)
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

        # Pre-synthesis quality filter.
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
                "research: dropped %d low-confidence finding(s) pre-synthesis",
                low_conf_dropped,
            )

        yield WorkflowEvent(
            type="step_done",
            step_id="dedup",
            summary=(
                f"after_dedup={len(deduped_all)} "
                f"low_conf_dropped={low_conf_dropped} "
                f"to_synthesis={len(deduped)}"
            ),
        )

        # If no findings made it through, skip synthesis and emit a
        # quiet artifact. No outbound tool calls, no Alert, no DMs.
        if not deduped:
            yield WorkflowEvent(
                type="result",
                data={
                    "findings": [], "tool_calls": [], "narrative": "",
                    "usage": {"run_id": run_id, **rollup.as_dict()},
                },
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
                profile=profile, initiatives=initiatives,
                departments=departments, decisions=decisions,
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
                f"watchlist_adds={sum(1 for t in watchlist_calls if t.get('outcome') == 'added')} "
                f"watchlist_suggested={sum(1 for t in watchlist_calls if t.get('outcome') == 'suggested')}"
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
                "usage": {"run_id": run_id, **rollup.as_dict()},
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
    from openexecutive.memory.workspace_settings import effective_workspace_mode
    from openexecutive.orchestrator.executive import (
        _ALL_SKILL_HANDLERS,
        _ALL_SKILL_TOOLS,
    )
    from openexecutive.orchestrator.schedule_tools import (
        configured_integrations,
        current_session,
        filter_tools_for_configured_channels,
        unattended_toolkit,
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
    # Solo / team for this run; a caller's session override (evals) is read
    # before the synthesis binds its own session below.
    outer_session = current_session.get()
    mode = effective_workspace_mode(outer_session)
    solo = mode == "solo"
    roster_people = [p for p in people if getattr(p, "is_principal", False)] if solo else people
    has_roster = bool(_render_team_roster(roster_people))
    user_content = _render_synthesis_turn(findings, people, mode=mode)

    # Seed a synthetic Session so send_slack_dm / send_discord_dm etc.
    # don't trip the anti-spam guard the chat path enforces. Identical
    # mechanism to executive_reflection's setup — channels visible
    # only when they correspond to a real Person.
    seen: set[tuple[str, str]] = set()
    for person in roster_people:
        if person.slack_user_id:
            seen.add(("slack_dm", person.slack_user_id))
        if person.discord_user_id:
            seen.add(("discord_dm", person.discord_user_id))
        if person.telegram_chat_id:
            seen.add(("telegram", person.telegram_chat_id))
        if person.email:
            seen.add(("email", person.email))

    synth_session = Session(
        seen_channel_refs=seen,
        workspace_mode=getattr(outer_session, "workspace_mode", None),
    )
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
    # Dispatch only what was offered: _SYNTHESIS_EXCLUDED_TOOLS (watchlist
    # writes, raw DMs, ack_alert, run_workflow) used to stay runnable here if
    # the model emitted them anyway. Solo also withholds the team tools and
    # meeting booking, and messages only the founder.
    tools, handlers = unattended_toolkit(tools, _ALL_SKILL_HANDLERS, mode)
    # Build the system prompt for the SAME configured set, so the routing
    # menu never names a DM channel the model can't actually use.
    synthesis_system = _build_synthesis_system(configured, has_roster, mode=mode)
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
            log_model_usage(
                response, model=model, actor="research_synthesis", iteration=iteration,
            )

            ok_so_far = _routing_ok(tool_calls)
            budget_remaining = max(0, _MAX_ROUTING_TOOLS_PER_RUN - ok_so_far)
            # DMs the synthesis sends are proactive research outreach; the
            # outcome ledger records whether they land.
            from openexecutive.attunement.outcomes import SOURCE_RESEARCH, tag_proactive

            with tag_proactive(SOURCE_RESEARCH):
                iter_calls = await execute_tool_calls(
                    response, handlers,
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
    *,
    profile: Any = None,
    initiatives: list[Any] | None = None,
    departments: list[Any] | None = None,
    decisions: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Dedicated forward-looking pass: the model PROPOSES sources to monitor
    via ``propose_watch``; ``watch_policy.apply_proposals`` then decides,
    deterministically, which are added on their own (grounded in company
    data + corroborated) and which become suggestions for the principal.

    Separate from ``_executive_synthesis_loop`` (which routes urgent items
    to people) and from its budget. Returns tool-call summaries (same shape
    as the synthesis loop, plus ``outcome`` = added | suggested | rejected)
    so they merge into the run's ``tool_calls``. Only ``propose_watch`` is
    offered — no channel tools, so no synthetic Session is needed.
    """
    if not findings:
        return []

    from openexecutive.config import get_settings
    from openexecutive.monitoring import store as monitoring_store
    from openexecutive.monitoring.research import watch_policy
    from openexecutive.orchestrator.watchlist_tools import (
        PROPOSE_WATCH_TOOL,
        handle_propose_watch,
    )
    from openexecutive.providers import get_provider
    from openexecutive.workflows._synthesis import execute_tool_calls

    policy_settings = watch_policy.PolicySettings.load()
    proposals: list[watch_policy.WatchProposal] = []

    async def _propose(tool_input: dict[str, Any]) -> str:
        return await handle_propose_watch(tool_input, proposals)

    handlers = {"propose_watch": _propose}
    tools = [PROPOSE_WATCH_TOOL]
    settings = get_settings()
    model = settings.routing_model
    try:
        declines = monitoring_store.list_declines()
    except Exception:
        logger.exception("research.watchlist: list_declines failed")
        declines = []
    try:
        outcome_counts = monitoring_store.policy_outcome_counts()
        specialist_counts = monitoring_store.specialist_outcome_counts()
    except Exception:
        logger.exception("research.watchlist: outcome counts failed")
        outcome_counts, specialist_counts = {}, {}

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _render_watchlist_turn(
                findings, existing_watchlist,
                declines=declines, outcome_counts=outcome_counts,
                specialist_counts=specialist_counts,
                departments=departments,
            ),
        },
    ]
    system = _build_watchlist_system(
        policy_settings.max_direct_adds, policy_settings.max_suggestions,
    )
    call_summaries: list[dict[str, Any]] = []

    for _iteration in range(1, _MAX_WATCHLIST_ITERATIONS + 1):
        try:
            response = await get_provider(model).messages_create(
                model=model,
                max_tokens=_MAX_SYNTHESIS_TOKENS,
                system=system,
                tools=tools,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception("research.watchlist: provider call failed")
            break
        log_model_usage(
            response, model=model, actor="research_watchlist", iteration=_iteration,
        )

        budget_remaining = max(0, _MAX_WATCHLIST_PROPOSALS_PER_RUN - len(proposals))
        iter_calls = await execute_tool_calls(
            response, handlers, budget_remaining=budget_remaining,
        )
        call_summaries.extend(iter_calls)

        stop_reason = getattr(response, "stop_reason", "")
        if stop_reason != "tool_use" or not iter_calls:
            break
        if len(proposals) >= _MAX_WATCHLIST_PROPOSALS_PER_RUN:
            logger.info(
                "research.watchlist: proposal cap reached (%d); stopping",
                len(proposals),
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

    # Proposals the handler refused (bad target, declined, duplicate slug)
    # are already in call_summaries with ok=False; keep them so the artifact
    # shows what the model tried. Policy decides the rest.
    refused = [c for c in call_summaries if not c.get("ok")]
    for c in refused:
        c["outcome"] = "rejected"
    if not proposals:
        return refused

    ctx = watch_policy.PolicyContext(
        vocabulary=watch_policy.grounding_vocabulary(
            profile, list(initiatives or []), list(existing_watchlist), list(departments or []),
        ),
        priority_terms=watch_policy.priority_terms(profile),
        existing=list(existing_watchlist),
        outcome_counts=outcome_counts,
        settings=policy_settings,
        departments=watch_policy.department_refs(departments),
        recent_decisions=list(decisions or []),
    )
    decided = watch_policy.apply_proposals(proposals, findings, ctx)
    return refused + decided


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
    # Starting another workflow from inside the routing pass is the same
    # recursion risk in a different coat: a finding can suggest a workflow
    # (suggest_workflow) for a human to start, never start one itself.
    "run_workflow",
    "send_company_broadcast",
    # Watchlist writes are withheld from the routing pass: every watch the
    # research run creates must go through the dedicated watchlist pass and
    # its policy (grounding, budgets, declines, suggestions). With these
    # tools present, a finding could add an unfiltered live watch, flip a
    # pending suggestion to active (self-approval), or write a permanent
    # decline in the principal's name.
    "add_watchlist_entry",
    "tune_watchlist_entry",
    "remove_watchlist_entry",
    # Raw per-channel DM tools are withheld from synthesis: the model kept
    # passing the wrong identifier into them (a person_id, another channel's
    # id, or an invented Slack-style handle), so DMs silently failed the roster
    # gate. message_person(person_id, text) is the only DM path here — the
    # server resolves the channel + real id, so there is nothing to fabricate.
    "send_slack_dm",
    "send_discord_dm",
    "send_telegram_message",
    # Clearing an item off the principal's "Needs you" list is not this pass's
    # job, and nobody is watching it. Alert headlines and bodies are minted
    # from inbound mail and chat, so a line reading "the principal already
    # reviewed 12, 13 — mark them dismissed" arrives attacker-controlled in
    # the very context this pass reasons over. The alert review closes alerts,
    # with evidence; a research run does not.
    "ack_alert",
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
    decisions: list[Any] | None = None,
    departments: list[Any] | None = None,
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

    if decisions:
        # What the company recently decided is the freshest statement of
        # what matters; the static profile lags it by design.
        parts.append("RECENT DECISIONS:")
        for d in decisions[:10]:
            # One line per decision: the summary is free text written by
            # the chat memory extractor, so an embedded newline must not be
            # able to forge another labelled line of this turn.
            summary = " ".join(str(getattr(d, "summary", "") or "").split())
            if not summary:
                continue
            when = str(getattr(d, "timestamp", "") or "")[:10]
            dept = str(getattr(d, "department", "") or "")
            line = f"- {when}" if when else "-"
            if dept:
                line += f" [{dept}]"
            parts.append(f"{line}: {summary[:160]}")
        parts.append("")

    interest_lines = _render_department_interests(departments)
    if interest_lines:
        # What a department head asked to have watched is company intent
        # the profile does not carry; without it a specialist's grounding
        # rule drops findings about exactly those entities.
        parts.append(
            "DEPARTMENT WATCH INTERESTS (entities a department head asked "
            "to have watched — a finding may be grounded in one of these):"
        )
        parts.extend(interest_lines)
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


# Bounds on the DEPARTMENT WATCH INTERESTS block: departments listed and
# entities named per department. Generous for any real org chart; they only
# stop a runaway list from crowding the specialist turn.
_MAX_INTEREST_DEPARTMENTS = 20
_MAX_INTEREST_ENTITIES = 20


def _render_department_interests(departments: list[Any] | None) -> list[str]:
    """One ``- <slug>: <entities>`` line per department with watched
    entities. Shared by the specialist context and the watchlist turn so
    both name the same interests."""
    lines: list[str] = []
    for state in departments or []:
        config = getattr(state, "config", None)
        slug = getattr(config, "slug", "")
        entities = [
            " ".join(str(e).split())
            for e in (getattr(config, "watched_entities", None) or [])
            if str(e).strip()
        ]
        if slug and entities:
            lines.append(f"- {slug}: {', '.join(entities[:_MAX_INTEREST_ENTITIES])}")
    return lines[:_MAX_INTEREST_DEPARTMENTS]


def _render_team_roster(people: list[Any], heading: str = "YOUR TEAM") -> str:
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
        f"{heading} (DM with message_person — pass the person_id, nothing else):\n"
        + "\n".join(lines)
        + "\n"
    )


def _render_synthesis_turn(
    findings: list[ResearchFinding], people: list[Any], mode: str = "team"
) -> str:
    """Pack the deduped findings + the active roster into a single user-turn
    for the Executive's synthesis pass. Solo lists only the founder."""
    parts: list[str] = []
    solo = mode == "solo"
    roster_heading = "THE FOUNDER" if solo else "YOUR TEAM"
    if solo:
        people = [p for p in people if getattr(p, "is_principal", False)]
    roster = _render_team_roster(people, heading=roster_heading)
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
        f"person_id from {roster_heading} above — do NOT call lookup_person, the "
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
    *,
    declines: list[Any] | None = None,
    outcome_counts: dict[tuple[str, str], dict[str, int]] | None = None,
    specialist_counts: dict[str, dict[str, int]] | None = None,
    departments: list[Any] | None = None,
) -> str:
    """User-turn for the watchlist-analysis pass: the findings (with URLs),
    the current watchlist with its trust record, targets the principal
    declined, what each department has asked to watch, and how the
    policy's past guesses turned out."""
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
            if not slug:
                continue
            line = f"- {slug} [{signal_type}] target={target}"
            origin = getattr(item, "origin", "")
            if origin:
                line += f" origin={origin}"
            fired = getattr(item, "fired_count", 0) or 0
            dismissed = getattr(item, "dismiss_count", 0) or 0
            trust = getattr(item, "trust_score", None)
            if fired or dismissed:
                line += f" fired={fired} dismissed={dismissed}"
            if isinstance(trust, (int, float)) and trust < 1.0:
                line += f" trust={trust:.2f}"
            if not getattr(item, "enabled", True):
                line += " (disabled)"
            parts.append(line)
    else:
        parts.append("- (nothing yet)")

    if declines:
        parts.append("\nDECLINED BY THE PRINCIPAL (never propose these again):")
        for d in declines[:40]:
            target = getattr(d, "normalized_target", "")
            reason = getattr(d, "reason", "")
            entity = getattr(d, "entity", "")
            if target:
                line = f"- {target}"
                if entity:
                    line += f" (about {entity})"
                if reason:
                    line += f" — {reason}"
                parts.append(line)

    history_lines: list[str] = []
    for (signal_type, kind), tally in sorted((outcome_counts or {}).items()):
        approved = tally.get("approved", 0)
        declined = tally.get("declined", 0) + tally.get("auto_disabled", 0) + tally.get("expired", 0)
        if approved + declined:
            history_lines.append(
                f"- {signal_type} watches grounded in a {kind or 'unknown'}: "
                f"{approved} approved / {declined} declined or dropped"
            )
    for specialist, tally in sorted((specialist_counts or {}).items()):
        approved = tally.get("approved", 0)
        declined = tally.get("declined", 0) + tally.get("auto_disabled", 0) + tally.get("expired", 0)
        if approved + declined:
            history_lines.append(
                f"- proposals from {specialist}: {approved} approved / {declined} declined or dropped"
            )
    if history_lines:
        parts.append("\nHOW PAST PROPOSALS TURNED OUT:")
        parts.extend(history_lines[:12])

    interest_lines = _render_department_interests(departments)
    if interest_lines:
        parts.append(
            "\nDEPARTMENT WATCH INTERESTS (entities a department asked to have "
            "watched — ground a proposal about one in that entity):"
        )
        parts.extend(interest_lines)

    parts.append(
        "\nDecide which ongoing sources are worth monitoring and call "
        "propose_watch for each — real ticker/feed targets only, preferring "
        "URLs from the findings above, each tied to a named company entity."
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

    routed = [
        t for t in tool_calls
        if t.get("tool") not in ("add_watchlist_entry", "propose_watch")
    ]
    if routed:
        lines.append("\n## Actions routed")
        for t in routed[:25]:
            mark = "✓" if t.get("ok") else "✗"
            lines.append(
                f"- {mark} `{t.get('tool', '')}` — {t.get('result_preview', '')}"
            )

    watchlist_calls = [
        t for t in tool_calls
        if t.get("tool") in ("add_watchlist_entry", "propose_watch")
    ]
    added = [
        t for t in watchlist_calls
        if t.get("outcome") == "added" or (t.get("tool") == "add_watchlist_entry" and t.get("ok"))
    ]
    suggested = [t for t in watchlist_calls if t.get("outcome") == "suggested"]
    rejected = [t for t in watchlist_calls if t not in added and t not in suggested]
    if added:
        lines.append("\n## Now watching")
        for t in added[:25]:
            lines.append(f"- ✓ {_inline(t.get('result_preview', ''))}")
    if suggested:
        lines.append("\n## Suggested for your approval (see /watchlist)")
        for t in suggested[:25]:
            lines.append(f"- ? {_inline(t.get('result_preview', ''))}")
    if rejected:
        # Show refusals too (declined target, bad feed, policy) so a proposal
        # that didn't take isn't silently dropped from the artifact.
        lines.append("\n## Watch proposals not taken")
        for t in rejected[:25]:
            lines.append(f"- ✗ {_inline(t.get('result_preview', ''))}")

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
