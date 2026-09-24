"""Read API for the audit log.

Filter by event type, session, actor, date range, plus a LIKE search on the
human-readable `summary` column. Pagination is offset-based.

Also exposes a session-scoped read at GET /audit/sessions/{session_id} that
returns the full ordered timeline for one inbound request plus a derived
graph (nodes + edges) so the UI can render a flow-chart view without
re-deriving causality from event types client-side.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from openexecutive.audit import AuditLogger, get_audit_logger
from openexecutive.audit.logger import EVENT_TYPES

# Session IDs are integration-derived (e.g. "discord:dm:42", "slack:dm:U123",
# "slack:thread:C1:1700000000.001", "telegram:5556677", "email:thread@host"). They
# come from third-party webhooks, so reflecting an unvalidated value into the
# response body would let an attacker shape stored XSS via a malformed inbound.
# 256 chars is generous (longest observed in practice is ~80).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_:@\-\.\+/=]{1,256}$")

router = APIRouter()


class AuditEventOut(BaseModel):
    id: int
    ts: str
    event_type: str
    session_id: str | None
    turn_id: str | None
    actor: str | None
    summary: str
    details: dict[str, Any]


class AuditEventDetailOut(AuditEventOut):
    # Un-truncated drill-down payload (full message bodies, tool inputs/results,
    # specialist queries). Null when nothing extra was captured for this row,
    # or when the row predates the column.
    full: dict[str, Any] | None = None


class AuditListResponse(BaseModel):
    items: list[AuditEventOut]
    total: int
    limit: int
    offset: int
    event_types: list[str]


class AuditGraphNode(BaseModel):
    """One node in the session flow-chart.

    `kind` is the abstract role on the timeline (inbound / executive_turn /
    specialist / tool / knowledge / cache / memory / response / committee)
    derived from `event_type` + context, not a 1:1 mapping. Frontend renders
    icon + color from `kind` first, falls back to `event_type`.
    """
    id: str
    event_id: int
    event_type: str
    kind: str
    label: str
    actor: str | None
    turn_id: str | None
    ts: str


class AuditGraphEdge(BaseModel):
    """Directed edge from one timeline node to another."""
    source: str
    target: str
    # "order" = pure temporal sequencing inside one turn
    # "cause" = causal relation (e.g. executive_turn → specialist via tool_use)
    relation: str = "order"


class AuditGraph(BaseModel):
    nodes: list[AuditGraphNode]
    edges: list[AuditGraphEdge]


class TokenCounts(BaseModel):
    """The token counters every cost view shares: model-call count plus the
    four Anthropic token classes.

    `input_tokens` is fresh (non-cached) prompt input; `cache_read_input_tokens`
    is prompt input served from the cache (~10x cheaper) — together they make
    the cache-hit ratio legible. `calls` is the number of model invocations
    (Executive iterations + specialist consults + committee passes)."""
    calls: int
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    # Server-side web searches the calls made (0 for rows that predate capture).
    web_search_requests: int = 0


class TurnCost(TokenCounts):
    """Token usage for one user-assistant turn, summed across its LLM calls."""
    turn_id: str | None


class CostSummary(TokenCounts):
    """Session-level token totals plus a per-turn breakdown, derived from the
    session's `cache_event` rows. Null when no cache_event was logged."""
    per_turn: list[TurnCost]


class UsageTotals(TokenCounts):
    """Token + cost totals over a window, summed across all `cache_event` rows.

    `cost_usd` is the actual OpenRouter-charged amount captured per call; rows
    that predate cost capture (or non-OpenRouter calls) contribute 0, so the
    figure accrues from go-live rather than being a back-estimated guess."""
    cost_usd: float


class UsageByDay(UsageTotals):
    day: str  # YYYY-MM-DD (UTC, from the `ts` prefix)


class UsageByModel(UsageTotals):
    model: str


class UsageBySource(UsageTotals):
    """One row per call source (the audit `actor`): `executive` chat turns,
    `specialist` chat-turn consults, `specialist_workflow` steps,
    `specialist_research`, `research_synthesis`, `research_watchlist`,
    `triage`, `memory_extractor`, `agent_test`, … — so a heavy day can be
    traced to the part of the system that produced it."""
    source: str


class UsageSummary(BaseModel):
    """Aggregate token usage + cost across ALL sessions over an optional time
    window, with by-day, by-model and by-source breakdowns. Derived from
    `cache_event` rows — the same source as the per-session `CostSummary`,
    but unscoped."""
    since: str | None = None
    until: str | None = None
    totals: UsageTotals
    by_day: list[UsageByDay]
    by_model: list[UsageByModel]
    by_source: list[UsageBySource] = []


class Degradation(BaseModel):
    """A dependency failure during the session, surfaced from audit rows so a
    silently-degraded turn is visible to the operator. Today this covers memory
    (Honcho) timeouts/errors — the failure reliably logged with a distinct
    `outcome`. Specialist / RAG failures currently crash or swallow to empty
    without a distinct audit outcome; surfacing those needs their failure paths
    to log one first (follow-up)."""
    kind: str
    reason: str
    count: int
    turn_ids: list[str]
    detail: str | None = None


class AuditSessionResponse(BaseModel):
    session_id: str
    events: list[AuditEventOut]
    graph: AuditGraph
    # Channel inferred from the first integration_inbound event in this
    # session, or null if no inbound was logged (e.g. web chat session).
    channel: str | None = None
    # Token usage aggregated from this session's `cache_event` rows; null when
    # none were logged. Makes per-turn fan-out cost legible in the flow chart.
    cost_summary: CostSummary | None = None
    # Dependency failures detected from this session's audit rows (e.g. memory
    # timeouts/errors) so silently-degraded turns are visible. Empty when clean.
    degradations: list[Degradation] = []


def _resolve_logger(request: Request) -> AuditLogger:
    audit = getattr(request.app.state, "audit", None)
    if isinstance(audit, AuditLogger):
        return audit
    return get_audit_logger()


class AuditLogRequest(BaseModel):
    event_type: str
    summary: str
    session_id: str | None = None
    turn_id: str | None = None
    actor: str | None = None
    details: dict[str, Any] | None = None


@router.post("/audit/log", status_code=201)
def create_audit_log(body: AuditLogRequest, request: Request) -> dict[str, int | None]:
    if body.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown event_type: {body.event_type!r}")
    audit = _resolve_logger(request)
    row_id = audit.log(
        body.event_type,
        body.summary,
        session_id=body.session_id,
        turn_id=body.turn_id,
        actor=body.actor,
        details=body.details,
    )
    return {"id": row_id}


@router.get("/audit/logs/{event_id}", response_model=AuditEventDetailOut)
def get_audit_log(event_id: int, request: Request) -> AuditEventDetailOut:
    audit = _resolve_logger(request)
    event = audit.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="audit event not found")
    return AuditEventDetailOut(
        id=event.id,
        ts=event.ts,
        event_type=event.event_type,
        session_id=event.session_id,
        turn_id=event.turn_id,
        actor=event.actor,
        summary=event.summary,
        details=event.details,
        full=event.full,
    )


_KIND_BY_EVENT_TYPE: dict[str, str] = {
    "integration_inbound": "inbound",
    "chat_turn": "response",
    "specialist_consult": "specialist",
    "tool_invocation": "tool",
    "knowledge_retrieval": "knowledge",
    "cache_event": "cache",
    "memory_snapshot": "memory",
    "committee_review": "committee",
    "scheduled_action": "scheduled",
    "alert": "alert",
}


def _node_label(event_type: str, summary: str, details: dict[str, Any]) -> str:
    """Short, glanceable label for a flow-chart node.

    Pulls the most informative field per event_type — full payload is in the
    drill-down drawer, so the label is for orientation only (≤60 chars).
    """
    if event_type == "specialist_consult":
        actor = details.get("specialist") or ""
        return f"{actor}".strip(": ")[:60] or summary[:60]
    if event_type == "tool_invocation":
        tool = details.get("tool")
        if tool:
            return f"{tool}"[:60]
        return summary[:60]
    if event_type == "knowledge_retrieval":
        n = (details.get("builtin_count", 0) or 0) + (details.get("company_count", 0) or 0)
        domain = details.get("domain_filter") or "*"
        if isinstance(domain, list):
            domain = ",".join(domain) or "*"
        return f"RAG[{domain}] · {n} chunks"[:60]
    if event_type == "cache_event":
        cr = details.get("cache_read_input_tokens", 0)
        cc = details.get("cache_creation_input_tokens", 0)
        out = details.get("output_tokens", 0)
        return f"cache r={cr} w={cc} out={out}"[:60]
    if event_type == "memory_snapshot":
        ep = details.get("episodic_chars", 0)
        rag = details.get("retrieved_chars", 0)
        return f"memory · ep={ep}c rag={rag}c"[:60]
    if event_type == "integration_inbound":
        return summary.split(": ", 1)[-1][:60] if ": " in summary else summary[:60]
    if event_type == "committee_review":
        return "committee revision"
    return summary[:60]


def _build_session_graph(events: list[Any]) -> tuple[AuditGraph, str | None]:
    """Derive nodes + edges for a single session timeline.

    Events arrive sorted by id DESC (newest first); for graph construction we
    flip to ascending so causality reads left→right. Channel is inferred from
    the integration_inbound row if present.
    """
    # Ascending order: oldest first → causal flow reads top-to-bottom.
    ordered = sorted(events, key=lambda e: e.id)

    nodes: list[AuditGraphNode] = []
    edges: list[AuditGraphEdge] = []
    channel: str | None = None

    # For each turn_id, keep the "current executive_turn anchor" (the
    # memory_snapshot node, or the first chat_turn if memory_snapshot is
    # missing — both nominally represent the same logical turn entry).
    turn_anchor: dict[str, str] = {}
    # For each turn_id, latest specialist node — tools fired by that
    # specialist link back to it (best-effort; we don't have parent linkage
    # on the wire yet, so order-within-turn is the heuristic).
    last_specialist_in_turn: dict[str, str] = {}
    # The inbound node, if any — first chat_turn after it links from inbound.
    inbound_node_id: str | None = None
    inbound_consumed = False

    for evt in ordered:
        kind = _KIND_BY_EVENT_TYPE.get(evt.event_type, evt.event_type)
        node_id = f"n{evt.id}"
        details = evt.details or {}
        label = _node_label(evt.event_type, evt.summary or "", details)
        nodes.append(
            AuditGraphNode(
                id=node_id,
                event_id=evt.id,
                event_type=evt.event_type,
                kind=kind,
                label=label,
                actor=evt.actor,
                turn_id=evt.turn_id,
                ts=evt.ts,
            )
        )

        # Track channel from inbound details.
        if evt.event_type == "integration_inbound":
            if isinstance(details, dict):
                ch = details.get("channel")
                if isinstance(ch, str) and ch:
                    channel = ch
            inbound_node_id = node_id
            # Reset the consumed flag so each new inbound in a long-running
            # session (e.g., a multi-turn Discord thread) links to its own
            # downstream memory_snapshot / chat_turn instead of becoming
            # orphaned in the graph.
            inbound_consumed = False

        # Edge construction.
        tid = evt.turn_id

        if evt.event_type == "memory_snapshot" and tid:
            # Memory snapshot is the canonical "turn started" anchor.
            turn_anchor[tid] = node_id
            if inbound_node_id is not None and not inbound_consumed:
                edges.append(
                    AuditGraphEdge(
                        source=inbound_node_id, target=node_id, relation="cause"
                    )
                )
                inbound_consumed = True

        elif evt.event_type in {
            "knowledge_retrieval",
            "specialist_consult",
            "tool_invocation",
            "cache_event",
            "committee_review",
        } and tid:
            anchor = turn_anchor.get(tid)
            if anchor is not None:
                edges.append(AuditGraphEdge(source=anchor, target=node_id, relation="order"))
            # A tool right after a specialist on the same turn likely fired
            # because of that specialist's recommendation — link it.
            if evt.event_type == "tool_invocation":
                prev_spec = last_specialist_in_turn.get(tid)
                if prev_spec is not None:
                    edges.append(
                        AuditGraphEdge(source=prev_spec, target=node_id, relation="cause")
                    )
            if evt.event_type == "specialist_consult":
                last_specialist_in_turn[tid] = node_id

        elif evt.event_type == "chat_turn":
            # chat_turn is the *response* row. If we have a turn anchor for
            # the same turn_id, link from there; otherwise treat it as the
            # implicit turn anchor (older sessions without memory_snapshot).
            if tid and tid in turn_anchor:
                edges.append(
                    AuditGraphEdge(
                        source=turn_anchor[tid], target=node_id, relation="cause"
                    )
                )
            elif inbound_node_id is not None and not inbound_consumed:
                edges.append(
                    AuditGraphEdge(
                        source=inbound_node_id, target=node_id, relation="cause"
                    )
                )
                inbound_consumed = True
            if tid:
                turn_anchor.setdefault(tid, node_id)

    return AuditGraph(nodes=nodes, edges=edges), channel


def _compute_cost_summary(events: list[Any]) -> CostSummary | None:
    """Aggregate token usage from a session's `cache_event` rows.

    One cache_event is emitted per model response (Executive iteration,
    specialist consult, committee pass), carrying the token breakdown in
    `details`. We sum per turn_id and overall; missing/garbled fields count as
    0 so a single odd row never breaks the summary. Returns None when the
    session has no cache_event rows (nothing to show)."""
    rows = [e for e in events if e.event_type == "cache_event"]
    if not rows:
        return None
    # query() returns id DESC; sort ascending so per_turn reads oldest -> newest
    # (totals are order-independent, but a future per-turn timeline isn't).
    rows = sorted(rows, key=lambda e: e.id)

    token_fields = (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "web_search_requests",
    )

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    per_turn_acc: dict[str | None, dict[str, int]] = {}
    order: list[str | None] = []
    for e in rows:
        if e.turn_id not in per_turn_acc:
            per_turn_acc[e.turn_id] = {key: 0 for key in ("calls", *token_fields)}
            order.append(e.turn_id)
        acc = per_turn_acc[e.turn_id]
        acc["calls"] += 1
        details = e.details or {}
        for field in token_fields:
            acc[field] += _as_int(details.get(field))

    per_turn = [TurnCost(turn_id=tid, **per_turn_acc[tid]) for tid in order]
    totals = {
        key: sum(acc[key] for acc in per_turn_acc.values())
        for key in ("calls", *token_fields)
    }
    return CostSummary(per_turn=per_turn, **totals)


def _compute_degradations(events: list[Any]) -> list[Degradation]:
    """Surface dependency failures from the session's audit rows. Today: memory
    (Honcho) timeouts/errors from `peer_memory` rows — the one failure reliably
    logged with a distinct outcome. Normal outcomes (ok / disabled / no_person /
    empty / ...) are NOT degradations. Returns [] when the session ran clean."""
    degraded_outcomes = {"timeout", "error"}
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for e in events:
        if e.event_type != "peer_memory":
            continue
        details = e.details or {}
        outcome = details.get("outcome")
        if outcome not in degraded_outcomes:
            continue
        key = ("memory", str(outcome))
        if key not in acc:
            acc[key] = {"count": 0, "turn_ids": [], "detail": None}
            order.append(key)
        entry = acc[key]
        entry["count"] += 1
        if e.turn_id and e.turn_id not in entry["turn_ids"]:
            entry["turn_ids"].append(e.turn_id)
        if entry["detail"] is None and details.get("error_type"):
            entry["detail"] = str(details["error_type"])
    return [
        Degradation(
            kind=key[0],
            reason=key[1],
            count=acc[key]["count"],
            turn_ids=acc[key]["turn_ids"],
            detail=acc[key]["detail"],
        )
        for key in order
    ]


@router.get(
    "/audit/sessions/{session_id}",
    response_model=AuditSessionResponse,
)
def get_audit_session(session_id: str, request: Request) -> AuditSessionResponse:
    """Full timeline + derived graph for one session.

    `session_id` is matched exactly against the indexed column. Returns 404
    only when no events match — empty sessions don't exist in this table.
    Capped at 1000 events per session (the upper limit `query()` enforces);
    a runaway agent session would clip the tail rather than OOM the API.
    """
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id format")
    audit = _resolve_logger(request)
    events = audit.query(session_id=session_id, limit=1000)
    if not events:
        raise HTTPException(status_code=404, detail="no events for session_id")
    graph, channel = _build_session_graph(events)
    return AuditSessionResponse(
        session_id=session_id,
        events=[
            AuditEventOut(
                id=e.id,
                ts=e.ts,
                event_type=e.event_type,
                session_id=e.session_id,
                turn_id=e.turn_id,
                actor=e.actor,
                summary=e.summary,
                details=e.details,
            )
            for e in events
        ],
        graph=graph,
        channel=channel,
        cost_summary=_compute_cost_summary(events),
        degradations=_compute_degradations(events),
    )


@router.get("/audit/logs", response_model=AuditListResponse)
def list_audit_logs(
    request: Request,
    event_type: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    q: str | None = Query(default=None, description="LIKE search over summary"),
    since: str | None = Query(default=None, description="ISO8601 inclusive lower bound"),
    until: str | None = Query(default=None, description="ISO8601 inclusive upper bound"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AuditListResponse:
    audit = _resolve_logger(request)
    events = audit.query(
        event_type=event_type,
        session_id=session_id,
        actor=actor,
        q=q,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    total = audit.count(
        event_type=event_type,
        session_id=session_id,
        actor=actor,
        q=q,
        since=since,
        until=until,
    )
    return AuditListResponse(
        items=[
            AuditEventOut(
                id=e.id,
                ts=e.ts,
                event_type=e.event_type,
                session_id=e.session_id,
                turn_id=e.turn_id,
                actor=e.actor,
                summary=e.summary,
                details=e.details,
            )
            for e in events
        ],
        total=total,
        limit=limit,
        offset=offset,
        event_types=list(EVENT_TYPES),
    )


@router.get("/audit/usage", response_model=UsageSummary)
def get_audit_usage(
    request: Request,
    since: str | None = Query(default=None, description="ISO8601 inclusive lower bound"),
    until: str | None = Query(default=None, description="ISO8601 inclusive upper bound"),
) -> UsageSummary:
    """Aggregate token usage + cost across all sessions, optionally time-bounded.

    Totals plus per-day and per-model breakdowns, summed from `cache_event`
    rows. Unlike `/audit/sessions/{id}` (one session), this spans the whole log
    so an operator can see overall token spend and where it goes.
    """
    audit = _resolve_logger(request)
    data = audit.usage_summary(since=since, until=until)
    return UsageSummary(
        since=since,
        until=until,
        totals=UsageTotals(**data["totals"]),
        by_day=[UsageByDay(**d) for d in data["by_day"]],
        by_model=[UsageByModel(**m) for m in data["by_model"]],
        by_source=[UsageBySource(**s) for s in data.get("by_source", [])],
    )


