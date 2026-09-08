"""Agentic research turn — a bounded search → scrape_url → emit tool-use
loop so a specialist READS its sources before emitting findings.

The single-shot path gives a specialist only web_search snippets, so it
infers a claim and bolts on a URL it never read (the verify pass measured
this: nearly every cited source failed to back its claim). Here the
specialist instead loops: web_search to find candidates, ``scrape_url`` to
read the full article, then ``emit_research_findings`` grounded in what it
actually read.

Block handling mirrors the Executive's main loop
(``orchestrator/executive.py``): server web_search blocks
(``server_tool_use`` / ``web_search_tool_result``) are resolved inline by
Anthropic (or, under OpenRouter, are the ``web`` plugin and never appear as
blocks) — we echo them forward verbatim and run no handler. Only the
client ``scrape_url`` tool is executed by us, and every client ``tool_use``
gets a matching ``tool_result`` in the next user turn.
"""
from __future__ import annotations

import logging
from typing import Any

from openexecutive.agents.base import BaseAgent
from openexecutive.config import get_settings
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.research.prompts import research_addendum_for
from openexecutive.monitoring.research.scrape_tool import (
    SCRAPE_URL_TOOL,
    SCRAPE_URL_TOOL_NAME,
    handle_scrape_url,
)
from openexecutive.monitoring.research.tools import EMIT_RESEARCH_FINDINGS_TOOL
from openexecutive.orchestrator.web_search_tool import build_web_search_tool
from openexecutive.providers import get_provider, model_supports_deep_reasoning
from openexecutive.providers.translator import reasoning_replay_block

logger = logging.getLogger(__name__)

_EMIT_TOOL_NAME = "emit_research_findings"
# A scrape can push a turn past the chat-time default; match the single-shot
# research ceiling.
_RESEARCH_TIMEOUT_SECONDS = 300.0
_MAX_TOKENS = 4096


async def run_specialist_agentic(
    specialist_slug: str,
    agent: BaseAgent,
    research_context: str,
) -> list[ResearchFinding]:
    """Run one specialist as a tool-use loop. Returns the parsed findings
    (possibly empty). Best-effort: any provider/scrape failure ends the loop
    with whatever was emitted so far (else empty) — never raises into the
    workflow's ``asyncio.gather``."""
    from openexecutive.agents.research_council import (
        get_research_model,
        get_research_use_deep_reasoning,
    )
    from openexecutive.monitoring.research.specialist_research import (
        _extract_findings,
    )

    settings = get_settings()
    model = get_research_model()
    use_deep = get_research_use_deep_reasoning()
    system_prompt = agent.effective_system_prompt() + research_addendum_for(
        specialist_slug
    )

    tools: list[dict[str, Any]] = [EMIT_RESEARCH_FINDINGS_TOOL, SCRAPE_URL_TOOL]
    web_search = build_web_search_tool()
    if web_search is not None:
        tools.append(web_search)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": research_context},
    ]
    scrapes_used = 0
    max_scrapes = max(0, settings.research_scrape_max_per_specialist)
    max_iters = max(1, settings.research_loop_max_iterations)

    for iteration in range(1, max_iters + 1):
        create_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "timeout": _RESEARCH_TIMEOUT_SECONDS,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": tools,
            "messages": messages,
        }
        if use_deep and model_supports_deep_reasoning(model):
            create_kwargs["thinking"] = {"type": "adaptive"}
            create_kwargs["output_config"] = {
                "effort": settings.specialist_effort
            }
            create_kwargs["max_tokens"] = max(_MAX_TOKENS, 16000)

        try:
            message = await get_provider(model).messages_create(**create_kwargs)
        except Exception:
            logger.exception(
                "research agentic: specialist=%s provider call failed (iter=%d)",
                specialist_slug, iteration,
            )
            return []

        content = getattr(message, "content", None) or []

        emitted = _has_emit(content)
        # Mandatory read-before-cite: a specialist that emits findings without
        # having scraped a single source is exactly the failure the loop
        # exists to fix (it cites web_search snippets it never read). Reject
        # that emit and force another turn — but only when scraping is
        # actually possible (budget left) and we still have iterations to
        # spare, so we never trap the model in an unsatisfiable loop.
        force_read = (
            emitted
            and scrapes_used == 0
            and max_scrapes > 0
            and iteration < max_iters
        )

        # Terminal — accept the emit unless we're forcing a read first.
        if emitted and not force_read:
            return _extract_findings(message, specialist_slug)

        assistant_content, scrape_calls = _assistant_turn(content)

        if not emitted and not scrape_calls:
            # No scrape and no emit → the model is done (text-only, or it
            # only ran web_search inline). There is no client tool_result to
            # send, so the conversation can't continue — stop here.
            logger.debug(
                "research agentic: specialist=%s neither scraped nor emitted "
                "at iter=%d — stopping", specialist_slug, iteration,
            )
            return []

        # Every CLIENT tool_use in the assistant turn needs a matching
        # tool_result in the next user turn or the API 400s. Execute scrapes,
        # and when forcing a read, also answer the rejected emit tool_use.
        tool_results: list[dict[str, Any]] = []
        for call in scrape_calls:
            budget_remaining = max_scrapes - scrapes_used
            try:
                result = await handle_scrape_url(
                    call["input"], budget_remaining=budget_remaining,
                )
            except Exception as exc:
                logger.warning(
                    "research agentic: scrape_url crashed for %s: %s",
                    specialist_slug, exc,
                )
                result = "error: scrape failed; emit without this source."
            if budget_remaining > 0:
                scrapes_used += 1
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": result,
            })

        if force_read:
            for emit_id in _emit_ids(content):
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": emit_id,
                    "content": (
                        "You must call scrape_url and READ at least one source "
                        "to ground your claims before emitting. web_search "
                        "snippets are not enough. Call scrape_url now, then "
                        "emit findings grounded in what you read."
                    ),
                })
            logger.info(
                "research agentic: specialist=%s emitted with 0 scrapes at "
                "iter=%d — rejecting, forcing a read", specialist_slug,
                iteration,
            )

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})

    logger.info(
        "research agentic: specialist=%s hit iteration cap (%d) without "
        "emitting", specialist_slug, max_iters,
    )
    return []


def _has_emit(content: list[Any]) -> bool:
    return any(
        getattr(b, "type", "") == "tool_use"
        and getattr(b, "name", "") == _EMIT_TOOL_NAME
        for b in content
    )


def _emit_ids(content: list[Any]) -> list[str]:
    """tool_use ids of every emit_research_findings block in the turn — each
    needs a tool_result when we reject the emit to force a read first."""
    return [
        getattr(b, "id", "")
        for b in content
        if getattr(b, "type", "") == "tool_use"
        and getattr(b, "name", "") == _EMIT_TOOL_NAME
    ]


def _assistant_turn(
    content: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild the assistant message from response blocks, and collect the
    scrape_url client tool_uses to execute.

    Server web_search blocks are echoed verbatim (Anthropic already resolved
    them); only scrape_url is executed by us.
    """
    assistant_content: list[dict[str, Any]] = []
    scrape_calls: list[dict[str, Any]] = []
    for block in content:
        btype = getattr(block, "type", "")
        if btype == "text":
            assistant_content.append(
                {"type": "text", "text": getattr(block, "text", "")}
            )
        elif btype == "tool_use":
            tu = {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", None) or {},
            }
            assistant_content.append(tu)
            if tu["name"] == SCRAPE_URL_TOOL_NAME:
                scrape_calls.append({"id": tu["id"], "input": tu["input"]})
        elif (replay := reasoning_replay_block(block)) is not None:
            # OpenRouter-path equivalent of a thinking block: replay its
            # reasoning_details verbatim or the next tool-loop turn loses
            # (Anthropic: rejects) the chain of thought.
            assistant_content.append(replay)
        elif btype in (
            "thinking",
            "redacted_thinking",
            "server_tool_use",
            "web_search_tool_result",
        ):
            # Echo verbatim. thinking / redacted_thinking MUST be preserved in
            # the assistant history when extended thinking is on (deep
            # reasoning) or Anthropic 400s the next turn; server web_search
            # blocks are already-resolved and carried for continuity.
            dump = getattr(block, "model_dump", None)
            if callable(dump):
                assistant_content.append(block.model_dump(exclude_none=True))
    return assistant_content, scrape_calls


__all__ = ["run_specialist_agentic"]
