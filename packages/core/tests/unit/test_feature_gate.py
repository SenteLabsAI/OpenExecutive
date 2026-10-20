"""Spec-driven stripping of Anthropic-only request features.

The gate is the safety net that keeps a non-Claude OpenRouter slug from
receiving fields that would 400 the request (``cache_control``,
adaptive thinking, server-side ``web_search``). These tests pin which
fields get removed for each combination of flags.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from openexecutive.providers.anthropic_provider import AnthropicProvider
from openexecutive.providers.feature_gate import (
    FeatureSpec,
    apply_feature_gates,
    fit_claude_generation,
    rejects_forced_tool_choice,
    relax_forced_tool_choice,
)


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


# ── Forced tool_choice on models that reject it ─────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-opus-5-5-20260801",
        "anthropic/claude-opus-5.5",
        "anthropic/claude-sonnet-5.5",
    ],
)
def test_newest_claude_models_reject_forced_tool_choice(model: str) -> None:
    assert rejects_forced_tool_choice(model)


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-7",
     "anthropic/claude-opus-5", "openai/gpt-5", "llama3.3", "claude-opus-5-50"],
)
def test_other_models_keep_forced_tool_choice(model: str) -> None:
    assert not rejects_forced_tool_choice(model)


def test_relax_turns_a_named_tool_into_auto_without_mutating() -> None:
    kwargs = {
        "tool_choice": {"type": "tool", "name": "emit", "disable_parallel_tool_use": True},
        "tools": [{"name": "emit"}],
    }
    out = relax_forced_tool_choice("claude-sonnet-5-5", kwargs)
    assert out["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert out["tools"] == [{"name": "emit"}]
    assert kwargs["tool_choice"]["type"] == "tool"


def test_relax_turns_any_into_auto() -> None:
    out = relax_forced_tool_choice("claude-opus-5-5", {"tool_choice": {"type": "any"}})
    assert out["tool_choice"] == {"type": "auto"}


@pytest.mark.parametrize("choice", [{"type": "auto"}, {"type": "none"}, None])
def test_relax_leaves_unforced_choices_alone(choice: object) -> None:
    kwargs = {} if choice is None else {"tool_choice": choice}
    assert relax_forced_tool_choice("claude-opus-5-5", kwargs) is kwargs


def test_relax_leaves_older_models_forced() -> None:
    kwargs = {"tool_choice": {"type": "tool", "name": "emit"}}
    assert relax_forced_tool_choice("claude-sonnet-5", kwargs) is kwargs


def test_anthropic_provider_relaxes_before_calling_the_sdk() -> None:
    seen: dict[str, object] = {}

    class _Messages:
        async def create(self, **kw: object) -> object:
            seen.update(kw)
            return None

    provider = AnthropicProvider(api_key="test")
    provider._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    asyncio.run(provider.messages_create(
        model="claude-opus-5-5", tool_choice={"type": "tool", "name": "emit"},
    ))
    assert seen["tool_choice"] == {"type": "auto"}


@pytest.mark.parametrize(
    "model",
    ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5", "anthropic/claude-haiku-5.5"],
)
def test_newer_claude_models_drop_sampling_fields(model: str) -> None:
    kwargs = {"temperature": 0, "top_p": 0.5, "top_k": 5, "max_tokens": 32}
    out = fit_claude_generation(model, kwargs)
    assert "temperature" not in out and "top_p" not in out and "top_k" not in out
    assert out["max_tokens"] == 32
    assert kwargs["temperature"] == 0  # the caller's dict is untouched


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "llama3.3", "openai/gpt-5"])
def test_older_and_other_models_keep_sampling_fields(model: str) -> None:
    kwargs = {"temperature": 0, "max_tokens": 32}
    assert fit_claude_generation(model, kwargs) is kwargs


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "anthropic/claude-haiku-5.5"])
def test_haiku_5_5_thinks_only_when_asked(model: str) -> None:
    kwargs = {"max_tokens": 32}
    assert fit_claude_generation(model, kwargs)["thinking"] == {"type": "disabled"}
    asked = {"max_tokens": 4096, "thinking": {"type": "adaptive"}}
    assert fit_claude_generation(model, asked) is asked


def test_haiku_5_5_keeps_thinking_at_efforts_that_require_it() -> None:
    kwargs = {"output_config": {"effort": "max"}}
    assert "thinking" not in fit_claude_generation("claude-haiku-5-5", kwargs)


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-4-5"])
def test_other_models_get_no_thinking_field(model: str) -> None:
    assert "thinking" not in fit_claude_generation(model, {"max_tokens": 32})


def test_anthropic_provider_fits_the_request_before_calling_the_sdk() -> None:
    seen: dict[str, object] = {}

    class _Messages:
        async def create(self, **kw: object) -> object:
            seen.update(kw)
            return None

    provider = AnthropicProvider(api_key="test")
    provider._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    asyncio.run(provider.messages_create(
        model="claude-haiku-5-5", max_tokens=32, temperature=0,
    ))
    assert "temperature" not in seen
    assert seen["thinking"] == {"type": "disabled"}
