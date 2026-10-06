"""Spec-driven stripping of Anthropic-only request features.

When a non-Claude model is selected (or a Claude model is routed via a
backend that doesn't honor Anthropic-only features), we strip the
unsupported fields before the request leaves our process. The gate is
applied inside the OpenRouter provider so any future re-routing keeps
the same guarantees — a caller cannot accidentally ship a request with
``cache_control`` to a model that would 400 on it.
"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeatureSpec:
    """What a given model is allowed to receive.

    All flags default to True for Claude family; the registry's
    ``MODEL_SPECS`` table flips them off for non-Claude models so a
    misconfigured caller can't bypass the gate.
    """

    supports_cache_control: bool = True
    supports_thinking: bool = True
    supports_web_search: bool = True
    supports_tool_use: bool = True
    # Whether a PDF ``document`` block may be sent. Anthropic reads it
    # natively and OpenRouter parses one for any model; a self-hosted
    # OpenAI-compatible server usually cannot (LOCAL_PDF_INPUT says it can).
    supports_pdf_input: bool = True


# Claude models that reject a forced ``tool_choice`` (``any`` or a named
# ``tool``) with HTTP 400: Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1.
# Matched as an Anthropic id (``claude-opus-5-5``, optionally date-pinned)
# or an OpenRouter slug (``anthropic/claude-opus-5.5``).
_NO_FORCED_TOOL_CHOICE_RE = re.compile(
    r"^(?:anthropic/)?claude-(?:(?:opus|sonnet)-5[-.]5|(?:fable|mythos)-5[-.]1)(?:-\d{8})?$"
)


def rejects_forced_tool_choice(model: str) -> bool:
    """Whether ``model`` 400s on ``tool_choice`` ``any`` / ``tool``."""
    return bool(_NO_FORCED_TOOL_CHOICE_RE.match(model.lower()))


def relax_forced_tool_choice(model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Turn a forced ``tool_choice`` into ``auto`` for models that reject it.

    Callers that force a tool (the emit-a-structured-result pattern) already
    name that tool in their prompt and handle a reply without a ``tool_use``
    block, so ``auto`` keeps them working where the forced form would 400.
    ``disable_parallel_tool_use`` is kept. Returns ``kwargs`` itself when
    nothing changes, else a shallow copy — the caller's dict is never mutated.
    """
    choice = kwargs.get("tool_choice")
    if not (
        isinstance(choice, dict)
        and choice.get("type") in ("any", "tool")
        and rejects_forced_tool_choice(model)
    ):
        return kwargs
    relaxed: dict[str, Any] = {"type": "auto"}
    if "disable_parallel_tool_use" in choice:
        relaxed["disable_parallel_tool_use"] = choice["disable_parallel_tool_use"]
    return {**kwargs, "tool_choice": relaxed}


# What a PDF becomes for a model that cannot read one: said, never dropped.
PDF_OMITTED_NOTE = "[PDF omitted: this model can't read PDF files]"


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
        and spec.supports_pdf_input
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

    if not spec.supports_pdf_input:
        _replace_documents(out)

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


def _replace_documents(kwargs: dict[str, Any]) -> None:
    """Swap every ``document`` block in the messages for a text note, so the
    model is told a PDF was there instead of the block vanishing in
    translation."""
    for message in kwargs.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        message["content"] = [
            {"type": "text", "text": PDF_OMITTED_NOTE}
            if isinstance(block, dict) and block.get("type") == "document"
            else block
            for block in content
        ]
