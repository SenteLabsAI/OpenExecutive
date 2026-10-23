"""A brief the model ran out of room for is never sent: not cut off, and not
as the quiet-day line. The 5.5 models think by default and the thinking
counts against max_tokens, so a 600-token cap came back empty or half."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.briefing.narrative import (
    BRIEF_MAX_TOKENS,
    BriefNotWritten,
    create_brief_text,
    synthesize_briefing_narrative,
)


class _Provider:
    def __init__(self, replies: list[tuple[str, str]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kw: Any) -> object:
        self.calls.append(kw)
        text, stop = self.replies.pop(0)
        content = [SimpleNamespace(type="thinking", thinking="")]
        if text:
            content.append(SimpleNamespace(type="text", text=text))
        return SimpleNamespace(content=content, stop_reason=stop)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    def make(replies: list[tuple[str, str]]) -> _Provider:
        p = _Provider(replies)
        monkeypatch.setattr("openexecutive.providers.get_provider", lambda _m: p)
        monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
        return p
    return make


@pytest.mark.asyncio
async def test_a_brief_gets_room_to_think(provider: Any) -> None:
    p = provider([("**Top call:** the offer.", "end_turn")])
    text = await create_brief_text("m", system="s", messages=[], actor="t")
    assert text == "**Top call:** the offer."
    assert p.calls[0]["max_tokens"] == BRIEF_MAX_TOKENS >= 4000


@pytest.mark.asyncio
async def test_a_cut_off_brief_is_asked_for_again_with_more_room(provider: Any) -> None:
    p = provider([("**Top call:** the off", "max_tokens"), ("**Top call:** the offer.", "end_turn")])
    text = await create_brief_text("m", system="s", messages=[], actor="t")
    assert text == "**Top call:** the offer."
    assert [c["max_tokens"] for c in p.calls] == [BRIEF_MAX_TOKENS, 2 * BRIEF_MAX_TOKENS]


@pytest.mark.asyncio
async def test_a_brief_never_written_raises_instead_of_going_quiet(provider: Any) -> None:
    provider([("", "max_tokens"), ("", "max_tokens")])
    with pytest.raises(BriefNotWritten):
        await synthesize_briefing_narrative(
            today_data={}, activity=[], period_label="p", rendered_context="ctx",
        )


@pytest.mark.asyncio
async def test_an_empty_end_turn_is_not_a_brief_either(provider: Any) -> None:
    provider([("", "end_turn"), ("", "end_turn")])
    with pytest.raises(BriefNotWritten):
        await create_brief_text("m", system="s", messages=[], actor="t")
