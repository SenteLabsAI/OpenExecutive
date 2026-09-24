"""Shared fakes for driving `Executive._stream_agent_loop` without a provider.

The loop only needs a provider exposing `messages_stream(**kwargs)` that
returns an async context manager yielding content-block deltas and, at the end,
a final message with `.content` and `.stop_reason`. These are the smallest
objects satisfying that contract, so a test can script a turn as a list of
final messages and assert on what the loop yields.

Note the stream emits NO text deltas: content rides on the final message.
That is enough for tool-round behaviour (which is what these fakes exist for)
but means the loop yields no assistant text, so assert on `ScriptedProvider.
calls` rather than on streamed prose when you need to prove the loop advanced.

Extracted from the copy in test_executive_form_patch.py. Several older modules
still carry their own near-identical copies; they can migrate here piecemeal.
"""
from __future__ import annotations

from typing import Any


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    type = "tool_use"

    def __init__(self, id_: str, name: str, input_: dict[str, Any]) -> None:
        self.id = id_
        self.name = name
        self.input = input_


class FinalMsg:
    usage = None  # _emit_cache_event no-ops on usage=None

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class FakeStream:
    def __init__(self, final_msg: FinalMsg) -> None:
        self._final_msg = final_msg

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration  # no text deltas; content rides on final_msg

    async def get_final_message(self) -> FinalMsg:
        return self._final_msg


class ScriptedProvider:
    """Returns one scripted final message per `messages_stream` call.

    `calls` records the kwargs of each call, so a test can prove the loop made
    its follow-up request after feeding tool results back.
    """

    def __init__(self, final_msgs: list[FinalMsg]) -> None:
        self._final_msgs = list(final_msgs)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return FakeStream(self._final_msgs.pop(0))
