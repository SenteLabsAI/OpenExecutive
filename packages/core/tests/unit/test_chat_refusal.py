"""A turn the model refuses: the loop stops, and Executive.chat sends a plain line."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator import executive as executive_module
from openexecutive.orchestrator.session import Session
from tests.unit.test_executive_form_patch import _FinalMsg, _ScriptedProvider, _TextBlock


def test_the_loop_marks_a_refused_round_and_stops() -> None:
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("You're")], stop_reason="refusal")])

    async def go() -> list[int]:
        refused: list[int] = []
        token = executive_module._REFUSED.set(refused)
        try:
            with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
                async for _ in executive_module.Executive()._stream_agent_loop(
                    system_blocks=[], messages=[{"role": "user", "content": "why?"}], model="claude-test",
                ):
                    pass
        finally:
            executive_module._REFUSED.reset(token)
        return refused

    assert asyncio.run(go()) == [1]
    # No claim check or retry round after a refusal.
    assert len(provider.calls) == 1


async def test_a_refused_turn_sends_a_plain_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.alerts import card_refs

    monkeypatch.setattr(card_refs, "name_cards_in_reply", lambda text, *a, **k: text)
    executive = executive_module.Executive.__new__(executive_module.Executive)

    async def stream_chat(**_kw: Any) -> AsyncIterator[Any]:
        yield "You're"
        refused = executive_module._REFUSED.get()
        assert refused is not None
        refused.append(1)

    executive.stream_chat = stream_chat  # type: ignore[method-assign]
    assert await executive.chat("why?", Session()) == "I can't help with that one."
    assert executive_module._REFUSED.get() is None
