"""Deep reasoning beyond Anthropic-direct Claude.

The Council "Deep reasoning" checkbox sets Anthropic-native ``thinking`` +
``output_config.effort``. These tests pin the three pieces that make it
work (or safely no-op) for every model:

1. Translator: those fields become OpenRouter's unified ``reasoning``
   parameter on the OpenRouter path — for Claude-via-OpenRouter AND for
   non-Claude models — and are never forwarded as raw ``thinking``.
2. Registry / catalog: a non-Claude catalog model advertising "reasoning"
   in ``supported_parameters`` keeps its thinking fields through the
   feature gate; one that doesn't (or an unknown slug) has them stripped.
3. Haiku guard: Haiku rejects adaptive thinking, so the agent call paths
   never attach it for a Haiku model, whatever the checkbox says.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

import httpx  # noqa: E402
import pytest  # noqa: E402

from openexecutive.providers import openrouter_catalog as catalog  # noqa: E402
from openexecutive.providers import registry as registry_mod  # noqa: E402
from openexecutive.providers.feature_gate import apply_feature_gates  # noqa: E402
from openexecutive.providers.registry import (  # noqa: E402
    get_provider,
    model_supports_deep_reasoning,
)
from openexecutive.providers.translator import (  # noqa: E402
    OPENROUTER_REASONING_BLOCK,
    StreamAccumulator,
    from_openai_response,
    reasoning_replay_block,
    to_openai_request,
)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    catalog._reset_for_tests()
    registry_mod._reset_for_tests()
    yield
    catalog._reset_for_tests()
    registry_mod._reset_for_tests()


def _registry_settings(*, enabled: bool = True) -> Any:
    return SimpleNamespace(
        anthropic_api_key="sk-test",
        openrouter_enabled=enabled,
        openrouter_api_key="sk-or-test",
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_app_title="Open Executive",
        openrouter_referer=None,
        openrouter_timeout_s=180.0,
        local_models_enabled=False,
        local_base_url=None,
        local_models=[],
        local_api_key=None,
        local_timeout_s=300.0,
    )


def _deep_kwargs(model: str, effort: str = "low") -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }


# --------------------------------------------------------------------------
# 1. Translator: thinking + effort → OpenRouter ``reasoning``
# --------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_adaptive_thinking_becomes_reasoning_effort(effort: str) -> None:
    body = to_openai_request("openai/gpt-6", _deep_kwargs("openai/gpt-6", effort))
    assert body["reasoning"] == {"effort": effort}
    # Anthropic-only fields must never leak into an OpenAI-format body.
    assert "thinking" not in body
    assert "output_config" not in body


def test_adaptive_thinking_without_effort_defaults_to_low() -> None:
    kwargs = _deep_kwargs("openai/gpt-6")
    del kwargs["output_config"]
    assert to_openai_request("openai/gpt-6", kwargs)["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("bad", ["ludicrous", "High", "low ", "", 3, None])
def test_invalid_effort_fails_closed_to_low(bad: Any) -> None:
    """A mistyped SPECIALIST_EFFORT must never escalate to an expensive tier."""
    kwargs = _deep_kwargs("openai/gpt-6")
    kwargs["output_config"] = {"effort": bad}
    assert to_openai_request("openai/gpt-6", kwargs)["reasoning"] == {"effort": "low"}


def test_legacy_budget_tokens_becomes_reasoning_max_tokens() -> None:
    kwargs = _deep_kwargs("x")
    kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4096}
    assert to_openai_request("x", kwargs)["reasoning"] == {"max_tokens": 4096}


@pytest.mark.parametrize(
    "thinking",
    [None, {"type": "disabled"}, "adaptive", {}],
    ids=["absent", "disabled", "not-a-dict", "no-type"],
)
def test_no_reasoning_field_when_thinking_is_off_or_malformed(thinking: Any) -> None:
    kwargs = _deep_kwargs("x")
    if thinking is None:
        del kwargs["thinking"]
    else:
        kwargs["thinking"] = thinking
    assert "reasoning" not in to_openai_request("x", kwargs)


@pytest.mark.parametrize("budget", [0, -1, None, True, "4096"])
def test_enabled_without_usable_budget_sends_no_reasoning(budget: Any) -> None:
    kwargs = _deep_kwargs("x", "medium")
    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    assert "reasoning" not in to_openai_request("x", kwargs)


# --------------------------------------------------------------------------
# 2. Registry + catalog: which models keep their thinking fields
# --------------------------------------------------------------------------


def _load_catalog(models: list[str], reasoning: list[str]) -> None:
    catalog._loaded_models = list(models)
    catalog._loaded_reasoning = frozenset(reasoning)


def test_supports_reasoning_reports_catalog_capability() -> None:
    assert catalog.supports_reasoning("openai/gpt-6") is None  # nothing loaded
    _load_catalog(["openai/gpt-6", "meta-llama/llama-4-scout"], ["openai/gpt-6"])
    assert catalog.supports_reasoning("openai/gpt-6") is True
    assert catalog.supports_reasoning("meta-llama/llama-4-scout") is False
    assert catalog.supports_reasoning("not/in-catalog") is None


def test_reasoning_capable_ids_reads_supported_parameters() -> None:
    entries = [
        {"id": "a/one", "supported_parameters": ["tools", "reasoning"]},
        {"id": "a/two", "supported_parameters": ["tools"]},
        {"id": "a/three"},
        {"id": None, "supported_parameters": ["reasoning"]},
    ]
    assert catalog.reasoning_capable_ids(entries) == frozenset({"a/one"})


def test_catalog_refresh_records_reasoning_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    def _entry(model_id: str, *params: str) -> dict[str, Any]:
        return {
            "id": model_id,
            "created": 1,
            "supported_parameters": ["tools", *params],
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "architecture": {"output_modalities": ["text"]},
        }

    payload = {"data": [_entry("openai/gpt-6", "reasoning"), _entry("meta-llama/llama-4-scout")]}
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda _r: httpx.Response(200, json=payload))
        return real_client(**kwargs)

    monkeypatch.setattr(catalog.httpx, "AsyncClient", _factory)
    settings = SimpleNamespace(
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_catalog_timeout_s=1.0,
        openrouter_catalog_providers=["openai", "meta-llama"],
        openrouter_catalog_per_provider=6,
        openrouter_catalog_refresh_s=0.0,
    )
    import asyncio

    assert asyncio.run(catalog.refresh_openrouter_catalog(settings)) is True
    assert catalog.supports_reasoning("openai/gpt-6") is True
    assert catalog.supports_reasoning("meta-llama/llama-4-scout") is False


def test_openrouter_provider_keeps_thinking_only_for_reasoning_capable_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the registry resolver + feature gate + translator:
    the request body carries ``reasoning`` exactly when the model can use it."""
    monkeypatch.setattr(
        "openexecutive.providers.registry.get_settings",
        lambda: _registry_settings(),
    )
    _load_catalog(
        ["openai/gpt-6", "meta-llama/llama-4-scout", "anthropic/claude-opus-4.8"],
        ["openai/gpt-6", "anthropic/claude-opus-4.8"],
    )
    provider = get_provider("openai/gpt-6")

    def _body_for(model: str) -> dict[str, Any]:
        slug, spec = provider._resolve(model)
        kwargs = _deep_kwargs(model)
        kwargs.pop("model")
        return to_openai_request(slug, apply_feature_gates(spec, kwargs))

    # Reasoning-capable non-Claude: thinking survives the gate → reasoning.
    assert _body_for("openai/gpt-6")["reasoning"] == {"effort": "low"}
    # Catalog says no reasoning: stripped, no field.
    assert "reasoning" not in _body_for("meta-llama/llama-4-scout")
    # Unknown slug (e.g. from the hardcoded fallback): conservative default.
    assert "reasoning" not in _body_for("deepseek/not-in-catalog")
    # Claude via OpenRouter, both spellings: reasoning is sent.
    assert _body_for("claude-sonnet-5")["reasoning"] == {"effort": "low"}
    assert _body_for("anthropic/claude-opus-4.8")["reasoning"] == {"effort": "low"}


# --------------------------------------------------------------------------
# 3. Haiku guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-4-5", False),
        ("claude-haiku-4-5-20251001", False),
        ("anthropic/claude-haiku-4.5", False),
        ("Claude-Haiku-4-5", False),
        # Legacy ordering with the family last.
        ("claude-3-5-haiku-20241022", False),
        ("anthropic/claude-3.5-haiku", False),
        ("anthropic/claude-3-haiku", False),
        ("claude-sonnet-5", True),
        ("claude-opus-5", True),
        ("openai/gpt-6", True),
        ("llama3.3", True),
        # Structural match: an unrelated slug that merely contains the word
        # keeps deep reasoning; a Haiku alias without the word is not caught
        # here (the feature gate / Anthropic 400 is the backstop for that).
        ("local/haiku-clone", True),
        ("qwen/haiku-poet-7b", True),
    ],
)
def test_model_supports_deep_reasoning(model: str, expected: bool) -> None:
    assert model_supports_deep_reasoning(model) is expected


async def _analyze_kwargs(model: str, deep: bool) -> dict[str, Any]:
    from openexecutive.agents.strategy import StrategyAgent

    create = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    )
    fake_provider = SimpleNamespace(messages_create=create)
    with patch("openexecutive.agents.base.get_provider", return_value=fake_provider):
        await StrategyAgent().analyze(
            "q", model_override=model, deep_reasoning_override=deep
        )
    return create.await_args.kwargs


async def test_agent_never_sends_thinking_to_haiku() -> None:
    kw = await _analyze_kwargs("claude-haiku-4-5", deep=True)
    assert kw["model"] == "claude-haiku-4-5"
    assert "thinking" not in kw
    assert "output_config" not in kw


async def test_agent_sends_thinking_to_sonnet_when_deep_is_on() -> None:
    kw = await _analyze_kwargs("claude-sonnet-5", deep=True)
    assert kw["thinking"] == {"type": "adaptive"}
    assert "effort" in kw["output_config"]


async def test_agent_sends_no_thinking_when_deep_is_off() -> None:
    kw = await _analyze_kwargs("claude-sonnet-5", deep=False)
    assert "thinking" not in kw


# --------------------------------------------------------------------------
# 4. Reasoning continuity across a multi-turn tool loop (OpenRouter path)
# --------------------------------------------------------------------------

_DETAILS = [{"type": "reasoning.summary", "summary": "think", "id": "r1", "format": "x"}]


def test_response_carries_reasoning_details_as_a_block() -> None:
    msg = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "reasoning_details": _DETAILS,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "f", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    types = [b.type for b in msg.content]
    # Reasoning is LAST: ``content[0]`` must stay the text block for the
    # callers that index it directly (wait_for_human, eval judges).
    assert types == ["text", "tool_use", OPENROUTER_REASONING_BLOCK]
    assert msg.content[0].text == "hi"
    assert msg.content[-1].reasoning_details == _DETAILS
    # Absent / empty / non-list → no block.
    for bad in (None, [], "x"):
        m = from_openai_response({"choices": [{"message": {"content": "a", "reasoning_details": bad}}]})
        assert [b.type for b in m.content] == ["text"]


def test_stream_accumulates_reasoning_details_into_one_block() -> None:
    acc = StreamAccumulator()
    acc.feed({"id": "m", "choices": [{"delta": {"reasoning_details": _DETAILS[:1]}}]})
    acc.feed({"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "…"}]}}]})
    acc.feed({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]})
    final = acc.finalize()
    assert [b.type for b in final.content] == ["text", OPENROUTER_REASONING_BLOCK]
    assert final.content[0].text == "answer"
    assert len(final.content[-1].reasoning_details) == 2


def test_reasoning_block_is_replayed_as_reasoning_details_on_assistant_turn() -> None:
    body = to_openai_request(
        "openai/gpt-6",
        {
            "messages": [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": OPENROUTER_REASONING_BLOCK, "reasoning_details": _DETAILS},
                        {"type": "tool_use", "id": "c1", "name": "f", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "c1", "content": "r"}
                    ],
                },
            ]
        },
    )
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_details"] == _DETAILS
    assert assistant["tool_calls"][0]["id"] == "c1"
    # The synthetic block never leaks as text.
    assert assistant["content"] is None


def test_research_loop_echoes_reasoning_block_into_history() -> None:
    from openexecutive.monitoring.research.agentic import _assistant_turn

    block = SimpleNamespace(type=OPENROUTER_REASONING_BLOCK, reasoning_details=_DETAILS)
    tool = SimpleNamespace(type="tool_use", id="c1", name="scrape_url", input={"url": "u"})
    history, scrapes = _assistant_turn([block, tool])
    assert history[0] == {"type": OPENROUTER_REASONING_BLOCK, "reasoning_details": _DETAILS}
    assert history[1]["type"] == "tool_use"
    assert scrapes == [{"id": "c1", "input": {"url": "u"}}]


# --------------------------------------------------------------------------
# 5. Remote catalog data: capability flags need a real list
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    ["tools,reasoning", {"reasoning": True}, None, 7],
    ids=["string", "dict", "none", "int"],
)
def test_reasoning_capability_requires_a_list(params: Any) -> None:
    assert catalog.reasoning_capable_ids([{"id": "a/one", "supported_parameters": params}]) == frozenset()


def test_tools_capability_requires_a_list() -> None:
    entry = {
        "id": "openai/x",
        "created": 1,
        "supported_parameters": "tools",  # substring would match; a list is required
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "architecture": {"output_modalities": ["text"]},
    }
    assert catalog.select_models([entry], providers=["openai"]) == []


# --------------------------------------------------------------------------
# 6. Config: SPECIALIST_EFFORT is validated at boot
# --------------------------------------------------------------------------


def test_specialist_effort_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from openexecutive.config import Settings

    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "exec@example.com")
    monkeypatch.setenv("SPECIALIST_EFFORT", "Low")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("SPECIALIST_EFFORT", "xhigh")
    assert Settings().specialist_effort == "xhigh"


# --------------------------------------------------------------------------
# 7. A reasoning-only (truncated) reply is logged, not silently empty
# --------------------------------------------------------------------------


async def test_empty_specialist_reply_is_logged() -> None:
    """Asserted on the module logger directly: the app's logging config
    (exercised by other tests in the suite) turns propagation off for the
    ``openexecutive`` tree, which would make ``caplog`` miss the record."""
    from openexecutive.agents import base as base_mod
    from openexecutive.agents.strategy import StrategyAgent

    create = AsyncMock(return_value=SimpleNamespace(content=[], stop_reason="max_tokens"))
    with (
        patch(
            "openexecutive.agents.base.get_provider",
            return_value=SimpleNamespace(messages_create=create),
        ),
        patch.object(base_mod.logger, "warning") as warn,
    ):
        out = await StrategyAgent().analyze(
            "q", model_override="openai/gpt-6", deep_reasoning_override=True
        )
    assert out == ""
    warn.assert_called_once()
    rendered = warn.call_args.args[0] % warn.call_args.args[1:]
    assert "returned no text block" in rendered
    assert "max_tokens" in rendered
    assert "openai/gpt-6" in rendered


def test_content_zero_stays_text_for_direct_indexers() -> None:
    """workflows.wait_for_human and evals.judges read ``content[0].text``; a
    natively-reasoning OpenRouter model returns reasoning_details even when
    not asked, so the synthetic block must never take slot 0."""
    msg = from_openai_response(
        {"choices": [{"message": {"content": "APPROVE", "reasoning_details": _DETAILS}}]}
    )
    assert msg.content[0].text == "APPROVE"


def test_reasoning_replay_block_helper() -> None:
    block = SimpleNamespace(type=OPENROUTER_REASONING_BLOCK, reasoning_details=_DETAILS)
    assert reasoning_replay_block(block) == {
        "type": OPENROUTER_REASONING_BLOCK,
        "reasoning_details": _DETAILS,
    }
    for other in (
        SimpleNamespace(type="text", text="x"),
        SimpleNamespace(type=OPENROUTER_REASONING_BLOCK, reasoning_details=[]),
        SimpleNamespace(type=OPENROUTER_REASONING_BLOCK, reasoning_details="nope"),
        SimpleNamespace(type=OPENROUTER_REASONING_BLOCK),
        object(),
    ):
        assert reasoning_replay_block(other) is None


def test_every_tool_loop_replays_reasoning() -> None:
    """The block-by-block assistant-history rebuilders all consult the
    helper, so continuity isn't a research-loop-only property."""
    import inspect

    from openexecutive.monitoring.research import agentic
    from openexecutive.orchestrator import executive
    from openexecutive.workflows import executive_reflection, executive_research

    for mod in (agentic, executive, executive_research, executive_reflection):
        assert "reasoning_replay_block(block)" in inspect.getsource(mod), mod.__name__
