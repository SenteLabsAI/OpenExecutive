"""Chat tool that drives the executive_research workflow.

The Executive calls ``run_executive_research`` when the principal asks
"what should we be looking into?" or "what's worth knowing right now?".
The tool runs the workflow — which both produces findings AND routes
them (DMs, alerts, watchlist additions, etc. land as side-effects of
the synthesis step) — and returns a structured summary of what was
researched + what was routed. The Executive then describes the run to
the principal in chat.

Mirrors the JSON-in / JSON-out pattern of the other orchestrator tools.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.audit import log_event as audit_log
from openexecutive.workflows.gate import ensure_workflow_event

logger = logging.getLogger(__name__)


RUN_EXECUTIVE_RESEARCH_TOOL: dict[str, Any] = {
    "name": "run_executive_research",
    "description": (
        "Run the 7-specialist research council. Each specialist (CFO / "
        "CSO / CMO / COO / CHRO / CPO / GC) researches within their "
        "domain using web_search, emits structured findings, and the "
        "Executive synthesis step routes each finding via the right "
        "outbound tool: DM a Person, message a department channel, "
        "create a briefing alert, add to the external watchlist for "
        "ongoing monitoring, schedule a follow-up, or suggest a deeper "
        "workflow. Side-effects fire during the run; this tool returns "
        "a summary of what was researched and what was routed. Use "
        "when the principal asks for proactive research, or whenever "
        "the company state has shifted materially."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": (
                    "Optional one-line note added to the research "
                    "context — useful for narrowing the run ('focus on "
                    "Series-B competitor signals'). Leave empty for a "
                    "general research pass."
                ),
            },
        },
    },
}


async def handle_run_executive_research(
    tool_input: dict[str, Any],
) -> str:
    note = str(tool_input.get("note", "")).strip()

    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.workflows import WORKFLOW_REGISTRY
    from openexecutive.workflows.persistence import (
        complete_run,
        create_run,
        fail_run,
    )

    workflow = WORKFLOW_REGISTRY["executive_research"]
    input_cls = workflow.input_model()
    try:
        wf_inputs = input_cls(note=note)
    except Exception as exc:
        return _err(f"bad arguments: {exc}")

    run_id = str(uuid.uuid4())
    try:
        create_run(
            run_id,
            "executive_research",
            f"{workflow.title} (chat-tool fire)",
            wf_inputs.model_dump(),
        )
    except Exception:
        logger.exception("run_executive_research: create_run failed")

    artifact = ""
    result_data: dict[str, Any] = {}
    last_error = ""
    store = ChromaDBStore(persist_directory=get_settings().vector_store_path)
    try:
        async for event in workflow.run(inputs=wf_inputs, store=store):
            event = ensure_workflow_event(event, site='research_tools.run_executive_research')
            if event.type == "result" and event.data:
                result_data = event.data
            elif event.type == "artifact" and event.content:
                artifact = event.content
            elif event.type == "error" and event.message:
                last_error = event.message
    except Exception as exc:
        logger.exception("run_executive_research: workflow.run crashed")
        last_error = str(exc)[:200]

    if last_error:
        import contextlib
        with contextlib.suppress(Exception):
            fail_run(run_id, last_error)
        _audit(
            False,
            f"run_executive_research FAILED — {last_error}",
            {"error": last_error},
        )
        return _err(f"workflow error: {last_error}")

    import contextlib
    with contextlib.suppress(Exception):
        complete_run(run_id, artifact or "(no artifact)")

    findings = result_data.get("findings") or []
    tool_calls = result_data.get("tool_calls") or []
    narrative = result_data.get("narrative") or ""

    _audit(
        True,
        (
            f"run_executive_research: {len(findings)} finding(s), "
            f"{len(tool_calls)} tool call(s)"
        ),
        {
            "findings": len(findings),
            "tool_calls": len(tool_calls),
            "run_id": run_id,
        },
    )

    # Return ONLY counts + the narrative the synthesis emitted —
    # NOT the per-finding details or the tool_calls log. The
    # synthesis step already fired all routing tools; echoing the
    # full payloads back to the calling Executive turn invites a
    # re-fire (the model sees a tool name + a result preview that
    # looks like an executable instruction and may run it again).
    # Mirrors `executive_reflection`'s behaviour, which never
    # surfaces its own tool inputs to the next chat turn.
    tool_summary = [
        {"tool": t.get("tool", ""), "ok": bool(t.get("ok"))}
        for t in tool_calls
    ]
    return json.dumps({
        "ok": True,
        "run_id": run_id,
        "findings_count": len(findings),
        "tool_calls_count": len(tool_calls),
        "ok_tool_calls": sum(1 for t in tool_calls if t.get("ok")),
        "tool_summary": tool_summary,
        "narrative": narrative,
        "presentation_hint": (
            "Tell the principal what you researched and what was "
            "routed. The synthesis step already DM'd, alerted, and "
            "added to the watchlist where appropriate — do NOT call "
            "those tools again. Lead with the headline numbers "
            "(findings count, actions taken), then summarise the "
            "narrative; do not re-enumerate the findings."
        ),
    })


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _err(msg: str) -> str:
    _audit(False, f"run_executive_research: {msg}", {"error": msg[:300]})
    return json.dumps({"error": msg})


def _audit(ok: bool, summary: str, details: dict[str, Any]) -> None:
    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": "run_executive_research", "ok": ok, **details},
    )


RESEARCH_TOOLS: list[dict[str, Any]] = [RUN_EXECUTIVE_RESEARCH_TOOL]

RESEARCH_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "run_executive_research": handle_run_executive_research,
}


__all__ = [
    "RESEARCH_TOOLS",
    "RESEARCH_TOOL_HANDLERS",
    "RUN_EXECUTIVE_RESEARCH_TOOL",
    "handle_run_executive_research",
]
