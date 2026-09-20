"""Anthropic ↔ OpenAI-format request/response translation.

OpenRouter exposes an OpenAI-format ``/api/v1/chat/completions`` endpoint
(no Anthropic-Messages-compatible URL). To avoid rewriting every
Anthropic-shaped call site, we keep the internal request/response shape
Anthropic-native and translate only at the OpenRouter boundary.

The translator is intentionally functional — three pure helpers — so
the matrix of (system blocks, tool use, streaming tool fragments) is
testable in isolation without an HTTP client or an event loop.

What we DO NOT translate:

* ``cache_control`` blocks pass through unchanged when present. We
  preserve them on system content, user-turn content, tool entries, and
  tool-result content (the agent loop's intra-turn breakpoint)
  so OpenRouter can forward the Anthropic cache hints to the upstream
  Anthropic call (see OpenRouter prompt-caching docs:
  https://openrouter.ai/docs/guides/best-practices/prompt-caching).
  For non-Anthropic slugs the feature_gate has already stripped them
  before we get here, so the no-cache_control path falls back to the
  legacy string-flatten form for maximum upstream compatibility.
* Anthropic thinking / output_config blocks are NOT forwarded verbatim —
  OpenRouter has no ``thinking`` field. When feature_gate has preserved
  them (Claude family, or a catalog model that advertises ``reasoning``
  support) ``to_openai_request`` translates their intent into OpenRouter's
  unified ``reasoning`` parameter (``{"effort": ...}`` for adaptive
  thinking, ``{"max_tokens": N}`` for a legacy ``budget_tokens`` request).
  OpenRouter maps that onto each vendor's native form (Anthropic budget /
  effort, OpenAI reasoning effort, Gemini thinkingLevel, …). The
  ``reasoning`` / ``reasoning_details`` fields OpenRouter adds to the
  response are ignored by the response path — only ``content`` and
  ``tool_calls`` are read. See
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.
* Web-search server tools — feature_gate strips these only for the local
  OpenAI-compatible backend. For every OpenRouter model the Anthropic
  ``web_search_*`` server tool (which OpenRouter can't execute as-is, and
  which has no ``input_schema``) is replaced in ``tools[]`` by OpenRouter's
  own ``openrouter:web_search`` server tool with the same ``max_uses`` and
  domain lists, so the search cap and filters hold on this path and the
  response's ``usage.web_search_requests`` is recorded. See
  https://openrouter.ai/docs/guides/features/server-tools/web-search.
  OpenRouter's search makes the model cite results with
  ``(cite index="...">…</cite>`` markup; the response path strips that
  markup so it doesn't leak into findings / chat text.
"""
from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any


def _any_block_has_cache_control(blocks: Any) -> bool:
    """True iff at least one dict in ``blocks`` carries a real (dict-valued)
    ``cache_control`` marker. We require the value to be a dict because a
    caller setting ``cache_control: None`` should NOT flip us into the
    typed-block path — that would emit an array shape with zero cache
    markers, wasting wire bytes for no benefit."""
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(b, dict) and isinstance(b.get("cache_control"), dict)
        for b in blocks
    )


def _typed_text_with_cc(text: str, cc: Any) -> dict[str, Any] | None:
    """Build one typed text block, carrying ``cache_control`` when it is real.

    The single place that decides how a cache marker is projected onto the
    wire, shared by the system/user text path (`_typed_text_block`) and the
    tool-result path. Pass the full ``cache_control`` dict through —
    ``ttl: "1h"`` and any future extension fields ride along untouched.
    OpenRouter forwards this to Anthropic verbatim.

    Returns ``None`` for empty text: an empty typed block carries no content
    to cache and risks an upstream 400.
    """
    if not isinstance(text, str) or not text:
        return None
    out: dict[str, Any] = {"type": "text", "text": text}
    if isinstance(cc, dict):
        out["cache_control"] = cc
    return out


def _typed_text_block(block: dict[str, Any]) -> dict[str, Any] | None:
    """Project an Anthropic text block down to the typed-block shape OpenRouter
    forwards to Anthropic. Preserves ``cache_control`` (including the optional
    ``ttl`` extension); drops anything else to keep the wire payload minimal."""
    if block.get("type") != "text":
        return None
    return _typed_text_with_cc(block.get("text", ""), block.get("cache_control"))


def _translate_system(system: Any) -> str | list[dict[str, Any]] | None:
    """Translate an Anthropic ``system`` argument to the OpenRouter shape.

    Two output forms, picked to maximize compatibility:

    * ``None`` when there's nothing to send (caller omits the system message).
    * A plain ``str`` when system is a string OR a list of blocks where NO
      block carries ``cache_control``. The string form is the broadest
      OpenAI-compatible shape; we use it whenever caching isn't in play.
    * A ``list[dict]`` of typed text blocks (with ``cache_control``
      preserved on the relevant blocks) when at least one block has
      ``cache_control``. This is the shape OpenRouter accepts and forwards
      to Anthropic so prompt caching actually engages. See
      https://openrouter.ai/docs/guides/best-practices/prompt-caching.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        if _any_block_has_cache_control(system):
            typed = [b for b in (_typed_text_block(blk) for blk in system if isinstance(blk, dict)) if b is not None]
            return typed or None
        chunks: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if isinstance(txt, str) and txt:
                    chunks.append(txt)
        return "\n\n".join(chunks) or None
    return str(system) or None


def _request_caches_tool_results(messages: list[Any]) -> bool:
    """True iff any ``tool_result`` block in the request carries a marker.

    Drives a UNIFORM tool-message shape for the whole request, which the
    prompt cache depends on. The agent loop's marker moves each iteration,
    so a given tool result is marked on the iteration it is newest and
    unmarked on every later one. If the shape followed the marker, that
    result would serialize as a typed array once and as a flat string
    afterwards — the cached prefix bytes would change under the very
    breakpoint that was supposed to read them, and every iteration would
    miss. (The ``cache_control`` key itself is not an invalidator; a
    content-shape change is.) So the decision is made per request, not per
    block: if anything in this request is cached, every tool message uses
    the typed form.
    """
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("cache_control"), dict)
            ):
                return True
    return False


def _anthropic_messages_to_openai(messages: list[Any]) -> list[dict[str, Any]]:
    """Convert Anthropic ``messages`` to OpenAI chat-completions ``messages``.

    Anthropic shape: ``[{"role": "user"|"assistant", "content": str | list[block]}]``.
    OpenAI shape: ``[{"role": "user"|"assistant", "content": str}]`` plus optional
    ``tool_calls`` on assistant turns, with separate ``tool`` role messages for
    tool results.
    """
    out: list[dict[str, Any]] = []
    typed_tool_results = _request_caches_tool_results(messages)
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            out.extend(_user_content_to_openai(content, typed_tool_results))
        elif role == "assistant":
            out.append(_assistant_content_to_openai(content))
        else:
            # Unknown role — preserve as best-effort.
            out.append({"role": role, "content": _content_to_text(content)})
    return out


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if isinstance(txt, str):
                    chunks.append(txt)
        return "\n\n".join(chunks)
    return ""


def _user_content_to_openai(
    content: Any, typed_tool_results: bool = False
) -> list[dict[str, Any]]:
    """User-turn content: emit one user message, plus one ``tool`` role message
    per Anthropic ``tool_result`` block so the OpenAI chat history threads
    correctly through tool-use turns.

    When any text block in ``content`` carries ``cache_control`` (used for
    Anthropic's rolling user-turn cache), the user message's ``content``
    stays as a typed-block array so the cache hint survives translation to
    OpenRouter. Otherwise we flatten to a plain string — broader upstream
    compatibility for non-Anthropic routing and slightly smaller wire bytes.
    """
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return [{"role": "user", "content": str(content)}]

    text_blocks: list[dict[str, Any]] = []  # populated when cache_control present
    text_chunks: list[str] = []  # populated for the flat-string fallback
    tool_messages: list[dict[str, Any]] = []
    preserve_typed = _any_block_has_cache_control(content)

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if isinstance(txt, str) and txt:
                text_chunks.append(txt)
                if preserve_typed:
                    typed = _typed_text_block(block)
                    if typed is not None:
                        text_blocks.append(typed)
        elif btype == "tool_result":
            tool_use_id = block.get("tool_use_id")
            inner = block.get("content")
            tool_text = _content_to_text(inner)
            # A cache_control marker on a tool_result is the agent loop's
            # intra-turn breakpoint (executive._apply_loop_cache_marker). It
            # must survive translation or the loop re-pays full input price
            # for its own transcript on every iteration. Carrying it on the
            # tool message keeps it AFTER the tool results in wire order —
            # a marker text block in the user message would be emitted
            # before them (tool messages are appended last, below).
            typed = _typed_text_with_cc(tool_text, block.get("cache_control"))
            # Shape is decided per REQUEST, not per block — see
            # _request_caches_tool_results. Following the marker would flip
            # this result between array and string as the marker moves,
            # changing the cached prefix out from under the breakpoint.
            # Uncached requests keep the flat string they have always sent.
            tool_content: Any = tool_text
            if typed_tool_results and typed is not None:
                tool_content = [typed]
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": tool_content,
                }
            )

    msgs: list[dict[str, Any]] = []
    if preserve_typed and text_blocks:
        msgs.append({"role": "user", "content": text_blocks})
    elif text_chunks:
        # Fall back to the flat-string form even when ``preserve_typed`` is
        # True but ``text_blocks`` is empty — that happens when the only
        # cache_control marker rides on a non-text block. A tool_result
        # marker is carried on its own tool message above, so this is not
        # a dropped marker; it just means this user message has no text
        # block of its own to mark. We don't want to drop the user's
        # actual text just because the marker lives elsewhere.
        msgs.append({"role": "user", "content": "\n\n".join(text_chunks)})
    msgs.extend(tool_messages)
    return msgs


def _assistant_content_to_openai(content: Any) -> dict[str, Any]:
    """Assistant-turn content: collapse text blocks; lift tool_use blocks to
    OpenAI ``tool_calls``."""
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    if not isinstance(content, list):
        return {"role": "assistant", "content": str(content)}

    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_details: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = block.get("text", "")
            if isinstance(txt, str) and txt:
                text_chunks.append(txt)
        elif btype == OPENROUTER_REASONING_BLOCK:
            details = block.get("reasoning_details")
            if isinstance(details, list):
                reasoning_details.extend(details)
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    out: dict[str, Any] = {"role": "assistant"}
    out["content"] = "\n\n".join(text_chunks) if text_chunks else None
    if reasoning_details:
        out["reasoning_details"] = reasoning_details
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _anthropic_tools_to_openai(tools: list[Any]) -> list[dict[str, Any]]:
    """Anthropic ``tools[]`` (with ``input_schema``) → OpenAI ``tools[]``
    (with ``function.parameters``). Preserves ``cache_control`` on the
    matching translated tool entry so OpenRouter can forward Anthropic's
    tools-prefix cache hint to the upstream Anthropic call. For
    non-Anthropic routing, feature_gate already stripped the marker
    before we ran, so the no-cache_control path is the legacy shape."""
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Skip Anthropic server tools (no input_schema, uses 'type' instead).
        if "input_schema" not in t and t.get("type", "").startswith(
            ("web_search_", "computer_", "bash_", "code_execution_")
        ):
            continue
        params = t.get("input_schema") or {"type": "object", "properties": {}}
        entry: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": params,
            },
        }
        cc = t.get("cache_control")
        if isinstance(cc, dict):
            entry["cache_control"] = cc
        out.append(entry)
    return out


# Anthropic native web-search server-tool type prefix (e.g.
# ``web_search_20250305``). OpenRouter cannot execute Anthropic's server tool,
# but its own server tool does the same job, so we translate the intent.
logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL_PREFIX = "web_search_"
# OpenRouter's own web-search server tool. Like Anthropic's, it runs inside
# the provider request: the model decides when to search (0..N times), the
# search count is capped server-side by ``max_uses``, domain lists apply, and
# the response reports ``usage.web_search_requests``. The engine is left on
# ``auto``: Claude, GPT, Gemini and Grok use their provider's native search,
# everything else Exa. See
# https://openrouter.ai/docs/guides/features/server-tools/web-search.
_OPENROUTER_WEB_SEARCH_TYPE = "openrouter:web_search"
# Tool-call ``type`` prefix of OpenRouter's server-side tools in a response.
# Those calls were executed upstream; they are never surfaced as tool_use
# blocks for the caller to run.
_OPENROUTER_SERVER_TOOL_PREFIX = "openrouter:"


def _is_server_tool_call(call: dict[str, Any]) -> bool:
    call_type = call.get("type")
    return isinstance(call_type, str) and call_type.startswith(_OPENROUTER_SERVER_TOOL_PREFIX)


def _web_search_server_tool(tools: Any) -> dict[str, Any] | None:
    """Return the OpenRouter ``openrouter:web_search`` server tool entry
    when ``tools`` carries an Anthropic native ``web_search_*`` server tool,
    else ``None``.

    OpenRouter's OpenAI-format endpoint can't run Anthropic's server-side
    ``web_search_20250305`` tool — ``_anthropic_tools_to_openai`` drops it
    (no ``input_schema``), which would silently strip web search from a call
    routed via OpenRouter. Its own server tool carries the same controls, so
    ``max_uses`` and the domain lists translate one-to-one
    (``blocked_domains`` → ``excluded_domains``).
    """
    if not isinstance(tools, list):
        return None
    for t in tools:
        if (
            isinstance(t, dict)
            and "input_schema" not in t
            and isinstance(t.get("type"), str)
            and t["type"].startswith(_WEB_SEARCH_TOOL_PREFIX)
        ):
            params: dict[str, Any] = {}
            max_uses = t.get("max_uses")
            # bool is an int subtype, so exclude it explicitly.
            if isinstance(max_uses, int) and not isinstance(max_uses, bool) and max_uses > 0:
                params["max_uses"] = max_uses
            allowed = t.get("allowed_domains")
            if isinstance(allowed, list) and allowed:
                params["allowed_domains"] = [str(d) for d in allowed]
            blocked = t.get("blocked_domains")
            if isinstance(blocked, list) and blocked:
                params["excluded_domains"] = [str(d) for d in blocked]
            tool: dict[str, Any] = {"type": _OPENROUTER_WEB_SEARCH_TYPE}
            if params:
                tool["parameters"] = params
            return tool
    return None


# OpenRouter's accepted ``reasoning.effort`` strings. Anthropic's
# ``output_config.effort`` values (low / medium / high / xhigh / max) are a
# subset, so they pass through unchanged. Anything else (or no effort at
# all) falls back to "low" — the cheapest real tier and this codebase's
# SPECIALIST_EFFORT default — so a typo can never silently escalate to the
# most expensive reasoning budget.
_OPENROUTER_EFFORT_LEVELS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_DEFAULT_REASONING_EFFORT = "low"


def _translate_reasoning(anthropic_kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Anthropic ``thinking`` + ``output_config.effort`` → OpenRouter ``reasoning``.

    * ``{"type": "adaptive"}`` (current models) → ``{"effort": <level>}`` where
      the level comes from ``output_config.effort``, defaulting to "low".
    * ``{"type": "enabled", "budget_tokens": N}`` (pre-4.6 models) →
      ``{"max_tokens": N}``.
    * ``{"type": "disabled"}``, absent, or malformed → ``None`` (no field).
    """
    thinking = anthropic_kwargs.get("thinking")
    if not isinstance(thinking, dict):
        return None
    kind = thinking.get("type")
    if kind == "enabled":
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
            return {"max_tokens": budget}
        # ``enabled`` with no usable budget is malformed — Anthropic itself
        # would reject it — so send no reasoning rather than guessing.
        return None
    if kind != "adaptive":
        return None
    output_config = anthropic_kwargs.get("output_config")
    effort = output_config.get("effort") if isinstance(output_config, dict) else None
    if not isinstance(effort, str) or effort not in _OPENROUTER_EFFORT_LEVELS:
        effort = _DEFAULT_REASONING_EFFORT
    return {"effort": effort}


def to_openai_request(
    model_slug: str, anthropic_kwargs: dict[str, Any], *, include_usage: bool = False
) -> dict[str, Any]:
    """Translate an Anthropic ``messages.create`` kwargs dict to an OpenAI
    ``/chat/completions`` body. ``model_slug`` is the OpenRouter model id."""
    body: dict[str, Any] = {
        "model": model_slug,
        "messages": [],
    }

    system_content = _translate_system(anthropic_kwargs.get("system"))
    if system_content is not None:
        body["messages"].append({"role": "system", "content": system_content})

    messages = anthropic_kwargs.get("messages") or []
    body["messages"].extend(_anthropic_messages_to_openai(messages))

    max_tokens = anthropic_kwargs.get("max_tokens")
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    temperature = anthropic_kwargs.get("temperature")
    if temperature is not None:
        body["temperature"] = temperature

    tools = anthropic_kwargs.get("tools")
    if tools:
        translated = _anthropic_tools_to_openai(tools)
        # Anthropic's web_search server tool is dropped from ``tools`` above
        # (OpenRouter can't execute it); OpenRouter's own server tool takes
        # its place with the same cap and domain lists.
        web_search = _web_search_server_tool(tools)
        if web_search is not None:
            translated.append(web_search)
        if translated:
            body["tools"] = translated

    tool_choice = anthropic_kwargs.get("tool_choice")
    if tool_choice is not None:
        body["tool_choice"] = _translate_tool_choice(tool_choice)

    # Deep reasoning: the Council checkbox sets Anthropic-native ``thinking``
    # + ``output_config.effort``; feature_gate leaves them in place only for
    # models that can reason, and here they become OpenRouter's ``reasoning``.
    reasoning = _translate_reasoning(anthropic_kwargs)
    if reasoning is not None:
        body["reasoning"] = reasoning

    # Ask OpenRouter to report the actual charged cost of this generation in
    # the response `usage` block (and the final usage chunk when streaming).
    # This is a read-only accounting flag — it does not alter the messages,
    # the cached system blocks, or the cache key, so prompt caching is
    # unaffected. The cost surfaces as `usage.cost` (USD) and is captured into
    # the per-call `cache_event` audit row downstream.
    #
    # OpenRouter-only: it's not part of the OpenAI or Anthropic request
    # schema. A generic self-hosted/gateway backend (Ollama, vLLM, or a
    # LiteLLM gateway fronting real Anthropic) may forward the request
    # nearly verbatim to a stricter upstream that rejects an unrecognized
    # top-level field outright instead of ignoring it — so only set it when
    # the caller has confirmed the backend is actually OpenRouter.
    if include_usage:
        body["usage"] = {"include": True}

    return body


def _translate_tool_choice(tc: Any) -> Any:
    """``{"type":"tool","name":"X"}`` → ``{"type":"function","function":{"name":"X"}}``.
    Pass-through for ``{"type":"any"}`` and ``{"type":"auto"}``."""
    if isinstance(tc, dict):
        if tc.get("type") == "tool" and "name" in tc:
            return {"type": "function", "function": {"name": tc["name"]}}
        if tc.get("type") in ("any", "auto"):
            return tc.get("type")
    return tc


# --------------------------------------------------------------------------
# OpenAI response → Anthropic-shape Message
# --------------------------------------------------------------------------


def _block(type_: str, **fields: Any) -> SimpleNamespace:
    """Build a duck-typed Anthropic block (TextBlock / ToolUseBlock). The
    Executive's streaming loop only reads ``.type`` and the type-specific
    attributes; a SimpleNamespace matches the shape without pulling in
    pydantic models from the SDK."""
    return SimpleNamespace(type=type_, **fields)


# Synthetic content-block type carrying OpenRouter's ``reasoning_details``
# through the Anthropic-shaped message so a multi-turn tool loop can echo
# it back. Always the LAST content block, so ``content[0]`` stays whatever
# it was before (text, or tool_use for a text-less turn). OpenRouter requires the array to be replayed verbatim on the
# assistant turn for reasoning continuity (Anthropic 400s a tool_use turn
# whose thinking was dropped; other vendors lose their chain of thought).
# https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning-blocks
OPENROUTER_REASONING_BLOCK = "openrouter_reasoning"


def reasoning_replay_block(block: Any) -> dict[str, Any] | None:
    """Dict form of a response ``openrouter_reasoning`` block for the next
    assistant turn, or ``None`` for any other block. Every multi-turn tool
    loop that rebuilds its assistant history block-by-block calls this so
    reasoning continuity is preserved on the OpenRouter path."""
    if getattr(block, "type", None) != OPENROUTER_REASONING_BLOCK:
        return None
    details = getattr(block, "reasoning_details", None)
    if not isinstance(details, list) or not details:
        return None
    return {"type": OPENROUTER_REASONING_BLOCK, "reasoning_details": list(details)}


def _reasoning_details_block(details: Any) -> SimpleNamespace | None:
    if isinstance(details, list) and details:
        return _block(OPENROUTER_REASONING_BLOCK, reasoning_details=list(details))
    return None


def _stop_reason_from_openai(reason: str | None) -> str:
    """Map OpenAI ``finish_reason`` to Anthropic ``stop_reason``."""
    if reason == "tool_calls" or reason == "function_call":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "stop":
        return "end_turn"
    if reason == "content_filter":
        return "refusal"
    return reason or "end_turn"


# OpenRouter's web search makes the model cite sources with inline
# ``<cite index="3-14,3-15">…</cite>`` markup (it appears both in free text and
# inside tool-call argument strings). The wrapper tags are an upstream artifact
# that would otherwise leak into research findings, artifacts, and chat replies,
# so we strip the tags while keeping the cited text and any real URLs intact.
_CITE_TAG_RE = re.compile(r"</?cite\b[^>]*>")
# A trailing, unterminated ``<cite…`` / ``</cite…`` left by a mid-tag
# truncation (or a stream cut between the tag name and its closing ``>``).
# Requires the full ``cite`` word + boundary, so ordinary trailing text — a
# lone ``<`` or a ``<cited`` word — is preserved, not mistaken for a cut tag.
_CITE_OPEN_TAIL_RE = re.compile(r"</?cite\b[^>]*\Z")


def _remove_complete_cite_tags(text: str) -> str:
    """Strip every complete ``<cite …>`` / ``</cite>`` tag, iterating to a
    fixpoint so a tag *formed* by removing an inner one (``<ci`` + removed tag
    + ``te>`` → ``<cite>``) is also caught. Each pass that changes anything
    removes ≥1 tag, so the loop is bounded by the tag count."""
    while True:
        stripped = _CITE_TAG_RE.sub("", text)
        if stripped == text:
            return stripped
        text = stripped


def _parse_tool_arguments(raw_args: Any, tool_name: str) -> Any:
    """Parse a tool call's JSON arguments. OpenRouter search's ``<cite>`` markup
    can land inside argument strings with unescaped quotes; strip it from the
    raw text before parsing so a citation cannot turn a full payload into an
    empty one. A payload that still fails to parse is logged (it would
    otherwise read as "the model returned nothing")."""
    if not isinstance(raw_args, str):
        return raw_args
    try:
        return json.loads(_CITE_TAG_RE.sub("", raw_args))
    except json.JSONDecodeError as exc:
        logger.warning(
            "openrouter: tool call %r carried unparseable JSON arguments (%s) — "
            "treating as empty", tool_name, exc,
        )
        return {}


def _strip_cite_markup(text: str) -> str:
    """Remove ``<cite …>`` / ``</cite>`` wrapper tags, preserving inner text.

    Also drops a trailing *unterminated* ``<cite…`` / ``</cite…`` fragment: a
    message truncated (or a stream ended) mid-tag would otherwise leak raw
    markup. The ``"<" not in`` fast-path skips the common no-markup case while
    still catching an orphan ``</cite>`` (a narrower ``"<cite"`` guard missed
    those)."""
    if not isinstance(text, str) or "<" not in text:
        return text
    out = _remove_complete_cite_tags(text)
    out = _CITE_OPEN_TAIL_RE.sub("", out)
    return out


def _strip_cite_in_value(value: Any) -> Any:
    """Recursively strip ``<cite>`` markup from every string in a parsed tool
    input (dict / list / str). Used on tool-call arguments so OpenRouter's
    citation markup doesn't survive into structured tool output (e.g. a
    research finding's ``summary``)."""
    if isinstance(value, str):
        return _strip_cite_markup(value)
    if isinstance(value, list):
        return [_strip_cite_in_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_cite_in_value(v) for k, v in value.items()}
    return value


def _could_be_cite_prefix(tail: str) -> bool:
    """True if ``tail`` could be the start of a (not-yet-complete) ``<cite>`` /
    ``</cite>`` tag whose ``>`` hasn't streamed in yet. Used to decide what to
    withhold at a chunk boundary so a tag split across SSE deltas is never
    emitted half-stripped. A trailing ``<`` that clearly isn't a cite tag
    (e.g. ``5 < 10``) returns False so ordinary text isn't withheld."""
    return (
        "<cite".startswith(tail)
        or "</cite".startswith(tail)
        or tail.startswith("<cite")
        or tail.startswith("</cite")
    )


def _stream_strip_cite(buf: str) -> tuple[str, str]:
    """Stream-safe ``<cite>`` stripper. Returns ``(emit, pending)``: complete
    cite tags are removed from ``buf``; ``pending`` is a trailing fragment that
    might be the opening of a cite tag still arriving in a later chunk (carried
    forward to the next ``feed`` and flushed at ``finish_reason``). Guarantees
    that the concatenation of all ``emit`` slices equals
    ``_strip_cite_markup`` of the full streamed text."""
    cleaned = _remove_complete_cite_tags(buf)
    lt = cleaned.rfind("<")
    if lt != -1 and ">" not in cleaned[lt:] and _could_be_cite_prefix(cleaned[lt:]):
        return cleaned[:lt], cleaned[lt:]
    return cleaned, ""


def from_openai_response(body: dict[str, Any]) -> SimpleNamespace:
    """OpenAI ``/chat/completions`` non-streaming response →
    Anthropic-shape ``Message`` (duck-typed)."""
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content_blocks: list[SimpleNamespace] = []

    text = msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append(_block("text", text=_strip_cite_markup(text)))

    skipped_server_calls = 0
    for call in msg.get("tool_calls") or []:
        if _is_server_tool_call(call):
            # OpenRouter executed it already (web search); not for the
            # caller to run.
            skipped_server_calls += 1
            logger.debug("openrouter: skipping server-side tool call %r", call.get("type"))
            continue
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        parsed = _parse_tool_arguments(raw_args, fn.get("name", ""))
        content_blocks.append(
            _block(
                "tool_use",
                id=call.get("id", ""),
                name=fn.get("name", ""),
                input=_strip_cite_in_value(parsed),
            )
        )
    stop_reason = _stop_reason_from_openai(choice.get("finish_reason"))
    if skipped_server_calls and not any(b.type == "tool_use" for b in content_blocks):
        # Every tool call was server-side: nothing is left for the caller
        # to run, so the turn is complete, not waiting on tool results.
        stop_reason = "end_turn"

    # Reasoning LAST. Several callers read ``content[0].text`` (the SDK
    # itself only ever emits text first for those prompts), so the synthetic
    # block must never displace the text block. Order carries no meaning on
    # the replay side — ``reasoning_details`` is a separate message field.
    reasoning_block = _reasoning_details_block(msg.get("reasoning_details"))
    if reasoning_block is not None:
        content_blocks.append(reasoning_block)

    usage = body.get("usage") or {}
    cache_read, cache_create = _extract_cache_token_counts(usage)
    return SimpleNamespace(
        id=body.get("id", ""),
        type="message",
        role="assistant",
        model=body.get("model", ""),
        content=content_blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=SimpleNamespace(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
            # Actual USD charged for this generation, present when the request
            # set `usage: {include: true}`. None for upstreams that omit it.
            cost=usage.get("cost"),
            server_tool_use=_server_tool_usage(usage),
        ),
    )


def _server_tool_usage(usage: dict[str, Any]) -> SimpleNamespace:
    """Anthropic-shape ``usage.server_tool_use`` from OpenRouter's usage.

    On the wire the count sits under ``usage.server_tool_use_details``
    (``{"web_search_requests": N, "tool_calls_requested": N,
    "tool_calls_executed": N}``, observed on the chat-completions endpoint);
    a flat ``usage.web_search_requests`` (the shape the docs show) is
    accepted too. 0 when neither is present."""
    details = usage.get("server_tool_use_details")
    raw = (
        details.get("web_search_requests", 0)
        if isinstance(details, dict)
        else usage.get("web_search_requests", 0)
    )
    try:
        count = int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        count = 0
    return SimpleNamespace(web_search_requests=count)


def _extract_cache_token_counts(usage: dict[str, Any]) -> tuple[int, int]:
    """Pull cache_read / cache_create token counts out of an OpenAI-format
    usage block, accommodating OpenRouter's nesting shape.

    OpenRouter's documented shape when caching engages
    (https://openrouter.ai/docs/guides/best-practices/prompt-caching):

        "usage": {
          "prompt_tokens": 10339,
          "prompt_tokens_details": {
            "cached_tokens": 10318,        # read from cache (cache HIT)
            "cache_write_tokens": 0        # written to cache (cache MISS that populated)
          }
        }

    We also accept a flat ``usage.cached_tokens`` fallback for upstreams
    that haven't adopted the nested form — the field used to be top-level
    in earlier OpenRouter responses and may still appear that way for
    some models.
    """
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens")
    if cache_read is None:
        cache_read = usage.get("cached_tokens", 0)
    cache_create = details.get("cache_write_tokens", 0)
    return int(cache_read or 0), int(cache_create or 0)


# --------------------------------------------------------------------------
# Streaming: OpenAI SSE chunks → Anthropic-shape stream events
# --------------------------------------------------------------------------


class StreamAccumulator:
    """Stateful translator that turns OpenAI SSE deltas into Anthropic events.

    OpenAI streams ``choices[0].delta`` fragments: a text delta, OR a tool_call
    fragment with ``function.arguments`` arriving in pieces, OR a finish_reason.
    Anthropic's stream is structured as ``content_block_start`` →
    ``content_block_delta`` (one per token chunk) → ``content_block_stop`` →
    ``message_delta`` (with ``stop_reason``) → ``message_stop``.

    The Executive's dispatch only inspects ``event.type == "content_block_delta"``
    + ``event.delta.type == "text_delta"`` for streaming text, and pulls the
    final message at ``await stream.get_final_message()``. We emit text deltas
    inline and stash tool-call fragments to assemble at stream close.
    """

    def __init__(self) -> None:
        # Index 0 = text block (always emitted if any text arrived).
        # Indices ≥1 = tool_use blocks keyed by OpenAI tool_call.index.
        self._text_started = False
        self._text_buf: list[str] = []
        # Withheld trailing fragment that might be the start of a <cite> tag
        # split across SSE chunks; flushed (stripped) at finish_reason.
        self._cite_pending = ""
        # tool_calls[idx] = {"id": ..., "name": ..., "arg_chunks": [..]}
        self._tool_calls: dict[int, dict[str, Any]] = {}
        # Indices of server-side tool calls (typed only on their first
        # delta); their continuation chunks carry only index + arguments
        # and must be dropped too.
        self._server_tool_indices: set[int] = set()
        # OpenRouter streams ``delta.reasoning_details`` chunks; collected
        # verbatim and re-emitted as one block at finalize().
        self._reasoning_details: list[Any] = []
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._model: str = ""
        self._id: str = ""

    def feed(self, chunk: dict[str, Any]) -> list[SimpleNamespace]:
        """Process one OpenAI SSE chunk. Returns 0+ Anthropic-shape events to
        forward to the consumer."""
        events: list[SimpleNamespace] = []

        if not self._id:
            self._id = chunk.get("id", "")
        if not self._model:
            self._model = chunk.get("model", "")

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage.update(usage)

        choices = chunk.get("choices") or []
        if not choices:
            return events
        choice = choices[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if isinstance(text, str) and text:
            if not self._text_started:
                self._text_started = True
                events.append(
                    _block(
                        "content_block_start",
                        index=0,
                        content_block=_block("text", text=""),
                    )
                )
            self._text_buf.append(text)
            # Strip cite markup on the live delta stream too — consumers
            # (executive.py chat loop) build the visible reply from these
            # deltas, not from the finalized message. Withhold any trailing
            # fragment that could be a tag split across chunks.
            emit, self._cite_pending = _stream_strip_cite(self._cite_pending + text)
            if emit:
                events.append(
                    _block(
                        "content_block_delta",
                        index=0,
                        delta=_block("text_delta", text=emit),
                    )
                )

        details = delta.get("reasoning_details")
        if isinstance(details, list):
            self._reasoning_details.extend(details)

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            if _is_server_tool_call(tc):
                self._server_tool_indices.add(idx)
                logger.debug("openrouter: skipping server-side tool call %r", tc.get("type"))
                continue
            if idx in self._server_tool_indices:
                continue  # continuation chunk of a server-side call
            slot = self._tool_calls.setdefault(
                idx, {"id": "", "name": "", "arg_chunks": []}
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            args = fn.get("arguments")
            if isinstance(args, str):
                slot["arg_chunks"].append(args)

        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]
            # Flush any withheld cite fragment. ``_strip_cite_markup`` drops a
            # still-unterminated ``<cite…`` left by a mid-tag truncation, so a
            # partial tag is never emitted; a non-cite remainder is preserved.
            if self._cite_pending:
                leftover = _strip_cite_markup(self._cite_pending)
                self._cite_pending = ""
                if leftover and self._text_started:
                    events.append(
                        _block(
                            "content_block_delta",
                            index=0,
                            delta=_block("text_delta", text=leftover),
                        )
                    )

        return events

    def finalize(self) -> SimpleNamespace:
        """Build the final Anthropic-shape ``Message`` from accumulated state.

        Called by the OpenRouterProvider stream wrapper when SSE closes,
        before the consumer awaits ``stream.get_final_message()``.
        """
        content_blocks: list[SimpleNamespace] = []
        if self._text_started:
            content_blocks.append(
                _block("text", text=_strip_cite_markup("".join(self._text_buf)))
            )
        for idx in sorted(self._tool_calls):
            slot = self._tool_calls[idx]
            joined_args = "".join(slot["arg_chunks"])
            parsed = _parse_tool_arguments(joined_args or "{}", slot["name"])
            content_blocks.append(
                _block(
                    "tool_use",
                    id=slot["id"],
                    name=slot["name"],
                    input=_strip_cite_in_value(parsed),
                )
            )
        # Reasoning last — see from_openai_response for why.
        reasoning_block = _reasoning_details_block(self._reasoning_details)
        if reasoning_block is not None:
            content_blocks.append(reasoning_block)
        cache_read, cache_create = _extract_cache_token_counts(self._usage)
        return SimpleNamespace(
            id=self._id,
            type="message",
            role="assistant",
            model=self._model,
            content=content_blocks,
            stop_reason=(
                "end_turn"
                if self._server_tool_indices and not self._tool_calls
                else _stop_reason_from_openai(self._finish_reason)
            ),
            stop_sequence=None,
            usage=SimpleNamespace(
                input_tokens=self._usage.get("prompt_tokens", 0),
                output_tokens=self._usage.get("completion_tokens", 0),
                cache_creation_input_tokens=cache_create,
                cache_read_input_tokens=cache_read,
                # Actual USD charged, from the stream's final usage chunk when
                # the request set `usage: {include: true}`. None if absent.
                cost=self._usage.get("cost"),
                server_tool_use=_server_tool_usage(self._usage),
            ),
        )
