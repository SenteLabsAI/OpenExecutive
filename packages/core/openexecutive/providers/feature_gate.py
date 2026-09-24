"""Spec-driven stripping of Anthropic-only request features.

When a non-Claude model is selected (or a Claude model is routed via a
backend that doesn't honor Anthropic-only features), we strip the
unsupported fields before the request leaves our process. The gate is
applied inside the OpenRouter provider so any future re-routing keeps
the same guarantees — a caller cannot accidentally ship a request with
``cache_control`` to a model that would 400 on it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeatureSpec:
    """What a given model is allowed to receive.

    All four flags default to True for Claude family; the registry's
    ``MODEL_SPECS`` table flips them off for non-Claude models so a
    misconfigured caller can't bypass the gate.
    """

    supports_cache_control: bool = True
    supports_thinking: bool = True
    supports_web_search: bool = True
    supports_tool_use: bool = True


def apply_feature_gates(spec: FeatureSpec, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a new kwargs dict with unsupported features removed.

    Never mutates the input — the caller's dict is reused on retry paths
    and elsewhere, and an in-place strip would corrupt those.
    """
    if (
        spec.supports_cache_control
        and spec.supports_thinking
        and spec.supports_web_search
        and spec.supports_tool_use
    ):
        # Fast path: nothing to strip.
        return kwargs

    out = deepcopy(kwargs)

    if not spec.supports_cache_control:
        _strip_cache_control(out)

    if not spec.supports_thinking:
        out.pop("thinking", None)
        out.pop("output_config", None)

    if not spec.supports_web_search:
        _strip_web_search_tools(out)

    if not spec.supports_tool_use:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    return out


def _strip_cache_control(kwargs: dict[str, Any]) -> None:
    """Remove ``cache_control`` from system blocks and tool entries.

    Non-Claude OpenRouter models 400 on unknown fields, and even where
    they tolerate them, the field has no semantic effect — so dropping
    it is the right call. Anthropic-routed callers never hit this path.
    """
    system = kwargs.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                block.pop("cache_control", None)

    tools = kwargs.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                t.pop("cache_control", None)

    # cache_control also appears on user-turn content blocks — notably the
    # agent loop's intra-turn marker, which rides on a tool_result block.
    # Strip it there too. One level deep is enough because every marker we
    # emit sits at a content block's own top level, never nested inside a
    # tool_result's own content list.
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)


def _strip_web_search_tools(kwargs: dict[str, Any]) -> None:
    """Remove Anthropic server-side web_search / web_fetch tool entries.

    These use a ``type: "web_search_…"`` field instead of ``input_schema``;
    OpenRouter has no equivalent server tool, so they must be dropped.
    """
    tools = kwargs.get("tools")
    if not isinstance(tools, list):
        return
    kwargs["tools"] = [
        t
        for t in tools
        if not (
            isinstance(t, dict)
            and isinstance(t.get("type"), str)
            and t["type"].startswith(("web_search_", "web_fetch_"))
        )
    ]
    if not kwargs["tools"]:
        kwargs.pop("tools", None)
