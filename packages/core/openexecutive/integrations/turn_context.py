"""Shared warm-store RAG + episodic fetch for chat adapters.

Slack already gathers these on a warm ``get_store()``; Discord, Telegram,
Google Chat and email used to call ``retrieve()`` / ``format_for_prompt()``
synchronously (and sometimes without a session id). One helper keeps them
aligned with the web/Slack path.
"""

from __future__ import annotations

import asyncio


async def fetch_turn_context(
    query: str,
    *,
    session_id: str = "",
) -> tuple[str, str]:
    """Return ``(retrieved_context, episodic_context)`` off the event loop.

    Uses the lifespan-warmed Chroma store when the MCP server has one;
    otherwise ``retrieve`` builds its own (same fallback as ``/chat``).
    """
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.mcp_server.server import get_store
    from openexecutive.memory.episodic import format_for_prompt

    retrieved, episodic = await asyncio.gather(
        asyncio.to_thread(retrieve, query=query, store=get_store()),
        asyncio.to_thread(format_for_prompt, session_id=session_id),
    )
    return retrieved, episodic
