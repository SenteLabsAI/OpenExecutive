"""``Executive.chat(committee_review=...)`` must pick the right stream method.

Non-HTTP callers (the channel adapters, the eval harness) use ``chat()``
rather than going through the SSE route. The flag has to be honored at this
level so a caller that opts in gets the revised response; the default must
stay ``False`` because the channel adapters (Slack, Discord, email) rely on
it — committee review is a per-request opt-in on ``/chat``.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.session import Session


def _session() -> Session:
    return Session(session_id="s", company_profile=CompanyProfile())


def test_chat_routes_to_stream_chat_when_committee_review_false() -> None:
    captured: dict[str, bool] = {"plain": False, "committee": False}

    async def _plain(self: Executive, **_kwargs: Any) -> AsyncIterator[str]:
        captured["plain"] = True
        yield "plain reply"

    async def _committee(self: Executive, **_kwargs: Any) -> AsyncIterator[str]:
        captured["committee"] = True
        yield "should not run"

    with (
        patch.object(Executive, "stream_chat", new=_plain),
        patch.object(Executive, "stream_chat_with_committee", new=_committee),
    ):
        result = asyncio.run(
            Executive().chat(user_message="hi", session=_session())
        )
    assert result == "plain reply"
    assert captured["plain"] is True
    assert captured["committee"] is False


def test_chat_routes_to_stream_chat_with_committee_when_flag_set() -> None:
    captured: dict[str, bool] = {"plain": False, "committee": False}

    async def _plain(self: Executive, **_kwargs: Any) -> AsyncIterator[str]:
        captured["plain"] = True
        yield "should not run"

    async def _committee(self: Executive, **_kwargs: Any) -> AsyncIterator[Any]:
        captured["committee"] = True
        # Mimic the real method emitting non-text dicts plus the revised text.
        yield {"type": "phase", "phase": "drafting"}
        yield "revised "
        yield "reply"

    with (
        patch.object(Executive, "stream_chat", new=_plain),
        patch.object(Executive, "stream_chat_with_committee", new=_committee),
    ):
        result = asyncio.run(
            Executive().chat(
                user_message="hi",
                session=_session(),
                committee_review=True,
            )
        )
    # Only string chunks (not dicts) make it into the accumulated result.
    assert result == "revised reply"
    assert captured["committee"] is True
    assert captured["plain"] is False
