"""Built-in anonymous Search MCP caller, owned by one chat turn/research run.

The connection owns no conversation state. A chat supplies its opaque session
UUID; standalone research gets a new UUID. Limits belong to a turn, while the
identity survives follow-ups. No environment credentials are forwarded.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from openexecutive.config import get_settings

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "OpenExecutive (+https://github.com/SenteLabsAI/OpenExecutive)"
MCP_TIMEOUT_SECONDS = 30
TOOL_NAMES = frozenset({"web_search", "web_fetch"})

SEARCH_TOOL: dict[str, Any] = {
    "name": "web_search",
    "description": "Search the web for current facts. Cite the returned source URLs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "minLength": 1},
            "search_queries": {
                "type": "array", "items": {"type": "string", "minLength": 1},
                "minItems": 1, "maxItems": 3,
            },
        },
        "required": ["objective", "search_queries"],
        "additionalProperties": False,
    },
}
FETCH_TOOL: dict[str, Any] = {
    "name": "web_fetch",
    "description": "Read excerpts from public web pages when search results need more detail. Cite source URLs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
            "objective": {"type": "string", "maxLength": 200},
        },
        "required": ["urls"],
        "additionalProperties": False,
    },
}


async def _mcp_call(name: str, arguments: dict[str, Any]) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with (
        streamablehttp_client(MCP_URL, headers={"User-Agent": USER_AGENT}) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        if name not in {tool.name for tool in tools.tools}:
            raise ValueError("Search MCP tool unavailable")
        return await session.call_tool(name, arguments)


@dataclass
class SearchRun:
    max_uses: int
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    calls: int = 0
    conversation_id: str | None = None

    @classmethod
    def for_chat(cls, session: Any) -> SearchRun:
        # Only an opaque UUID may leave the runtime. Adapters recreate Session
        # on each message, so non-UUID channel identities need persisted state.
        try:
            identity = str(uuid.UUID(session.session_id))
        except (ValueError, TypeError, AttributeError):
            key = getattr(session, "session_id", None)
            return cls(
                get_settings().web_search_max_uses,
                conversation_id=key if isinstance(key, str) and key else None,
            )
        return cls(get_settings().web_search_max_uses, identity)

    async def search(self, arguments: dict[str, Any]) -> str:
        return await self.call("web_search", arguments)

    async def fetch(self, arguments: dict[str, Any]) -> str:
        return await self.call("web_fetch", arguments)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        from openexecutive.delegation.lockdown import outside_reach_refusal
        from openexecutive.orchestrator.web_search_tool import search_provider

        settings = get_settings()
        if not settings.enable_web_search or search_provider(settings) != "parallel":
            return json.dumps({"error": "Parallel web search is not enabled"})
        if (refused := outside_reach_refusal(name)) is not None:
            return refused
        # Anonymous MCP has no hard source-policy overrides. Never weaken a
        # configured domain restriction or silently switch to another service.
        if settings.web_search_allowed_domains or settings.web_search_blocked_domains:
            return json.dumps({"error": "Domain filters require WEB_SEARCH_PROVIDER=anthropic"})
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            return json.dumps({"error": "Invalid web tool arguments"})
        if name == "web_search":
            objective = arguments.get("objective")
            queries = arguments.get("search_queries")
            if (
                not isinstance(objective, str) or not objective.strip()
                or not isinstance(queries, list) or not 1 <= len(queries) <= 3
                or any(not isinstance(q, str) or not q.strip() for q in queries)
            ):
                return json.dumps({"error": "Supply an objective and one to three search_queries"})
            params: dict[str, Any] = {"objective": objective, "search_queries": queries}
        else:
            urls = arguments.get("urls")
            objective = arguments.get("objective", "")
            if (
                not isinstance(urls, list) or not 1 <= len(urls) <= 20
                or any(not _valid_url(url) for url in urls)
                or not isinstance(objective, str) or len(objective) > 200
            ):
                return json.dumps({"error": "Supply public HTTP(S) urls and an objective of at most 200 characters"})
            params = {"urls": urls, "objective": objective}
        # No await before reservation: concurrent calls cannot race the cap.
        # Fetches share the cap so the new tool cannot make an unbounded turn.
        if self.calls >= self.max_uses:
            return json.dumps({"error": "Web search/fetch limit reached for this turn"})
        self.calls += 1
        try:
            async with asyncio.timeout(MCP_TIMEOUT_SECONDS):
                if self.conversation_id is not None:
                    from openexecutive.memory.session_store import search_conversation_id

                    self.session_id = await asyncio.to_thread(search_conversation_id, self.conversation_id)
                params["session_id"] = self.session_id
                result = await _mcp_call(name, params)
            text = "\n".join(block.text for block in result.content if block.type == "text")
            if result.isError:
                return json.dumps({"error": "Parallel returned an error", "message": text})
            if not text:
                return json.dumps({"error": "Parallel returned no text content"})
            return text
        except TimeoutError:
            return json.dumps({"error": "Parallel request timed out"})
        except Exception:
            # Transport errors can include headers; never expose credentials.
            # CancelledError is a BaseException and propagates to the caller.
            return json.dumps({"error": "Parallel request failed"})


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".localhost", ".local")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass  # DNS/redirect safety at the remote extraction service.
        return (
            parts.scheme in {"http", "https"} and bool(parts.hostname)
            and not parts.username and not parts.password
            and not any(c.isspace() for c in value)
        )
    except ValueError:
        return False
