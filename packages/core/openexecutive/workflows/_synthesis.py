"""Shared helpers for workflows that run an Executive tool-use loop.

Both ``executive_reflection`` and ``executive_research`` drive the
Executive through a short tool-use loop with the full outbound toolkit.
The two helpers below are the bits both loops share:

- ``execute_tool_calls`` dispatches any tool_use blocks in a provider
  response against ``_ALL_SKILL_HANDLERS`` and returns one summary per
  call (tool, input_preview, result_preview, ok). Failures are caught +
  summarized so the loop survives a single tool crash.
- ``extract_artifact_from_response`` pulls the concatenated text-block
  content from a provider response — the model's narrative summary
  emitted alongside or after its tool calls.

Promoting these out of ``executive_reflection`` removes the private-
import tripwire ``executive_research`` would otherwise carry — a future
refactor renaming or moving them could silently break the other
workflow.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def execute_tool_calls(
    response: Any,
    handlers: dict[str, Any],
    *,
    budget_remaining: int | None = None,
    free_tools: frozenset[str] | None = None,
    result_chars: int = 160,
    wide_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Invoke any tool_use blocks in the LLM response.

    Returns one summary entry per call:
    ``{tool, input_preview, result_preview, ok}``. Failures are caught
    + summarized so a single tool crash does not abort the workflow's
    iteration loop.

    ``budget_remaining`` (optional) caps how many handlers actually
    run. Once budget hits zero, remaining tool_use blocks are NOT
    invoked — their summary entries are still appended with
    ``result_preview = 'over budget — skipped'`` so the model sees a
    tool_result acknowledgement on the next turn. Default ``None``
    leaves the loop unmetered (executive_reflection's behaviour).

    ``free_tools`` (optional) names tools that are exempt from the
    budget — read-only context lookups (e.g. ``lookup_person``) the
    model must call to PREPARE a routing decision. They always execute
    and never decrement ``budget_remaining``, so the Executive can't
    spend its outbound budget on lookups and then have no budget left
    to actually route (the "looks up people but never DMs" failure).

    ``result_chars`` caps each result the model sees back: short for a
    pass that only routes, longer for one that must read what a tool found
    (Take the lead's search_tools results, a created file's id).
    ``wide_results`` raises that cap for the named tools whose result is
    the point of calling them (``search_knowledge``'s passages).
    """
    free_tools = free_tools or frozenset()
    summaries: list[dict[str, Any]] = []
    if not getattr(response, "content", None):
        return summaries
    for block in response.content:
        if getattr(block, "type", "") != "tool_use":
            continue
        name = getattr(block, "name", "")
        tool_input = getattr(block, "input", {}) or {}
        is_free = name in free_tools
        if budget_remaining is not None and budget_remaining <= 0 and not is_free:
            # Refuse without executing. Surfaces in the next user-turn
            # tool_result so the model knows further calls are blocked.
            # Free (read-only) tools are never refused on budget grounds.
            summaries.append({
                "tool": name,
                "input_preview": str(tool_input)[:120],
                "result_preview": "over budget — skipped",
                "ok": False,
            })
            continue
        handler = handlers.get(name)
        if handler is None:
            summaries.append({
                "tool": name,
                "input_preview": str(tool_input)[:120],
                "result_preview": "unknown tool — skipped",
                "ok": False,
            })
            continue
        try:
            result = await handler(tool_input)
        except Exception as exc:
            logger.exception("synthesis: tool %s raised", name)
            summaries.append({
                "tool": name,
                "input_preview": str(tool_input)[:120],
                "result_preview": f"raised: {exc}"[:160],
                "ok": False,
            })
            if budget_remaining is not None and not is_free:
                budget_remaining -= 1
            continue
        ok = True
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
            if isinstance(parsed, dict) and "error" in parsed:
                ok = False
        except (ValueError, TypeError):
            # Non-JSON result — treat as success since we have no
            # signal otherwise (same as action_chips).
            pass
        summaries.append({
            "tool": name,
            "input_preview": str(tool_input)[:120],
            "result_preview": str(result)[: max(result_chars, (wide_results or {}).get(name, 0))],
            "ok": ok,
        })
        if budget_remaining is not None and not is_free:
            budget_remaining -= 1
    return summaries


def extract_artifact_from_response(response: Any) -> str:
    """Pull the final text summary from an Anthropic response.

    Returns the concatenated text of all text blocks. Empty string when
    the model only emitted tool_use blocks — callers fall back to a
    synthesized "no summary" artifact.
    """
    if not getattr(response, "content", None):
        return ""
    chunks: list[str] = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text = getattr(block, "text", "")
            if text:
                chunks.append(text)
    return "\n\n".join(chunks).strip()


__all__ = ["execute_tool_calls", "extract_artifact_from_response"]
