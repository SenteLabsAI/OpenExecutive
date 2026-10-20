"""Search provider resolution and native/client tool definitions.

Fresh search configuration uses anonymous Parallel Search MCP. Explicit native
selection retains the server-side tool described below.

The `web_search_20250305` tool is executed by the Anthropic API itself — no
client-side handler is needed. The model issues `server_tool_use` blocks
during a generation and the API returns `web_search_tool_result` blocks
inline; both must be preserved in the assistant turn echoed back on the
next iteration so the model retains context for what it searched.

Because the block carries a `type` field instead of an `input_schema`, it
cannot be cached the way client-side tools are. The Executive keeps it
separate from the alphabetically-sorted client-tool list so the
`cache_control` marker stays on a real client tool.
"""
from __future__ import annotations

from typing import Any

from openexecutive.config import get_settings

WEB_SEARCH_TOOL_NAME = "web_search"


def build_web_search_tool(*, max_uses: int | None = None) -> dict[str, Any] | None:
    """Return the selected provider's web_search tool, or None when disabled.

    Read at tool-list assembly time (per turn) so toggling the env var
    without a restart takes effect on the next turn. ``max_uses`` overrides
    the shared ``WEB_SEARCH_MAX_USES`` knob for callers with their own
    (the research fan-out uses ``RESEARCH_WEB_SEARCH_MAX_USES``).
    """
    settings = get_settings()
    if not settings.enable_web_search:
        return None

    if search_provider(settings) == "parallel":
        from openexecutive.orchestrator.parallel_search import SEARCH_TOOL

        return SEARCH_TOOL

    tool: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": WEB_SEARCH_TOOL_NAME,
        "max_uses": max_uses if max_uses is not None else settings.web_search_max_uses,
    }
    if settings.web_search_allowed_domains:
        tool["allowed_domains"] = settings.web_search_allowed_domains
    if settings.web_search_blocked_domains:
        tool["blocked_domains"] = settings.web_search_blocked_domains
    return tool


def search_provider(settings: Any) -> str:
    """Resolve once from trusted settings, never on an individual tool error.

    ENABLE_WEB_SEARCH previously selected native search. Preserve installations
    that explicitly set it (including .env), along with their domain policies.
    An inference credential alone is not a search-provider selection.
    """
    if settings.web_search_provider != "auto":
        return str(settings.web_search_provider)
    if (
        "enable_web_search" in settings.model_fields_set
        or settings.web_search_allowed_domains
        or settings.web_search_blocked_domains
    ):
        return "anthropic"
    return "parallel"


def build_web_fetch_tool() -> dict[str, Any] | None:
    settings = get_settings()
    if settings.enable_web_search and search_provider(settings) == "parallel":
        from openexecutive.orchestrator.parallel_search import FETCH_TOOL

        return FETCH_TOOL
    return None
