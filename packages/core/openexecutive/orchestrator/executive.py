from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from openexecutive.audit import bind_turn, clear_turn, set_turn
from openexecutive.audit import log_event as audit_log
from openexecutive.audit.redaction import (
    audit_tool_input,
    audit_tool_input_full,
    audit_tool_result,
    audit_tool_result_full,
)
from openexecutive.config import get_settings
from openexecutive.memory.honcho_client import ReasoningLevel as HonchoReasoningLevel
from openexecutive.orchestrator.action_chips import summarize_action
from openexecutive.orchestrator.alert_tools import (
    CREATE_ALERT_TOOL,
    handle_create_alert,
)
from openexecutive.orchestrator.artifact_tools import (
    DRAFT_ARTIFACT_TOOL,
    handle_draft_artifact,
)
from openexecutive.orchestrator.broadcast_tools import (
    BROADCAST_TOOL_HANDLERS,
    BROADCAST_TOOLS,
)
from openexecutive.orchestrator.calendar_tools import (
    CALENDAR_TOOL_HANDLERS,
    CALENDAR_TOOLS,
)
from openexecutive.orchestrator.debug_events import DebugCollector
from openexecutive.orchestrator.department_tools import (
    DEPARTMENT_TOOL_HANDLERS,
    DEPARTMENT_TOOLS,
)
from openexecutive.orchestrator.form_tools import (
    FORM_TOOL_HANDLERS,
    FORM_TOOLS,
    PROPOSE_FORM_VALUES,
    build_form_patch_event,
)
from openexecutive.orchestrator.mcp_gateway import MCP_TOOL_NAMES, MCP_TOOLS, MCPGateway
from openexecutive.orchestrator.onboarding_tools import (
    ONBOARDING_TOOL_HANDLERS,
    ONBOARDING_TOOLS,
)
from openexecutive.orchestrator.people_tools import (
    PEOPLE_TOOL_HANDLERS,
    PEOPLE_TOOLS,
)
from openexecutive.orchestrator.research_tools import (
    RESEARCH_TOOL_HANDLERS,
    RESEARCH_TOOLS,
)
from openexecutive.orchestrator.router import (
    SPECIALIST_TOOLS,
    partition_specialist_fanout,
    route_parallel,
)
from openexecutive.orchestrator.schedule_tools import (
    SCHEDULE_TOOL_HANDLERS,
    SCHEDULE_TOOLS,
    current_session,
)
from openexecutive.orchestrator.session import Session
from openexecutive.orchestrator.skills_tools import SKILL_TOOL_HANDLERS, SKILL_TOOLS
from openexecutive.orchestrator.talent_tools import (
    TALENT_TOOL_HANDLERS,
    TALENT_TOOLS,
)
from openexecutive.orchestrator.watchlist_tools import (
    WATCHLIST_TOOL_HANDLERS,
    WATCHLIST_TOOLS,
)
from openexecutive.orchestrator.web_search_tool import (
    WEB_SEARCH_TOOL_NAME,
    build_web_search_tool,
)
from openexecutive.orchestrator.workflow_authoring_tools import (
    WORKFLOW_AUTHORING_TOOL_HANDLERS,
    WORKFLOW_AUTHORING_TOOLS,
)
from openexecutive.orchestrator.workflow_run_tools import (
    WORKFLOW_RUN_TOOL_HANDLERS,
    WORKFLOW_RUN_TOOLS,
)
from openexecutive.prompts.cache_manager import build_system_blocks
from openexecutive.providers import get_provider
from openexecutive.security.grimdall_guard import guard_tool_call, is_enabled

logger = logging.getLogger(__name__)


def _trunc(value: Any, limit: int = 200) -> str:
    """Render *value* for a log line, capped at *limit* chars.

    Always uses `repr()` so strings stay quoted (matching the prior `%r`
    behaviour these call sites used) and dict/list payloads stay distinct.
    Appends `…[truncated N chars]` so the reader can tell when content was cut.
    """
    s = repr(value)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}…[truncated {len(s) - limit} chars]"


def _build_current_speaker_block(person_id: int | None) -> str | None:
    """Render the body of a <current_speaker> hint naming who is in the room.

    Without this the Executive has no idea which human it is talking to on
    the web/briefing path, so it routinely offers to "loop in" or DM the very
    person reading the reply — absurd when that person is the principal, the
    audience every brief and nudge is written for. Returns None when the
    speaker can't be resolved (CLI/curl with no caller, or a fresh install
    with no roster), leaving the user turn unchanged.
    """
    if person_id is None:
        return None
    try:
        from openexecutive.people.store import get_person
        person = get_person(person_id)
    except Exception:
        logger.warning("speaker_lookup_failed person_id=%s", person_id, exc_info=True)
        return None
    if person is None:
        return None

    who = f"{person.full_name} ({person.role})" if person.role else person.full_name
    if person.is_principal:
        return (
            f"You are speaking directly with {who} — the principal, the company "
            "owner these morning briefs, end-of-day digests, and proactive nudges "
            "are written for. They are already here in this conversation: address "
            "them directly and never offer to loop them in, DM them, notify them, "
            'escalate to them, or "pull them in."'
        )
    return (
        f"You are speaking directly with {who}. They are already here in this "
        "conversation: address them directly and never offer to loop them in, DM "
        "them, or notify them."
    )


_ALL_SKILL_TOOLS = [
    *SKILL_TOOLS,
    CREATE_ALERT_TOOL,
    DRAFT_ARTIFACT_TOOL,
    *SCHEDULE_TOOLS,
    *CALENDAR_TOOLS,
    *PEOPLE_TOOLS,
    *DEPARTMENT_TOOLS,
    *BROADCAST_TOOLS,
    *WATCHLIST_TOOLS,
    *RESEARCH_TOOLS,
    *WORKFLOW_AUTHORING_TOOLS,
    *WORKFLOW_RUN_TOOLS,
    *TALENT_TOOLS,
    *ONBOARDING_TOOLS,
    *FORM_TOOLS,
]
_ALL_SKILL_HANDLERS = {
    **SKILL_TOOL_HANDLERS,
    "create_alert": handle_create_alert,
    "draft_artifact": handle_draft_artifact,
    **SCHEDULE_TOOL_HANDLERS,
    **CALENDAR_TOOL_HANDLERS,
    **PEOPLE_TOOL_HANDLERS,
    **DEPARTMENT_TOOL_HANDLERS,
    **BROADCAST_TOOL_HANDLERS,
    **WATCHLIST_TOOL_HANDLERS,
    **RESEARCH_TOOL_HANDLERS,
    **WORKFLOW_AUTHORING_TOOL_HANDLERS,
    **WORKFLOW_RUN_TOOL_HANDLERS,
    **TALENT_TOOL_HANDLERS,
    **ONBOARDING_TOOL_HANDLERS,
    **FORM_TOOL_HANDLERS,
}


def _sync_consulted_departments_to_honcho(
    consulted_specialists: list[str],
    user_message: str,
    response: str,
    *,
    person_id: int | None,
    session_id: str | None,
    co_present_person_ids: list[int] | None,
) -> None:
    """Fire-and-forget per-dept Honcho sync for every department whose
    specialist contributed to this turn.

    Resolves each consulted specialist key → department slug via the
    departments registry, deduplicates (the same specialist may have been
    consulted in multiple iterations of the tool-use loop), and emits
    one ``sync_department_turn`` per unique dept slug. Person and other
    consulted depts join as co-present peers so Honcho's peer graph can
    cross-pollinate the dept and person representations later.

    Specialists with no owning department (e.g. ``triage``) are skipped.
    """
    if not consulted_specialists:
        return
    from openexecutive.departments.registry import slug_for_specialist
    from openexecutive.memory.honcho_client import sync_department_turn

    dept_slugs: list[str] = []
    seen: set[str] = set()
    for key in consulted_specialists:
        slug = slug_for_specialist(key)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        dept_slugs.append(slug)
    for slug in dept_slugs:
        sync_department_turn(
            user_message,
            response,
            department_slug=slug,
            session_id=session_id,
            originating_person_id=person_id,
            co_present_person_ids=co_present_person_ids,
            co_present_department_slugs=[s for s in dept_slugs if s != slug],
        )


def _system_block_names(blocks: list[dict[str, Any]]) -> list[str]:
    """Derive stable, human-readable labels for system prompt blocks.

    Used to annotate specialist_consult / tool_invocation full payloads so
    the timeline can show which cached blocks were live during each step.
    Block layout in cache_manager.build_system_blocks() is fixed:
      0 = persona + knowledge_index (1h TTL)
      1 = company_profile + org_context (5m TTL)
    """
    names: list[str] = []
    for i, b in enumerate(blocks):
        ttl = (b.get("cache_control") or {}).get("ttl", "5m")
        if i == 0:
            names.append(f"persona+knowledge_index (ttl={ttl})")
        elif i == 1:
            names.append(f"company_profile+org_context (ttl={ttl})")
        else:
            names.append(f"system_block_{i} (ttl={ttl})")
    return names


def _emit_memory_snapshot(
    *,
    session_id: str | None,
    turn_id: str,
    user_message: str,
    episodic_context: str,
    retrieved_context: str,
    system_blocks: list[dict[str, Any]],
    history_len: int,
    company_profile: Any,
    model: str,
    committee: bool,
) -> None:
    """One memory_snapshot per turn: what was in the prompt before the API call.

    Fire-and-forget. Captures both the *summary* (for the audit list view)
    and the *full* episodic + profile text (for the flow-chart detail pane).
    """
    # Only hash the company profile — the rendered prompt block can contain
    # revenue, headcount, hiring plans, and other commercially sensitive
    # facts. The hash is enough to spot "profile changed across turns" in
    # the flow view; the full block is reachable via /company-profile for
    # operators with explicit permission.
    profile_hash: str | None = None
    if company_profile is not None:
        try:
            block = company_profile.to_prompt_block() or ""
            profile_hash = hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]
        except Exception:
            # Profile rendering can fail when company data is malformed;
            # don't let it break the chat turn.
            profile_hash = None
    audit_log(
        "memory_snapshot",
        f"turn entry: model={model} history={history_len} episodic={len(episodic_context)}c rag={len(retrieved_context)}c",
        session_id=session_id,
        turn_id=turn_id,
        actor="executive",
        details={
            "model": model,
            "committee_review": committee,
            "history_len": history_len,
            "episodic_chars": len(episodic_context),
            "retrieved_chars": len(retrieved_context),
            "user_message_preview": user_message[:160],
            "company_profile_hash": profile_hash,
            "system_blocks": _system_block_names(system_blocks),
        },
        # Do NOT duplicate the raw user_message here — chat_turn already
        # persists it in its own row. The episodic_context and
        # retrieved_context are LLM-generated summaries / retrieved chunks
        # already produced for this turn; capturing them here lets the
        # flow chart show "what the agent saw" without re-running RAG.
        full={
            "episodic_context": episodic_context,
            "retrieved_context": retrieved_context,
            "system_blocks": _system_block_names(system_blocks),
        },
    )


def _emit_cache_event(
    *,
    session_id: str | None,
    turn_id: str,
    iteration: int,
    final_msg: Any,
    model: str,
    actor: str = "executive",
) -> None:
    """Capture token + cache stats from an Anthropic streaming response.

    Reads from `final_msg.usage` (a SimpleNamespace from the provider
    abstraction in providers/translator.py). All getattrs are guarded so
    a provider that doesn't surface a particular field still emits a row
    with what it does have — never blocks the caller.
    """
    usage = getattr(final_msg, "usage", None)
    if usage is None:
        return
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    stop_reason = getattr(final_msg, "stop_reason", None)
    # Actual USD charged for this call, surfaced by OpenRouter when usage
    # accounting is enabled. None on the Anthropic-direct path (no cost wire);
    # stored as-is so aggregation treats a missing cost as 0.
    raw_cost = getattr(usage, "cost", None)
    try:
        cost_usd = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    audit_log(
        "cache_event",
        f"{model} iter={iteration} in={inp} out={out} cache_read={cache_read} cache_create={cache_create} cost={cost_usd} stop={stop_reason}",
        session_id=session_id,
        turn_id=turn_id,
        actor=actor,
        details={
            "model": model,
            "iteration": iteration,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "cost_usd": cost_usd,
            "stop_reason": stop_reason,
        },
    )


class Executive:
    """The Executive orchestrator — the single voice the user always interacts with.

    Internally routes to specialist agents via Anthropic tool use, but synthesizes
    everything into one coherent executive response. The sub-agent architecture is
    never exposed to the user.
    """

    def __init__(self, mcp_gateway: MCPGateway | None = None) -> None:
        self._settings = get_settings()
        self._mcp_gateway = mcp_gateway
        self._mcp_tools = MCP_TOOLS if mcp_gateway is not None else []

    def _build_messages(
        self,
        session: Session,
        user_message: str,
        retrieved_context: str = "",
        episodic_context: str = "",
        attachment_blocks: list[dict[str, Any]] | None = None,
        peer_memory_context: str = "",
        person_id: int | None = None,
        briefing_context: str = "",
        page_context_block: str = "",
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        history = session.get_recent_history()

        for i, turn in enumerate(history):
            msg: dict[str, Any] = {"role": turn["role"], "content": turn["content"]}
            # Cache the penultimate assistant turn to build a rolling cache
            if (
                turn["role"] == "assistant"
                and i == len(history) - 2
                and self._settings.enable_caching
                and isinstance(turn["content"], str)
            ):
                msg["content"] = [
                    {
                        "type": "text",
                        "text": turn["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            messages.append(msg)

        user_content_parts: list[dict[str, Any]] = []

        # Name the human in the room first so the model never offers to loop
        # in / DM the person it is replying to. Lives in the user turn (never
        # the cached system block) since the speaker varies per request.
        speaker_block = _build_current_speaker_block(person_id)
        if speaker_block:
            user_content_parts.append(
                {"type": "text", "text": f"<current_speaker>\n{speaker_block}\n</current_speaker>"}
            )

        if episodic_context:
            user_content_parts.append(
                {"type": "text", "text": f"<past_decisions>\n{episodic_context}\n</past_decisions>"}
            )
        # Current open-alert digest (the items behind the /today "What's going
        # on" narrative + proposal cards) so the Executive can discuss an item
        # the principal clicked or named. Lives in the user turn alongside the
        # other per-request context — never a cached system block.
        if briefing_context:
            user_content_parts.append(
                {"type": "text", "text": f"<briefing>\n{briefing_context}\n</briefing>"}
            )
        # Per-person memory from Honcho (when HONCHO_ENABLED + person_id
        # available). Goes in the user turn alongside episodic / retrieved
        # context so it never lives in the cached system block.
        if peer_memory_context:
            user_content_parts.append(
                {"type": "text", "text": f"<peer_memory>\n{peer_memory_context}\n</peer_memory>"}
            )
        # What the user is looking at right now (Ask OE panel turns only):
        # route, page title, guide excerpt, and — when a form is on screen —
        # its field descriptor. Per-request by nature, so it lives in the
        # user turn alongside the other dynamic context, never a cached
        # system block.
        if page_context_block:
            user_content_parts.append(
                {"type": "text", "text": f"<page_context>\n{page_context_block}\n</page_context>"}
            )
        if retrieved_context:
            user_content_parts.append(
                {
                    "type": "text",
                    "text": f"<retrieved_context>\n{retrieved_context}\n</retrieved_context>",
                }
            )

        # Attachment blocks (images) are inserted before the text message so
        # the model sees the visual context first, then the user's question.
        # Text-extracted document content is already prepended to user_message
        # by the caller — no separate block needed for those.
        if attachment_blocks:
            user_content_parts.extend(attachment_blocks)

        user_content_parts.append({"type": "text", "text": user_message})
        messages.append({"role": "user", "content": user_content_parts})

        return messages

    # Sentinel yielded when specialist calls are in flight — lets callers send keepalives.
    _THINKING = "\x01"

    async def stream_chat(
        self,
        user_message: str,
        session: Session,
        retrieved_context: str = "",
        episodic_context: str = "",
        debug_collector: DebugCollector | None = None,
        max_iterations: int = 15,
        attachment_blocks: list[dict[str, Any]] | None = None,
        person_id: int | None = None,
        co_present_person_ids: list[int] | None = None,
        peer_memory_reasoning_level: HonchoReasoningLevel = "minimal",
        peer_memory_context: str | None = None,
        briefing_context: str = "",
        page_context_block: str = "",
    ) -> AsyncIterator[str | dict[str, Any]]:
        """Stream a response from the Executive, routing to specialists as needed.

        ``person_id`` (when provided) keys the Honcho per-person memory
        prefetch + post-turn sync. The integration adapters (Slack,
        Discord, Telegram, email) resolve the inbound user to a
        :class:`openexecutive.people.models.Person` and pass the id here;
        the web/SSE path leaves it ``None`` and the Honcho layer no-ops.

        ``co_present_person_ids`` (when provided) lists every distinct
        human in the conversation context besides the sender (Discord
        thread participants, Slack channel speakers, email cc'd people).
        They're added to the Honcho session as peers so peer-of-peer
        reasoning (via the ``ask_about_person`` tool) has data.

        ``peer_memory_reasoning_level`` trades latency for synthesis
        depth on Honcho's dialectic prefetch. Default ``"minimal"``
        bounds Honcho's synthesis depth — production telemetry showed
        ``"low"`` was hitting the ~5s tail consistently for power-user
        peers as their representations grew. The committee path keeps
        ``"medium"`` since it already pays the deep-review latency cost.
        """
        logger.info(
            "chat turn: %s",
            _trunc(user_message, 80),
            extra={"turn_break": True},
        )
        # Expose the current session to tool handlers (e.g. schedule_followup)
        # without threading it through every signature.
        current_session.set(session)
        # Persona and model can be overridden via the Agent Council admin UI.
        # Override is admin-set (not per-request dynamic), so placing it in the
        # cached block is fine — cache misses once on change, then hits normally.
        # Wrap in try/except so an override-store outage doesn't block chat.
        persona_override: str | None = None
        voice_persona_body: str | None = None
        effective_model = self._settings.default_model
        try:
            from openexecutive.agents.overrides import (
                EXECUTIVE_AGENT_ID,
            )
            from openexecutive.agents.overrides import (
                get_override as _get_override,
            )
            from openexecutive.personas.loader import get_active_body as _get_voice_body
            _ov = _get_override(EXECUTIVE_AGENT_ID)
            # Use falsy check so an empty-string override is treated as absent
            # (an empty persona sent to the API would produce garbled output).
            if _ov is not None and _ov.prompt:
                persona_override = _ov.prompt
            if _ov is not None and _ov.model:
                effective_model = _ov.model
            voice_persona_body = _get_voice_body(_ov.voice_persona_slug if _ov else None)
        except Exception:
            logger.exception("Failed to load executive override; using defaults")
        system_blocks = build_system_blocks(
            session.company_profile,
            mcp_enabled=self._mcp_gateway is not None,
            persona_override=persona_override,
            voice_persona_body=voice_persona_body,
        )
        # turn_id ties every downstream audit row (knowledge_retrieval,
        # specialist_consult, tool_invocation, cache_event, peer_memory)
        # to this exchange. set_turn binds the audit ContextVars scoped
        # to the current asyncio task — restored automatically on exit.
        # Bound BEFORE prefetch so the Honcho audit row inherits the
        # turn link (otherwise it lands with session_id=NULL and is
        # invisible in the session-grouped audit view).
        turn_id = f"t-{uuid.uuid4().hex[:12]}"

        t0 = time.monotonic()
        full_response = ""
        with set_turn(session_id=session.session_id, turn_id=turn_id):
            # Per-person prefetch from Honcho. Runs once per turn (NOT inside
            # the tool-call loop) so a long multi-tool turn doesn't accrue
            # multiple round trips. The wrapper enforces a hard timeout and
            # returns "" on any error, so a Honcho outage degrades silently.
            #
            # Callers can pre-resolve this in parallel with RAG and episodic
            # context (see api/routes/chat.py) and pass the result through —
            # in that case we skip the await here entirely.
            if peer_memory_context is None:
                from openexecutive.memory.honcho_client import prefetch as _honcho_prefetch
                peer_memory_context = await _honcho_prefetch(
                    user_message,
                    person_id=person_id,
                    session_id=session.session_id,
                    reasoning_level=peer_memory_reasoning_level,
                )
            messages = self._build_messages(
                session,
                user_message,
                retrieved_context,
                episodic_context,
                attachment_blocks,
                peer_memory_context=peer_memory_context,
                person_id=person_id,
                briefing_context=briefing_context,
                page_context_block=page_context_block,
            )

            _emit_memory_snapshot(
                session_id=session.session_id,
                turn_id=turn_id,
                user_message=user_message,
                episodic_context=episodic_context,
                retrieved_context=retrieved_context,
                system_blocks=system_blocks,
                history_len=len(session.get_recent_history()),
                company_profile=session.company_profile,
                model=effective_model,
                committee=False,
            )
            consulted: list[str] = []
            async for item in self._stream_agent_loop(
                system_blocks,
                messages,
                model=effective_model,
                max_iterations=max_iterations,
                episodic_context=episodic_context,
                debug_collector=debug_collector,
                consulted_out=consulted,
                turn_id=turn_id,
            ):
                if isinstance(item, str) and item != self._THINKING:
                    full_response += item
                yield item

        if debug_collector:
            evt = debug_collector.emit("synthesis_done", {
                "total_duration_ms": round((time.monotonic() - t0) * 1000),
                "response_length": len(full_response),
            })
            yield debug_collector.to_sse_dict(evt)

        session.add_user_message(user_message)
        session.add_assistant_message(full_response)

        # Audit the Executive's outbound response. Without this, the audit
        # log only records the *inputs* to a turn (inbound message, memory
        # snapshot, retrievals, specialist consults, tool calls) but never
        # the response itself — making sessions hard to follow. Mirrors the
        # web /api/chat route's pattern, but at the unified place that every
        # entry point (web stream, .chat() wrapper used by Discord / Slack
        # / Telegram / Email / Google Chat) flows through.
        # Only emit on real text — an empty response means the agent loop
        # produced only tool_use blocks or hit max_iterations without text,
        # and an audit row for "" adds noise without signal.
        if full_response.strip():
            audit_log(
                "chat_turn",
                f"Executive: {full_response[:200]}",
                session_id=session.session_id,
                turn_id=turn_id,
                actor="executive",
                details={
                    "direction": "out",
                    "response_len": len(full_response),
                    "duration_s": round(time.monotonic() - t0, 3),
                    "model": effective_model,
                    "committee": False,
                },
                full={"response": full_response},
            )

        from openexecutive.memory.episodic import (
            MIN_TURN_CHARS_FOR_EXTRACTION,
            schedule_extraction,
        )
        if len(full_response) + len(user_message) >= MIN_TURN_CHARS_FOR_EXTRACTION:
            schedule_extraction(user_message, full_response, session_id=session.session_id)

        # Mirror the completed exchange into Honcho so its server-side
        # extraction can update the peer card. Fire-and-forget; the
        # wrapper no-ops when person_id is None or Honcho is disabled.
        #
        # Re-bind the audit ContextVars for the duration of these calls so
        # the fire-and-forget tasks they schedule can snapshot the right
        # session_id/turn_id (the main `with set_turn(...)` block above
        # exited at the end of the `async for` so the ContextVars are back
        # to None by now). Without this wrapper, every sync_turn /
        # sync_department_turn audit row would land with session_id=NULL
        # and be invisible in the per-session audit view.
        with set_turn(session_id=session.session_id, turn_id=turn_id):
            from openexecutive.memory.honcho_client import sync_turn as _honcho_sync
            _honcho_sync(
                user_message,
                full_response,
                person_id=person_id,
                session_id=session.session_id,
                co_present_person_ids=co_present_person_ids,
            )
            # Per-dept mirror for every department whose specialist contributed
            # this turn. Runs after the person-side sync so dept and person
            # syncs are visible in the audit log as a related pair.
            _sync_consulted_departments_to_honcho(
                consulted,
                user_message,
                full_response,
                person_id=person_id,
                session_id=session.session_id,
                co_present_person_ids=co_present_person_ids,
            )

    async def stream_chat_with_committee(
        self,
        user_message: str,
        session: Session,
        retrieved_context: str = "",
        episodic_context: str = "",
        debug_collector: DebugCollector | None = None,
        max_iterations: int = 15,
        attachment_blocks: list[dict[str, Any]] | None = None,
        person_id: int | None = None,
        co_present_person_ids: list[int] | None = None,
        peer_memory_reasoning_level: HonchoReasoningLevel = "medium",
        peer_memory_context: str | None = None,
        briefing_context: str = "",
        page_context_block: str = "",
    ) -> AsyncIterator[str | dict[str, Any]]:
        """Committee-reviewed variant of stream_chat.

        Flow: drafting (silent — text chunks accumulated, debug events
        passed through) → reviewing (3 parallel critique calls) →
        finalizing (revision streams to caller as text chunks).

        Only the revised response is yielded as text and persisted to
        history; the draft and critiques are recorded via audit_log so
        cache continuity is preserved against the polished version.

        Defaults ``peer_memory_reasoning_level="medium"`` (vs
        ``"minimal"`` on the non-committee path) — committee turns
        already pay deep-review latency, so a richer Honcho synthesis
        is worth the extra second + Sonnet-tier OpenRouter cost.
        """
        logger.info(
            "chat turn (committee): %s",
            _trunc(user_message, 80),
            extra={"turn_break": True},
        )
        current_session.set(session)

        persona_override: str | None = None
        voice_persona_body: str | None = None
        effective_model = self._settings.default_model
        try:
            from openexecutive.agents.overrides import (
                EXECUTIVE_AGENT_ID,
            )
            from openexecutive.agents.overrides import (
                get_override as _get_override,
            )
            from openexecutive.personas.loader import get_active_body as _get_voice_body
            _ov = _get_override(EXECUTIVE_AGENT_ID)
            if _ov is not None and _ov.prompt:
                persona_override = _ov.prompt
            if _ov is not None and _ov.model:
                effective_model = _ov.model
            voice_persona_body = _get_voice_body(_ov.voice_persona_slug if _ov else None)
        except Exception:
            logger.exception("Failed to load executive override; using defaults")

        system_blocks = build_system_blocks(
            session.company_profile,
            mcp_enabled=self._mcp_gateway is not None,
            persona_override=persona_override,
            voice_persona_body=voice_persona_body,
        )
        # turn_id covers both the draft and (later) the revision pass so a
        # committee-reviewed turn renders as one flow chart, not two.
        # Generated and bound BEFORE the Honcho prefetch so the peer_memory
        # audit row inherits the turn link (else it lands with
        # session_id=NULL and is invisible in the session-grouped view).
        turn_id = f"t-{uuid.uuid4().hex[:12]}"
        # Stash on the function frame so the closing audit_log("committee_review")
        # at the end of this function can carry the same turn_id.
        _committee_turn_id = turn_id

        # Bind ContextVars for the full turn (draft + review + revision).
        # bind_turn (no reset) matches the existing `current_session.set()`
        # pattern used for scheduling — fine for one-task-per-turn handlers.
        # A `with set_turn(...)` would force wrapping the entire committee
        # body in an extra indent level for negligible safety gain.
        #
        # Tradeoff note: binding before prefetch widens the window between
        # bind_turn and the eventual clear_turn at function end. If
        # something between here and the first clear_turn raises uncaught,
        # the binding leaks to the next turn on this asyncio task. Prefetch
        # swallows its own exceptions; _build_messages is pure. If a future
        # refactor adds a raising step in this window, wrap that step in
        # try/finally with clear_turn() or migrate to `with set_turn`.
        bind_turn(session_id=session.session_id, turn_id=turn_id)

        # Per-person prefetch from Honcho (see stream_chat for rationale).
        # Skip when caller pre-resolved (e.g. parallel context fetch in route).
        if peer_memory_context is None:
            from openexecutive.memory.honcho_client import prefetch as _honcho_prefetch
            peer_memory_context = await _honcho_prefetch(
                user_message,
                person_id=person_id,
                session_id=session.session_id,
                reasoning_level=peer_memory_reasoning_level,
            )
        messages = self._build_messages(
            session,
            user_message,
            retrieved_context,
            episodic_context,
            attachment_blocks,
            peer_memory_context=peer_memory_context,
            person_id=person_id,
            briefing_context=briefing_context,
            page_context_block=page_context_block,
        )

        # ----- Phase 1: drafting -----------------------------------------
        yield {
            "type": "phase",
            "phase": "drafting",
            "session_id": session.session_id,
        }

        t0 = time.monotonic()
        draft = ""
        consulted: list[str] = []
        specialist_outputs: dict[str, str] = {}
        _emit_memory_snapshot(
            session_id=session.session_id,
            turn_id=turn_id,
            user_message=user_message,
            episodic_context=episodic_context,
            retrieved_context=retrieved_context,
            system_blocks=system_blocks,
            history_len=len(session.get_recent_history()),
            company_profile=session.company_profile,
            model=effective_model,
            committee=True,
        )

        async for item in self._stream_agent_loop(
            system_blocks,
            messages,
            model=effective_model,
            max_iterations=max_iterations,
            episodic_context=episodic_context,
            debug_collector=debug_collector,
            consulted_out=consulted,
            specialist_outputs_out=specialist_outputs,
            turn_id=turn_id,
        ):
            # Swallow draft text and the THINKING sentinel — the user sees
            # only the revised stream. Pass debug-event dicts through so the
            # UI still gets specialist routing visibility.
            if isinstance(item, str):
                if item != self._THINKING:
                    draft += item
            else:
                yield item

        draft_ms = round((time.monotonic() - t0) * 1000)
        logger.info(
            "committee.draft_done duration_ms=%d consulted=%s draft_chars=%d",
            draft_ms,
            consulted,
            len(draft),
        )

        # If the draft came back empty (timeout, max_iterations, etc.) skip
        # review — there is nothing to critique. Still emit the remaining
        # phase events so the UI's stepper completes; otherwise it stays
        # frozen on "drafting" until the connection closes.
        if not draft.strip():
            yield {
                "type": "phase",
                "phase": "reviewing",
                "session_id": session.session_id,
            }
            yield {
                "type": "phase",
                "phase": "finalizing",
                "session_id": session.session_id,
            }
            fallback = "I was unable to complete the analysis. Please try again."
            yield fallback
            session.add_user_message(user_message)
            session.add_assistant_message(fallback)
            # Reset audit ContextVars so this task doesn't leak the turn_id
            # to a follow-up turn that runs on the same task.
            clear_turn()
            return

        # ----- Phase 2: reviewing ----------------------------------------
        yield {
            "type": "phase",
            "phase": "reviewing",
            "session_id": session.session_id,
        }

        from openexecutive.orchestrator.committee import Committee
        committee = Committee(
            reviewer_model=self._settings.default_model,
        )
        # Mirror the upcoming review onto the debug stream so the Agent
        # Activity panel shows committee progress alongside the inline
        # phase events that drive the in-chat stepper.
        planned_reviewers = [r.name for r in committee.select_reviewers(consulted)]
        if debug_collector:
            evt = debug_collector.emit("committee_review_start", {
                "reviewers": planned_reviewers,
                "consulted": consulted,
                "draft_length": len(draft),
            })
            yield debug_collector.to_sse_dict(evt)

        review_t0 = time.monotonic()
        critiques = await committee.review(
            user_message=user_message,
            draft=draft,
            consulted=consulted,
            specialist_outputs=specialist_outputs,
        )
        review_ms = round((time.monotonic() - review_t0) * 1000)
        logger.info(
            "committee.review_done duration_ms=%d critiques=%s",
            review_ms,
            [(c.reviewer_name, c.severity) for c in critiques],
        )

        if debug_collector:
            evt = debug_collector.emit("committee_review_done", {
                "review_ms": review_ms,
                "critiques": [
                    {
                        "reviewer": c.reviewer_name,
                        "severity": c.severity,
                        # Short preview only — the debug panel surfaces
                        # this on expand, full text lives in audit_log.
                        "critique_preview": c.critique[:200],
                    }
                    for c in critiques
                ],
            })
            yield debug_collector.to_sse_dict(evt)

        # Surface critique severities (not full text) to the UI for debug.
        # Full critiques stay server-side via audit_log below.
        for c in critiques:
            yield {
                "type": "committee_critique",
                "reviewer": c.reviewer_name,
                "severity": c.severity,
                "session_id": session.session_id,
            }

        # ----- Phase 3: finalizing (revision streams to caller) ----------
        yield {
            "type": "phase",
            "phase": "finalizing",
            "session_id": session.session_id,
        }

        if debug_collector:
            evt = debug_collector.emit("committee_revision_start", {
                "critique_count": len(critiques),
            })
            yield debug_collector.to_sse_dict(evt)

        from openexecutive.prompts.committee_prompts import build_revision_user_turn
        revision_turn = build_revision_user_turn(
            [c.as_dict() for c in critiques]
        )
        revision_messages = [
            *messages,
            {"role": "assistant", "content": draft},
            {"role": "user", "content": revision_turn},
        ]

        revision_t0 = time.monotonic()
        final_response = ""
        revision_final_msg: Any = None
        async with get_provider(effective_model).messages_stream(
            model=effective_model,
            max_tokens=8192,
            system=system_blocks,  # type: ignore[arg-type]
            messages=revision_messages,  # type: ignore[arg-type]
        ) as stream:
            async for event in stream:
                if (
                    hasattr(event, "type")
                    and event.type == "content_block_delta"
                    and hasattr(event, "delta")
                    and hasattr(event.delta, "type")
                    and event.delta.type == "text_delta"
                ):
                    final_response += event.delta.text
                    yield event.delta.text
            try:
                revision_final_msg = await stream.get_final_message()
            except Exception:
                # Provider variance — some adapters may not implement
                # get_final_message after we've already iterated. Don't
                # let an audit-only branch break the revision response.
                revision_final_msg = None
        revision_ms = round((time.monotonic() - revision_t0) * 1000)
        if revision_final_msg is not None:
            _emit_cache_event(
                session_id=session.session_id,
                turn_id=_committee_turn_id,
                iteration=0,  # 0 = revision pass (post-draft, post-review)
                final_msg=revision_final_msg,
                model=effective_model,
                actor="committee_revision",
            )

        if debug_collector:
            evt = debug_collector.emit("synthesis_done", {
                "total_duration_ms": round((time.monotonic() - t0) * 1000),
                "response_length": len(final_response),
                "committee": True,
                "draft_length": len(draft),
                "review_ms": review_ms,
                "revision_ms": revision_ms,
            })
            yield debug_collector.to_sse_dict(evt)

        session.add_user_message(user_message)
        # Guard against a revision pass that produced no text (only tool_use
        # blocks, model_stop, etc.). Persisting an empty assistant turn
        # corrupts the in-memory history with a phantom turn that future
        # cache hits will key off of.
        if final_response.strip():
            session.add_assistant_message(final_response)

            # Audit the committee's final response. Same rationale as the
            # non-committee path in stream_chat: callers should always be
            # able to see the Executive's outbound text in the audit log,
            # not just the inputs. committee=True so the UI can distinguish
            # a committee-revised response from a normal one.
            audit_log(
                "chat_turn",
                f"Executive: {final_response[:200]}",
                session_id=session.session_id,
                turn_id=_committee_turn_id,
                actor="executive",
                details={
                    "direction": "out",
                    "response_len": len(final_response),
                    "duration_s": round(time.monotonic() - t0, 3),
                    "model": effective_model,
                    "committee": True,
                    "draft_length": len(draft),
                },
                full={"response": final_response, "draft": draft},
            )

        audit_log(
            "committee_review",
            f"Committee revised draft (consulted={consulted})",
            session_id=session.session_id,
            turn_id=_committee_turn_id,
            actor="committee",
            details={
                "draft_length": len(draft),
                "final_length": len(final_response),
                "consulted": consulted,
                "review_ms": review_ms,
                "revision_ms": revision_ms,
                "critiques": [
                    {
                        "reviewer": c.reviewer_name,
                        "severity": c.severity,
                        "critique": c.critique[:500],
                        "suggested_edits": c.suggested_edits[:500],
                    }
                    for c in critiques
                ],
            },
        )

        from openexecutive.memory.episodic import (
            MIN_TURN_CHARS_FOR_EXTRACTION,
            schedule_extraction,
        )
        if len(final_response) + len(user_message) >= MIN_TURN_CHARS_FOR_EXTRACTION:
            schedule_extraction(user_message, final_response, session_id=session.session_id)

        # Mirror the completed exchange into Honcho (see stream_chat for
        # rationale). Fire-and-forget; no-ops when person_id is None.
        from openexecutive.memory.honcho_client import sync_turn as _honcho_sync
        _honcho_sync(
            user_message,
            final_response,
            person_id=person_id,
            session_id=session.session_id,
            co_present_person_ids=co_present_person_ids,
        )
        _sync_consulted_departments_to_honcho(
            consulted,
            user_message,
            final_response,
            person_id=person_id,
            session_id=session.session_id,
            co_present_person_ids=co_present_person_ids,
        )

        # Reset audit ContextVars at normal completion. The abandoned-stream
        # case (SSE client drop mid-yield) doesn't reach here — accept that
        # narrow leak window; concurrent FastAPI requests run on their own
        # tasks and don't share the ContextVar value.
        clear_turn()

    async def _stream_agent_loop(
        self,
        system_blocks: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str = "",
        max_iterations: int = 15,
        episodic_context: str = "",
        debug_collector: DebugCollector | None = None,
        consulted_out: list[str] | None = None,
        specialist_outputs_out: dict[str, str] | None = None,
        turn_id: str | None = None,
    ) -> AsyncIterator[str | dict[str, Any]]:
        """Tool-use loop that yields text deltas as they arrive.

        Yields _THINKING before each specialist call round so callers can send
        keepalive/progress events while the blocking specialist calls run.
        Also yields debug event dicts when a debug_collector is provided.
        """
        current_messages = list(messages)
        last_full_text = ""
        specialists_consulted: list[str] = []

        for iteration in range(1, max_iterations + 1):
            logger.info(
                "iter %d/%d", iteration, max_iterations,
                extra={"iter_marker": True},
            )
            full_text = ""
            tool_uses: list[dict[str, Any]] = []
            response_content: list[dict[str, Any]] = []

            # If specialists were already consulted, this is the synthesis pass.
            # Emit synthesis_start before text chunks begin streaming.
            if debug_collector and specialists_consulted:
                evt = debug_collector.emit("synthesis_start", {
                    "specialist_count": len(specialists_consulted),
                    "specialists_consulted": specialists_consulted,
                })
                yield debug_collector.to_sse_dict(evt)

            # Client-side tools are sorted by name for cache stability; the
            # last one carries the cache_control marker. Anthropic server-side
            # tools (e.g. web_search) are appended after — they use a `type`
            # field instead of input_schema and cannot accept cache_control.
            client_tools = sorted(
                [*SPECIALIST_TOOLS, *_ALL_SKILL_TOOLS, *self._mcp_tools],
                key=lambda t: t["name"],
            )
            tools_with_cache: list[dict[str, Any]] = [
                *client_tools[:-1],
                {**client_tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ]
            web_search_tool = build_web_search_tool()
            if web_search_tool is not None:
                tools_with_cache.append(web_search_tool)
            stream_model = model or self._settings.default_model
            async with get_provider(stream_model).messages_stream(
                model=stream_model,
                max_tokens=8192,
                system=system_blocks,  # type: ignore[arg-type]
                tools=tools_with_cache,  # type: ignore[arg-type,list-item]
                messages=current_messages,  # type: ignore[arg-type]
            ) as stream:
                async for event in stream:
                    if (
                        hasattr(event, "type")
                        and event.type == "content_block_delta"
                        and hasattr(event, "delta")
                        and hasattr(event.delta, "type")
                        and event.delta.type == "text_delta"
                    ):
                        full_text += event.delta.text
                        yield event.delta.text

                final_msg = await stream.get_final_message()

            # cache_event: capture per-iteration token + cache stats so the
            # flow chart can show "this turn used N cache hits at iter K".
            # Always after the API call, never in the request path — does
            # not touch system_blocks / messages, so caching is unaffected.
            _emit_cache_event(
                session_id=getattr(current_session.get(), "session_id", None),
                turn_id=turn_id or "",
                iteration=iteration,
                final_msg=final_msg,
                model=stream_model,
            )

            web_search_queries: list[str] = []
            for block in final_msg.content:
                if block.type == "text":
                    response_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_uses.append({"id": block.id, "name": block.name, "input": block.input})
                    response_content.append(
                        {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                    )
                elif block.type == "server_tool_use":
                    # Anthropic resolves server tools (web_search) within the
                    # same generation; we just echo the block back on the next
                    # iteration so the model retains continuity. No handler runs.
                    response_content.append(block.model_dump(exclude_none=True))
                    if block.name == WEB_SEARCH_TOOL_NAME:
                        query = (block.input or {}).get("query", "")
                        if isinstance(query, str) and query:
                            web_search_queries.append(query)
                elif block.type == "web_search_tool_result":
                    response_content.append(block.model_dump(exclude_none=True))

            last_full_text = full_text

            if web_search_queries:
                session_id = getattr(current_session.get(), "session_id", None)
                if debug_collector:
                    evt = debug_collector.emit("web_search_invocation", {
                        "iteration": iteration,
                        "queries": web_search_queries,
                    })
                    yield debug_collector.to_sse_dict(evt)
                for q in web_search_queries:
                    audit_log(
                        "tool_invocation",
                        f"web_search: {q[:200]}",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor="executive",
                        details={
                            "tool": WEB_SEARCH_TOOL_NAME,
                            "kind": "server_tool",
                            "iteration": iteration,
                            "query": q[:500],
                        },
                        full={
                            "query": q,
                            "active_prompt_blocks": _system_block_names(system_blocks),
                        },
                    )

            if final_msg.stop_reason != "tool_use":
                return

            specialist_tool_uses = [tu for tu in tool_uses if tu["name"] == "consult_specialist"]
            skill_tool_uses = [tu for tu in tool_uses if tu["name"] in _ALL_SKILL_HANDLERS]
            mcp_tool_uses = [tu for tu in tool_uses if tu["name"] in MCP_TOOL_NAMES]

            # ── Grimdall execution guardrails (optional, default off) ──────
            # Evaluate every skill/MCP tool call before it dispatches. Enforce
            # mode replaces violating calls with an error tool_result (seeded
            # into results_by_id below); shadow mode logs a signed receipt and
            # proceeds unchanged. consult_specialist and server-side web_search
            # are internal LLM calls — not covered.
            session_id = getattr(current_session.get(), "session_id", None)
            grimdall_blocked: dict[str, str] = {}
            if is_enabled():
                for tu in (*skill_tool_uses, *mcp_tool_uses):
                    decision = guard_tool_call(
                        tu["name"],
                        tu.get("input") or {},
                        session_id=session_id,
                        turn_id=turn_id or "",
                    )
                    if not decision.allowed:
                        grimdall_blocked[tu["id"]] = decision.error_result()
                skill_tool_uses = [
                    tu for tu in skill_tool_uses if tu["id"] not in grimdall_blocked
                ]
                mcp_tool_uses = [
                    tu for tu in mcp_tool_uses if tu["id"] not in grimdall_blocked
                ]

            specialist_calls = [
                {
                    "specialist": tu["input"].get("specialist", ""),
                    "query": tu["input"].get("query", ""),
                    "context": tu["input"].get("context", ""),
                }
                for tu in specialist_tool_uses
            ]

            # Cap fan-out width per turn (inert by default — cap == roster
            # size). Dispatch the first `cap`; the rest get a skip tool_result
            # the model can react to. See router.partition_specialist_fanout.
            run_tool_uses, run_calls, skipped_results, fanout_cap = (
                partition_specialist_fanout(
                    specialist_tool_uses,
                    specialist_calls,
                    self._settings.max_parallel_specialists,
                )
            )

            if debug_collector and specialist_calls:
                evt = debug_collector.emit("routing_decision", {
                    "iteration": iteration,
                    "requested_count": len(specialist_calls),
                    "cap": fanout_cap,
                    "dispatched_count": len(run_calls),
                    "skipped_count": len(skipped_results),
                    "specialists": [
                        {
                            "specialist": c["specialist"],
                            "query": c["query"],
                            "context": c.get("context", "")[:200],
                        }
                        for c in run_calls
                    ],
                })
                yield debug_collector.to_sse_dict(evt)

            if debug_collector and skill_tool_uses:
                evt = debug_collector.emit("skill_invocation", {
                    "iteration": iteration,
                    "calls": [
                        {"tool": tu["name"], "input_preview": str(tu["input"])[:200]}
                        for tu in skill_tool_uses
                    ],
                })
                yield debug_collector.to_sse_dict(evt)

            if debug_collector and mcp_tool_uses:
                evt = debug_collector.emit("mcp_invocation", {
                    "iteration": iteration,
                    "calls": [
                        {"tool": tu["name"], "input_preview": str(tu["input"])[:200]}
                        for tu in mcp_tool_uses
                    ],
                })
                yield debug_collector.to_sse_dict(evt)

            # Signal that tool calls are in flight so the client can show progress.
            yield self._THINKING

            results_by_id: dict[str, str] = {}

            # Grimdall-blocked calls resolve to their error payload here; the
            # tool_results loop below feeds them back to the model untouched.
            results_by_id.update(grimdall_blocked)

            event_cursor = len(debug_collector._events) if debug_collector else 0
            session_id = getattr(current_session.get(), "session_id", None)
            if specialist_calls:
                spec_t0 = time.monotonic()
                specialist_results = await route_parallel(
                    run_calls,
                    episodic_context=episodic_context,
                    session_id=session_id,
                    debug_collector=debug_collector,
                )
                spec_ms = round((time.monotonic() - spec_t0) * 1000)
                for tu, result in zip(
                    run_tool_uses, specialist_results, strict=True
                ):
                    results_by_id[tu["id"]] = result
                # Over-budget specialist calls get an explicit skip result
                # rather than being silently fanned out.
                results_by_id.update(skipped_results)
                if skipped_results:
                    logger.info(
                        "specialist fan-out capped: requested=%d cap=%d skipped=%d",
                        len(specialist_calls),
                        fanout_cap,
                        len(skipped_results),
                    )
                specialists_consulted.extend(c["specialist"] for c in run_calls)
                if consulted_out is not None:
                    consulted_out.extend(c["specialist"] for c in run_calls)
                if specialist_outputs_out is not None:
                    for call, result in zip(
                        run_calls, specialist_results, strict=True
                    ):
                        # Last-write-wins if the same specialist is consulted
                        # in multiple iterations — committee only needs a
                        # representative excerpt per domain.
                        specialist_outputs_out[call["specialist"]] = result
                for call, spec_result in zip(
                    run_calls, specialist_results, strict=True
                ):
                    audit_log(
                        "specialist_consult",
                        f"Consulted {call['specialist']}: {str(call['query'])[:160]}",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor=call["specialist"],
                        details={
                            "iteration": iteration,
                            "duration_ms": spec_ms,
                            "context_preview": str(call.get("context", ""))[:200],
                        },
                        full={
                            "query": call["query"],
                            "context": call.get("context", ""),
                            "response": spec_result,
                            "active_prompt_blocks": _system_block_names(system_blocks),
                        },
                    )

            if skill_tool_uses:
                for tu in skill_tool_uses:
                    logger.info("→ skill:%s  input=%s", tu["name"], _trunc(tu["input"]))
                skill_results = await asyncio.gather(
                    *(_ALL_SKILL_HANDLERS[tu["name"]](tu["input"]) for tu in skill_tool_uses)
                )
                for tu, result in zip(skill_tool_uses, skill_results, strict=True):
                    logger.info("← skill:%s  result=%s", tu["name"], _trunc(result))
                    results_by_id[tu["id"]] = result
                    # Inline action chip for side-effecting tools. None
                    # when the tool is read-only (search_skills, load_skill,
                    # list_people, ask_about_person, lookup_person) or
                    # when the handler reported an error.
                    chip = summarize_action(
                        tool_name=tu["name"],
                        tool_input=tu["input"],
                        tool_result=result,
                        iteration=iteration,
                    )
                    if chip is not None:
                        yield chip
                    # Form proposals reach the Ask OE panel as a dedicated
                    # SSE event (not an action chip — nothing was mutated).
                    # Only deliver what the handler accepted; a shape error
                    # already went back to the model as the tool_result.
                    if tu["name"] == PROPOSE_FORM_VALUES:
                        try:
                            handler_ok = "error" not in json.loads(result)
                        except (json.JSONDecodeError, TypeError):
                            # The handler always returns JSON today; if a
                            # future change breaks that, drop the event
                            # rather than killing the whole SSE stream.
                            logger.warning(
                                "propose_form_values returned non-JSON; "
                                "suppressing form_patch event"
                            )
                            handler_ok = False
                        if handler_ok:
                            yield build_form_patch_event(tu["input"], iteration)
                    audit_log(
                        "tool_invocation",
                        f"skill:{tu['name']} input={audit_tool_input(tu['name'], tu['input'])}",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor="executive",
                        details={
                            "tool": tu["name"],
                            "kind": "skill",
                            "iteration": iteration,
                            "result_preview": audit_tool_result(tu["name"], result),
                        },
                        full={
                            "input": audit_tool_input_full(tu["name"], tu["input"]),
                            "result": audit_tool_result_full(tu["name"], result),
                            "active_prompt_blocks": _system_block_names(system_blocks),
                        },
                    )

            if mcp_tool_uses and self._mcp_gateway is not None:
                _mcp_dispatch = {
                    "search_tools": self._mcp_gateway.search_tools,
                    "call_tool": self._mcp_gateway.call_tool,
                    "load_mcp_server": self._mcp_gateway.load_mcp_server,
                }
                for tu in mcp_tool_uses:
                    if tu["name"] == "call_tool":
                        logger.info(
                            "→ %s  args=%s",
                            tu["input"].get("name", "call_tool"),
                            _trunc(tu["input"].get("arguments", "")),
                        )
                    else:
                        logger.info("→ %s  input=%s", tu["name"], _trunc(tu["input"]))
                mcp_results = await asyncio.gather(
                    *(_mcp_dispatch[tu["name"]](tu["input"]) for tu in mcp_tool_uses)
                )
                for tu, result in zip(mcp_tool_uses, mcp_results, strict=True):
                    tool_label = tu["input"].get("name", tu["name"]) if tu["name"] == "call_tool" else tu["name"]
                    logger.info("← %s  result=%s", tool_label, _trunc(result))
                    results_by_id[tu["id"]] = result
                    # MCP chip emission. search_tools is read-only (gets
                    # filtered out by summarize_action's allowlist);
                    # call_tool and load_mcp_server both surface a chip.
                    chip = summarize_action(
                        tool_name=tu["name"],
                        tool_input=tu["input"],
                        tool_result=result,
                        iteration=iteration,
                    )
                    if chip is not None:
                        yield chip
                    audit_log(
                        "tool_invocation",
                        f"mcp:{tool_label} input={audit_tool_input(tool_label, tu['input'])}",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor="executive",
                        details={
                            "tool": tool_label,
                            "kind": "mcp",
                            "iteration": iteration,
                            "result_preview": audit_tool_result(tool_label, result),
                        },
                        full={
                            "input": audit_tool_input_full(tool_label, tu["input"]),
                            "result": audit_tool_result_full(tool_label, result),
                            "active_prompt_blocks": _system_block_names(system_blocks),
                        },
                    )

            if debug_collector:
                for evt in debug_collector._events[event_cursor:]:
                    yield debug_collector.to_sse_dict(evt)

            current_messages.append({"role": "assistant", "content": response_content})
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": results_by_id.get(
                        tu["id"], f"Unknown tool: {tu['name']}"
                    ),
                }
                for tu in tool_uses
            ]
            current_messages.append({"role": "user", "content": tool_results})

        logger.warning("max_iterations=%d reached — returning partial result", max_iterations)
        yield last_full_text or "I was unable to complete the analysis. Please try again."

    async def chat(
        self,
        user_message: str,
        session: Session,
        retrieved_context: str = "",
        episodic_context: str = "",
        max_iterations: int = 15,
        committee_review: bool = False,
        debug_collector: DebugCollector | None = None,
        attachment_blocks: list[dict[str, Any]] | None = None,
        person_id: int | None = None,
        co_present_person_ids: list[int] | None = None,
        peer_memory_reasoning_level: HonchoReasoningLevel | None = None,
        peer_memory_context: str | None = None,
        briefing_context: str = "",
    ) -> str:
        """Non-streaming chat — collects and returns the full response.

        When ``committee_review=True`` the call routes through
        ``stream_chat_with_committee`` so the answer is the revised
        response, not the raw draft. Used by non-HTTP callers (e.g.
        the email poller) that want committee output without holding
        an SSE stream open.

        ``attachment_blocks`` is a list of Anthropic content blocks (image
        type) assembled by the caller from inbound file attachments.  Text
        document content is expected to be prepended to ``user_message``
        directly by the integration layer.

        ``person_id`` / ``co_present_person_ids`` / ``peer_memory_reasoning_level``
        thread through to the Honcho memory layer — see ``stream_chat``
        for details. ``peer_memory_reasoning_level=None`` (default)
        keeps each underlying entry point's own default (``"low"`` for
        the streaming path, ``"medium"`` for the committee path).
        """
        # Build the kwargs dict so we can conditionally include the
        # reasoning_level only when caller specified one — otherwise
        # the inner method's own default ("low" or "medium") wins.
        common_kwargs: dict[str, Any] = {
            "user_message": user_message,
            "session": session,
            "retrieved_context": retrieved_context,
            "episodic_context": episodic_context,
            "debug_collector": debug_collector,
            "max_iterations": max_iterations,
            "attachment_blocks": attachment_blocks,
            "person_id": person_id,
            "co_present_person_ids": co_present_person_ids,
            "briefing_context": briefing_context,
        }
        if peer_memory_reasoning_level is not None:
            common_kwargs["peer_memory_reasoning_level"] = peer_memory_reasoning_level
        if peer_memory_context is not None:
            common_kwargs["peer_memory_context"] = peer_memory_context
        stream = (
            self.stream_chat_with_committee(**common_kwargs)
            if committee_review
            else self.stream_chat(**common_kwargs)
        )
        result = ""
        async for chunk in stream:
            if isinstance(chunk, str) and chunk != self._THINKING:
                result += chunk
        return result
