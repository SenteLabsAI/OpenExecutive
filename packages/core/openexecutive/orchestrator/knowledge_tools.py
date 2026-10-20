"""``search_knowledge``: the knowledge base as a tool, for unattended passes.

A chat turn looks up the knowledge base before the Executive answers, and
each specialist it consults looks up its own domain. The morning reflection
(and Take the lead, which acts from it) has neither: it reads the org's
state, not the question someone asked. This tool lets it look up what the
company's documents say about a customer, contract, price or policy before
it acts on or flags one.

It runs the same ``knowledge.retriever.retrieve`` as chat, so the review gate
and the sources (uploads, Drive, OneDrive, Confluence, Notion) are the same.
The pass runs with no viewer (``artifact_records.current_viewer`` is nobody
outside a turn), so no one's private documents come back. Read-only: Take the
lead never gates it. The schema is static.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# What the pass reads back: enough for a few matching passages.
RESULT_CHARS = 4000
_MAX_QUERY_CHARS = 300

SEARCH_KNOWLEDGE_TOOL: dict[str, Any] = {
    "name": "search_knowledge",
    "description": (
        "Search the company's knowledge base (uploaded documents and synced "
        "Drive, OneDrive, Confluence and Notion pages) and return the passages "
        "that match. Use it before acting on or flagging something that turns "
        "on what the company has written down: a customer or supplier's terms, "
        "a price, a policy, a plan or a past decision. Pass a short query, "
        "e.g. 'Westline fleet discount'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up, in a few words.",
            },
        },
        "required": ["query"],
    },
}

KNOWLEDGE_TOOLS: list[dict[str, Any]] = [SEARCH_KNOWLEDGE_TOOL]


async def handle_search_knowledge(tool_input: dict[str, Any]) -> str:
    query = str((tool_input or {}).get("query") or "").strip()[:_MAX_QUERY_CHARS]
    if not query:
        return json.dumps({"error": "Pass a query: what to look up, in a few words."})
    from openexecutive.knowledge.retriever import retrieve

    try:
        found = await asyncio.to_thread(retrieve, query=query)
    except Exception:
        logger.exception("search_knowledge: retrieval failed")
        return json.dumps({"error": "The knowledge base couldn't be searched just now."})
    if not found.strip():
        return "Nothing in the knowledge base matches that."
    return found[:RESULT_CHARS]


KNOWLEDGE_TOOL_HANDLERS: dict[str, Any] = {
    "search_knowledge": handle_search_knowledge,
}
