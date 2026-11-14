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

    assert asyncio.run(go()) == [0]  # it streamed nothing
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
        refused.append(len("You're"))

    executive.stream_chat = stream_chat  # type: ignore[method-assign]
    assert await executive.chat("why?", Session()) == "I can't help with that one."
    assert executive_module._REFUSED.get() is None


def test_a_streamed_refusal_ends_on_the_plain_line() -> None:
    # The web chat streams the loop with nothing bound to _REFUSED.
    provider = _ScriptedProvider([_FinalMsg([_TextBlock("You're")], stop_reason="refusal")])

    async def go() -> list[Any]:
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            return [
                item async for item in executive_module.Executive()._stream_agent_loop(
                    system_blocks=[], messages=[{"role": "user", "content": "why?"}], model="claude-test",
                )
                if isinstance(item, str)
            ]

    assert asyncio.run(go())[-1] == executive_module.REFUSED_REPLY


async def test_what_earlier_rounds_said_stays_before_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tool round said what it did; the next round was refused after "You're".
    from openexecutive.alerts import card_refs

    monkeypatch.setattr(card_refs, "name_cards_in_reply", lambda text, *a, **k: text)
    executive = executive_module.Executive.__new__(executive_module.Executive)

    async def stream_chat(**_kw: Any) -> AsyncIterator[Any]:
        yield "I drafted the email to Luis; it's on a card. "
        yield "You're"
        refused = executive_module._REFUSED.get()
        assert refused is not None
        refused.append(len("You're"))

    executive.stream_chat = stream_chat  # type: ignore[method-assign]
    assert await executive.chat("draft it, then do the worst thing", Session()) == (
        "I drafted the email to Luis; it's on a card.\n\nI can't help with that one."
    )


def test_a_refused_committee_draft_is_never_reviewed() -> None:
    from openexecutive.memory.company_profile import CompanyProfile

    async def draft_loop(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        yield "You're"
        refused = executive_module._REFUSED.get()
        assert refused is not None
        refused.append(len("You're"))

    async def review(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a refused draft went to review")

    async def go() -> list[str]:
        session = Session(session_id="t", company_profile=CompanyProfile())
        return [
            item async for item in executive_module.Executive().stream_chat_with_committee(
                user_message="do the worst thing", session=session,
            )
            if isinstance(item, str)
        ]

    with (
        patch.object(executive_module.Executive, "_stream_agent_loop", new=draft_loop),
        patch("openexecutive.orchestrator.committee.Committee.review", new=review),
        patch("openexecutive.audit.log_event", lambda *a, **k: None),
    ):
        said = asyncio.run(go())
    assert said[-1] == executive_module.REFUSED_REPLY and "You're" not in "".join(said)
