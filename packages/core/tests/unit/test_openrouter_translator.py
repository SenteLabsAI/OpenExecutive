"""Translation between Anthropic and OpenAI request/response shapes.

These tests pin the wire format the OpenRouter provider sends and receives.
Bugs here surface deep inside ``executive.py``'s streaming dispatch, where
they're hardest to debug — so each shape is covered explicitly.
"""
from __future__ import annotations

import json

from openexecutive.providers.translator import (
    StreamAccumulator,
    from_openai_response,
    to_openai_request,
)  # noqa: I001 — explicit grouping; ruff wants a single line break, but the multi-line import is intentional for readability.

# --------------------------------------------------------------------------
# to_openai_request
# --------------------------------------------------------------------------


def test_request_flattens_system_blocks_into_string_when_no_cache_control() -> None:
    """No cache_control on any block → flat string. This is the non-Claude
    path (after feature_gate strips cache_control), kept for maximum
    upstream compatibility — most non-Anthropic OpenAI-format providers
    only accept a string ``content`` on system messages."""
    body = to_openai_request(
        "openai/gpt-5",
        {
            "model": "internal-name",  # popped by registry path; translator gets a slug
            "max_tokens": 256,
            "system": [
                {"type": "text", "text": "Persona block 1"},
                {"type": "text", "text": "Persona block 2"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert body["model"] == "openai/gpt-5"
    assert body["max_tokens"] == 256
    assert body["messages"][0] == {
        "role": "system",
        "content": "Persona block 1\n\nPersona block 2",
    }
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_request_preserves_system_typed_blocks_when_cache_control_present() -> None:
    """At least one cache_control on a system block → typed-block array
    survives translation so OpenRouter can forward the Anthropic cache
    hint to the upstream Anthropic call. This is the Claude path; without
    this preservation, every Executive turn would pay full freight for
    the ~15-20k-token system-prompt prefix (no cache hits)."""
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "max_tokens": 256,
            "system": [
                {
                    "type": "text",
                    "text": "Persona + knowledge index",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": "Company profile + org context",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    system_msg = body["messages"][0]
    assert system_msg["role"] == "system"
    # Content is a typed-block array, not a flat string.
    assert isinstance(system_msg["content"], list)
    assert len(system_msg["content"]) == 2
    # Block 0: persona block with the ``ttl: "1h"`` extension intact —
    # stripping it would silently downgrade block 0 from 1h to 5min TTL.
    assert system_msg["content"][0] == {
        "type": "text",
        "text": "Persona + knowledge index",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }
    # Block 1: default ephemeral cache_control preserved.
    assert system_msg["content"][1] == {
        "type": "text",
        "text": "Company profile + org context",
        "cache_control": {"type": "ephemeral"},
    }


def test_request_preserves_system_typed_blocks_with_partial_cache_control() -> None:
    """When some blocks have cache_control and others don't, the array form
    must include ALL text blocks (even the unmarked ones) so the cached
    prefix matches what Anthropic sees."""
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "system": [
                {
                    "type": "text",
                    "text": "Cached prefix",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "Tail (uncached, dynamic)"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    blocks = body["messages"][0]["content"]
    assert len(blocks) == 2
    assert "cache_control" in blocks[0]
    # The tail block has no cache_control and shouldn't synthesize one.
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"] == "Tail (uncached, dynamic)"


def test_request_omits_system_message_when_no_system_text() -> None:
    body = to_openai_request(
        "openai/gpt-5",
        {"max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]},
    )
    # First message is the user turn — no synthetic system row when none given.
    assert body["messages"][0]["role"] == "user"


def test_request_translates_anthropic_tools_to_openai_functions() -> None:
    """Baseline translation without cache_control — the legacy shape stays
    backward-compatible for non-Anthropic routing."""
    body = to_openai_request(
        "openai/gpt-5",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "consult_specialist",
                    "description": "Ask a specialist.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"specialist": {"type": "string"}},
                        "required": ["specialist"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "consult_specialist"},
        },
    )
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "consult_specialist",
                "description": "Ask a specialist.",
                "parameters": {
                    "type": "object",
                    "properties": {"specialist": {"type": "string"}},
                    "required": ["specialist"],
                },
            },
        }
    ]
    # tool_choice forced-call shape is rewritten.
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "consult_specialist"},
    }


def test_request_preserves_tool_cache_control_when_present() -> None:
    """When a tool entry carries cache_control (Anthropic's tools-prefix
    caching marker), the translated tool entry preserves it so OpenRouter
    forwards the cache hint upstream to Anthropic. Without this, the
    tool-definitions block (sorted tools, ~1-2k tokens) would not cache."""
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "consult_specialist",
                    "description": "Ask a specialist.",
                    "input_schema": {"type": "object", "properties": {}},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    )
    assert body["tools"][0]["cache_control"] == {"type": "ephemeral"}
    # Function/type fields still translated correctly.
    assert body["tools"][0]["function"]["name"] == "consult_specialist"


def test_request_preserves_user_content_blocks_when_cache_control_present() -> None:
    """Anthropic supports rolling cache on user-turn text blocks. When
    any text block in a user message carries cache_control, the user
    message's content must stay as a typed-block array so the cache
    hint survives translation. We don't write to this path yet, but the
    shape needs to round-trip correctly when callers do."""
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Long stable preamble",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": "Today's question"},
                    ],
                }
            ],
        },
    )
    user_msg = body["messages"][0]
    assert user_msg["role"] == "user"
    # Array form preserved, not flattened to a string.
    assert isinstance(user_msg["content"], list)
    assert len(user_msg["content"]) == 2
    assert user_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert user_msg["content"][1]["text"] == "Today's question"
    assert "cache_control" not in user_msg["content"][1]


def test_request_flattens_user_content_to_string_when_no_cache_control() -> None:
    """Existing behaviour preserved: when no user-content block has
    cache_control, content flattens to a single string (the broadest
    OpenAI-compatible shape)."""
    body = to_openai_request(
        "openai/gpt-5",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part one"},
                        {"type": "text", "text": "Part two"},
                    ],
                }
            ],
        },
    )
    user_msg = body["messages"][0]
    assert user_msg["content"] == "Part one\n\nPart two"


def test_request_drops_web_search_tool_but_injects_web_plugin() -> None:
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search"},
            ],
        },
    )
    # The Anthropic server tool has no OpenAI equivalent — it's still dropped
    # from ``tools`` so the body doesn't carry a phantom function entry...
    assert "tools" not in body or body["tools"] == []
    # ...but its intent is reproduced via OpenRouter's ``web`` plugin so search
    # actually runs (the bug this fixes: research specialists got no search and
    # emitted zero findings). Default max_results when no max_uses is given.
    assert body["plugins"] == [{"id": "web", "max_results": 5}]


def test_request_web_search_max_uses_maps_to_capped_max_results() -> None:
    # max_uses within the cap is honored verbatim.
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
            ],
        },
    )
    assert body["plugins"] == [{"id": "web", "max_results": 8}]

    # An oversized max_uses is capped so a search *count* can't become an
    # unbounded result *count* (Exa bills per result).
    capped = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 50},
            ],
        },
    )
    assert capped["plugins"] == [{"id": "web", "max_results": 10}]


def test_request_no_web_plugin_without_web_search_tool() -> None:
    body = to_openai_request(
        "anthropic/claude-opus-4.7",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "consult_specialist",
                    "description": "ask a specialist",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ],
        },
    )
    # Ordinary client tools must not trigger the web plugin.
    assert "plugins" not in body
    assert body["tools"][0]["function"]["name"] == "consult_specialist"


def test_request_lifts_assistant_tool_use_blocks_to_tool_calls() -> None:
    body = to_openai_request(
        "openai/gpt-5",
        {
            "messages": [
                {"role": "user", "content": "what's our burn?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "consult_specialist",
                            "input": {"specialist": "cfo"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "Burn is $100k/mo.",
                        }
                    ],
                },
            ],
        },
    )
    # Anthropic assistant turn → OpenAI assistant message + tool_calls.
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["content"] == "Let me check."
    assert assistant["tool_calls"][0]["function"]["name"] == "consult_specialist"
    args = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
    assert args == {"specialist": "cfo"}
    # Tool result becomes a separate ``tool`` role message keyed by id.
    tool_msg = next(m for m in body["messages"] if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "toolu_1"
    assert "Burn is $100k/mo" in tool_msg["content"]


# --------------------------------------------------------------------------
# from_openai_response
# --------------------------------------------------------------------------


def test_request_enables_usage_accounting_when_opted_in() -> None:
    """An OpenRouter call (include_usage=True) asks OpenRouter to report the
    charged cost so the per-call cache_event audit row can store it. This is
    a read-only flag and must not disturb messages / cache_control blocks."""
    body = to_openai_request(
        "anthropic/claude-opus-4.8",
        {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
        include_usage=True,
    )
    assert body["usage"] == {"include": True}


def test_request_omits_usage_accounting_by_default() -> None:
    """A generic self-hosted/gateway backend (the default) must NOT get the
    OpenRouter-only `usage` field — some upstreams (e.g. a LiteLLM gateway
    fronting real Anthropic) reject an unrecognized top-level field outright
    instead of ignoring it. Regression test for that exact failure."""
    body = to_openai_request(
        "claude-sonnet-4-6",
        {"max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert "usage" not in body


def test_response_synthesizes_text_block() -> None:
    msg = from_openai_response(
        {
            "id": "chatcmpl-1",
            "model": "openai/gpt-5",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    assert msg.content[0].type == "text"
    assert msg.content[0].text == "Hello!"
    assert msg.stop_reason == "end_turn"
    assert msg.usage.input_tokens == 10
    assert msg.usage.output_tokens == 5


def test_response_surfaces_cost_when_present() -> None:
    """OpenRouter reports the charged USD as usage.cost when accounting is on;
    it lands on Message.usage.cost for the cache_event audit row. Absent →
    None so aggregation treats it as 0."""
    with_cost = from_openai_response(
        {
            "id": "c1",
            "model": "anthropic/claude-opus-4.8",
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0123},
        }
    )
    assert with_cost.usage.cost == 0.0123

    without_cost = from_openai_response(
        {
            "id": "c2",
            "model": "openai/gpt-5",
            "choices": [{"message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    assert without_cost.usage.cost is None


def test_response_synthesizes_tool_use_block_with_parsed_arguments() -> None:
    msg = from_openai_response(
        {
            "id": "chatcmpl-2",
            "model": "openai/gpt-5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "type": "function",
                                "function": {
                                    "name": "consult_specialist",
                                    "arguments": '{"specialist": "cfo", "query": "burn"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    tool_use_blocks = [b for b in msg.content if b.type == "tool_use"]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0].id == "call_xyz"
    assert tool_use_blocks[0].name == "consult_specialist"
    # Anthropic ToolUseBlock.input is parsed JSON — the streaming loop
    # accesses it as a dict, so we must parse here.
    assert tool_use_blocks[0].input == {"specialist": "cfo", "query": "burn"}
    assert msg.stop_reason == "tool_use"


def test_response_usage_reads_nested_prompt_tokens_details() -> None:
    """OpenRouter returns cache token counts under usage.prompt_tokens_details
    when prompt caching engages:

        "usage": {
          "prompt_tokens": 10339,
          "prompt_tokens_details": {
            "cached_tokens": 10318,
            "cache_write_tokens": 0
          }
        }

    Both fields must land on the Anthropic-shape Message.usage so the
    cache_event audit row reports real numbers instead of always-zero."""
    msg = from_openai_response(
        {
            "id": "chatcmpl-cache-hit",
            "model": "anthropic/claude-opus-4.7",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10339,
                "completion_tokens": 60,
                "prompt_tokens_details": {
                    "cached_tokens": 10318,
                    "cache_write_tokens": 0,
                },
            },
        }
    )
    assert msg.usage.input_tokens == 10339
    assert msg.usage.output_tokens == 60
    assert msg.usage.cache_read_input_tokens == 10318
    assert msg.usage.cache_creation_input_tokens == 0


def test_response_usage_reports_cache_write_on_first_turn() -> None:
    """The first turn after a cache invalidation populates the cache —
    OpenRouter reports the write token count via cache_write_tokens."""
    msg = from_openai_response(
        {
            "id": "chatcmpl-cache-miss",
            "model": "anthropic/claude-opus-4.7",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10339,
                "completion_tokens": 60,
                "prompt_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 10318,
                },
            },
        }
    )
    assert msg.usage.cache_read_input_tokens == 0
    assert msg.usage.cache_creation_input_tokens == 10318


def test_response_usage_falls_back_to_flat_cached_tokens() -> None:
    """Some upstreams (older OpenRouter, certain models) still emit
    ``cached_tokens`` at the top of the usage block. We accept that
    shape as a fallback. cache_creation defaults to 0 in that case —
    the flat shape never carried a write counter."""
    msg = from_openai_response(
        {
            "id": "x",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "cached_tokens": 80,
            },
        }
    )
    assert msg.usage.cache_read_input_tokens == 80
    assert msg.usage.cache_creation_input_tokens == 0


def test_response_usage_zero_when_no_cache_fields() -> None:
    """No cache fields anywhere → both counters report zero (the
    pre-caching baseline). Verifies the change is backward-compatible
    with response bodies from non-caching models."""
    msg = from_openai_response(
        {
            "id": "x",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        }
    )
    assert msg.usage.cache_read_input_tokens == 0
    assert msg.usage.cache_creation_input_tokens == 0


def test_response_handles_malformed_tool_arguments_gracefully() -> None:
    """A non-JSON ``arguments`` string mustn't take the whole turn down."""
    msg = from_openai_response(
        {
            "id": "x",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "f",
                                    "arguments": "not-json",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    assert msg.content[0].input == {}


def test_response_strips_cite_markup_from_text() -> None:
    """OpenRouter's web plugin wraps cited claims in ``<cite index="...">…
    </cite>`` markup. The tags are stripped from response text so they don't
    leak into chat replies; the cited text (and any real URL) survives."""
    msg = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            'Mizuho <cite index="1-2">raised its target to '
                            "$289</cite>. Source: https://example.com/vlo"
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )
    assert msg.content[0].type == "text"
    assert "<cite" not in msg.content[0].text
    assert "</cite>" not in msg.content[0].text
    # Cited claim text and the real URL are preserved.
    assert "raised its target to $289" in msg.content[0].text
    assert "https://example.com/vlo" in msg.content[0].text


def test_response_strips_cite_markup_from_nested_tool_arguments() -> None:
    """Research findings arrive inside ``emit_research_findings`` tool-call
    arguments; the cite markup must be stripped from nested string fields too,
    or it surfaces verbatim in finding summaries / artifacts."""
    args = {
        "findings": [
            {
                "title": "WTI-Brent spread blows out",
                "summary": '<cite index="3-14,3-15">Spread widened to $8/bbl</cite>.',
                "relevant_urls": ["https://example.com/crude"],
            }
        ]
    }
    msg = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "emit_research_findings",
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    finding = msg.content[0].input["findings"][0]
    assert finding["summary"] == "Spread widened to $8/bbl."
    # Real source URL is untouched by the strip.
    assert finding["relevant_urls"] == ["https://example.com/crude"]


def test_response_strips_orphan_closing_cite_tag() -> None:
    """A fragment carrying only a closing ``</cite>`` (no matching open) must
    still be stripped — the fast-path guard must not skip it."""
    msg = from_openai_response(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done</cite> ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    assert msg.content[0].text == "done ok"


def test_response_drops_truncated_open_cite_tag() -> None:
    """A response truncated mid-tag (e.g. stop_reason=length) must not leak the
    unterminated ``<cite…`` fragment."""
    msg = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": 'Price target <cite index="1',
                    },
                    "finish_reason": "length",
                }
            ],
        }
    )
    assert "<cite" not in msg.content[0].text
    assert msg.content[0].text == "Price target "


# --------------------------------------------------------------------------
# StreamAccumulator
# --------------------------------------------------------------------------


def test_stream_accumulator_emits_text_deltas_in_anthropic_shape() -> None:
    acc = StreamAccumulator()
    events: list = []
    for chunk in [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo!"}}]},
        {"choices": [{"finish_reason": "stop"}]},
    ]:
        events.extend(acc.feed(chunk))

    # One content_block_start (for the text block) + two content_block_deltas.
    assert events[0].type == "content_block_start"
    assert events[0].content_block.type == "text"
    assert events[1].type == "content_block_delta"
    assert events[1].delta.type == "text_delta"
    assert events[1].delta.text == "Hel"
    assert events[2].delta.text == "lo!"

    final = acc.finalize()
    assert final.content[0].type == "text"
    assert final.content[0].text == "Hello!"
    assert final.stop_reason == "end_turn"


def test_stream_accumulator_assembles_fragmented_tool_call_arguments() -> None:
    acc = StreamAccumulator()
    chunks = [
        # First fragment names the function and begins the args.
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_77",
                                "function": {
                                    "name": "consult_specialist",
                                    "arguments": '{"specialist"',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        # Second fragment adds more args (no id/name re-sent).
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": ': "cfo"}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"finish_reason": "tool_calls"}]},
    ]
    for c in chunks:
        list(acc.feed(c))  # text events for tool_use chunks are empty

    final = acc.finalize()
    tool_blocks = [b for b in final.content if b.type == "tool_use"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].id == "call_77"
    assert tool_blocks[0].name == "consult_specialist"
    assert tool_blocks[0].input == {"specialist": "cfo"}
    assert final.stop_reason == "tool_use"


def test_stream_accumulator_handles_text_then_tool_use_interleaved() -> None:
    """A non-trivial assistant turn streams text then makes a tool call.
    Both must end up in the final message in the correct order."""
    acc = StreamAccumulator()
    chunks = [
        {"choices": [{"delta": {"content": "Thinking..."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {
                                    "name": "consult_specialist",
                                    "arguments": '{"specialist":"cfo"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"finish_reason": "tool_calls"}]},
    ]
    for c in chunks:
        list(acc.feed(c))
    final = acc.finalize()
    assert final.content[0].type == "text"
    assert final.content[0].text == "Thinking..."
    assert final.content[1].type == "tool_use"


def test_stream_accumulator_finalize_usage_extracted_from_chunks() -> None:
    acc = StreamAccumulator()
    list(acc.feed({"choices": [{"delta": {"content": "x"}}]}))
    list(
        acc.feed(
            {
                "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 1},
            }
        )
    )
    final = acc.finalize()
    assert final.usage.input_tokens == 42
    assert final.usage.output_tokens == 1


def test_stream_accumulator_finalize_extracts_cache_tokens_from_nested_usage() -> None:
    """Streaming path mirrors the non-streaming response parser for cache
    token counts — without this, the audit row from streamed turns
    (which is most turns) would always show 0/0 even when caching works."""
    acc = StreamAccumulator()
    list(acc.feed({"choices": [{"delta": {"content": "x"}}]}))
    list(
        acc.feed(
            {
                "choices": [{"finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10339,
                    "completion_tokens": 60,
                    "prompt_tokens_details": {
                        "cached_tokens": 10318,
                        "cache_write_tokens": 0,
                    },
                },
            }
        )
    )
    final = acc.finalize()
    assert final.usage.cache_read_input_tokens == 10318
    assert final.usage.cache_creation_input_tokens == 0


def test_stream_accumulator_finalize_surfaces_cost() -> None:
    """Streamed turns (most turns) carry cost in the final usage chunk; it must
    reach Message.usage.cost so the cache_event row records the charge."""
    acc = StreamAccumulator()
    list(acc.feed({"choices": [{"delta": {"content": "x"}}]}))
    list(
        acc.feed(
            {
                "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 1, "cost": 0.009},
            }
        )
    )
    final = acc.finalize()
    assert final.usage.cost == 0.009


def test_stream_accumulator_strips_cite_markup_from_text() -> None:
    """Streaming path mirrors the non-streaming response: cite markup is
    stripped from the finalized text block (cite tags can arrive split across
    deltas, so we assert on the assembled result)."""
    acc = StreamAccumulator()
    for chunk in [
        {"choices": [{"delta": {"content": 'A <cite index="1">'}}]},
        {"choices": [{"delta": {"content": "real claim</cite> here."}}]},
        {"choices": [{"finish_reason": "stop"}]},
    ]:
        list(acc.feed(chunk))
    final = acc.finalize()
    assert final.content[0].text == "A real claim here."


def test_stream_accumulator_strips_cite_from_emitted_deltas_split_across_chunks() -> None:
    """The chat loop builds the *visible* reply from the streamed
    ``content_block_delta`` events, not from the finalized message — so the
    deltas themselves must be cite-free, even when a tag is split across SSE
    chunks. Asserting on ``finalize()`` alone would miss a live-stream leak."""
    acc = StreamAccumulator()
    # The <cite ...> open tag is deliberately fragmented across three chunks.
    chunks = [
        {"choices": [{"delta": {"content": "Target "}}]},
        {"choices": [{"delta": {"content": "<cit"}}]},
        {"choices": [{"delta": {"content": 'e index="1-2">'}}]},
        {"choices": [{"delta": {"content": "raised to $289"}}]},
        {"choices": [{"delta": {"content": "</cite>"}}]},
        {"choices": [{"delta": {"content": " today."}}]},
        {"choices": [{"finish_reason": "stop"}]},
    ]
    streamed = ""
    for chunk in chunks:
        for ev in acc.feed(chunk):
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                streamed += ev.delta.text
    # No cite markup ever reaches the consumer, and no visible text is dropped.
    assert "<cite" not in streamed
    assert "</cite>" not in streamed
    assert streamed == "Target raised to $289 today."
    # Finalized message agrees with the live stream.
    assert acc.finalize().content[0].text == "Target raised to $289 today."


def test_stream_accumulator_does_not_leak_truncated_open_cite_tag() -> None:
    """If the stream ends mid-open-tag (truncation), the withheld fragment is
    dropped at the finish_reason flush — never emitted as a raw partial tag.
    The streamed deltas and the finalized text agree."""
    acc = StreamAccumulator()
    streamed = ""
    for chunk in [
        {"choices": [{"delta": {"content": "Price target "}}]},
        {"choices": [{"delta": {"content": '<cite index="1'}}]},
        {"choices": [{"finish_reason": "length"}]},
    ]:
        for ev in acc.feed(chunk):
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                streamed += ev.delta.text
    assert "<cite" not in streamed
    assert streamed == "Price target "
    assert acc.finalize().content[0].text == "Price target "


def test_stream_accumulator_emits_legitimate_trailing_text_with_angle_bracket() -> None:
    """A bare ``<`` that is NOT a cite prefix (e.g. ``5 < 10``) must not be
    withheld forever — it flushes as ordinary text."""
    acc = StreamAccumulator()
    streamed = ""
    for chunk in [
        {"choices": [{"delta": {"content": "5 <"}}]},
        {"choices": [{"delta": {"content": " 10 is true."}}]},
        {"choices": [{"finish_reason": "stop"}]},
    ]:
        for ev in acc.feed(chunk):
            if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                streamed += ev.delta.text
    assert streamed == "5 < 10 is true."
