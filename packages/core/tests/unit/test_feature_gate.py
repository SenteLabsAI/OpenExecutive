"""Spec-driven stripping of Anthropic-only request features.

The gate is the safety net that keeps a non-Claude OpenRouter slug from
receiving fields that would 400 the request (``cache_control``,
adaptive thinking, server-side ``web_search``). These tests pin which
fields get removed for each combination of flags.
"""
from __future__ import annotations

from copy import deepcopy

from openexecutive.providers.feature_gate import FeatureSpec, apply_feature_gates


def _claude_spec() -> FeatureSpec:
    return FeatureSpec()  # all four defaults to True


def _non_claude_spec() -> FeatureSpec:
    return FeatureSpec(
        supports_cache_control=False,
        supports_thinking=False,
        supports_web_search=False,
        supports_tool_use=True,
    )


def test_claude_spec_is_a_passthrough_no_copy() -> None:
    """For the all-features-on case we return the exact same dict — the
    fast path keeps Anthropic-routed calls allocation-free."""
    kwargs = {
        "model": "claude-sonnet-4-6",
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    out = apply_feature_gates(_claude_spec(), kwargs)
    assert out is kwargs
    # And no fields were mutated either.
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_strips_cache_control_from_system_blocks() -> None:
    kwargs = {
        "system": [
            {"type": "text", "text": "block1", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "block2"},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    original = deepcopy(kwargs)
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    # Input must not have been mutated.
    assert kwargs == original
    # Cache markers gone.
    assert "cache_control" not in out["system"][0]
    # Text content survives untouched.
    assert out["system"][0]["text"] == "block1"
    assert out["system"][1]["text"] == "block2"


def test_strips_cache_control_from_tools_and_user_content_blocks() -> None:
    kwargs = {
        "tools": [
            {"name": "f1", "input_schema": {}},
            {
                "name": "f2",
                "input_schema": {},
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ctx", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "question"},
                ],
            }
        ],
    }
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    assert "cache_control" not in out["tools"][1]
    assert "cache_control" not in out["messages"][0]["content"][0]


def test_strips_thinking_and_output_config() -> None:
    kwargs = {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
        "max_tokens": 16000,
    }
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    assert "thinking" not in out
    assert "output_config" not in out
    # Unrelated fields untouched.
    assert out["max_tokens"] == 16000


def test_strips_web_search_server_tool_but_keeps_client_tools() -> None:
    kwargs = {
        "tools": [
            {"name": "consult_specialist", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search"},
        ],
    }
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    names = [t.get("name") for t in out.get("tools") or []]
    assert "consult_specialist" in names
    assert "web_search" not in names


def test_strips_all_tools_when_tool_use_unsupported() -> None:
    """``supports_tool_use=False`` removes both ``tools`` and ``tool_choice``
    so the body is valid even for non-tool-use models."""
    spec = FeatureSpec(
        supports_cache_control=False,
        supports_thinking=False,
        supports_web_search=False,
        supports_tool_use=False,
    )
    kwargs = {
        "tools": [{"name": "f", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
    }
    out = apply_feature_gates(spec, kwargs)
    assert "tools" not in out
    assert "tool_choice" not in out


def test_strip_web_search_drops_tools_key_when_only_web_search_present() -> None:
    """Removing every entry from ``tools`` should pop the key so OpenRouter
    doesn't see an empty list (which some models reject)."""
    kwargs = {
        "tools": [
            {"type": "web_search_20250305", "name": "web_search"},
        ],
    }
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    assert "tools" not in out


def test_strips_cache_control_from_tool_result_blocks() -> None:
    """The agent loop's intra-turn breakpoint rides on a tool_result block.

    Before that loop existed, `messages` almost never carried a marker, so
    this branch of the strip was close to dead code. Now every tool-loop
    request carries one, and a non-Claude backend 400s on the unknown
    field — so the strip is load-bearing on every such request.

    The marker must stay at the tool_result block's own top level: the
    strip walks one level deep and does NOT recurse into a nested
    tool_result["content"] list, so moving it inside would silently leak
    it upstream.
    """
    kwargs = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "output",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ]
    }
    out = apply_feature_gates(_non_claude_spec(), kwargs)
    assert "cache_control" not in out["messages"][0]["content"][0]
    # Never mutates the caller's dict — the loop reuses it next iteration.
    assert "cache_control" in kwargs["messages"][0]["content"][0]
