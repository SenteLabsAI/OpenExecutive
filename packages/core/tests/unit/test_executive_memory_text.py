"""``memory_text`` decides what peer memory records as the person's words.

A turn's ``user_message`` often carries text the person did not write: an
inbound email's framing, headers and quoted chain, or a briefing card's
body. Recorded under the person's peer, Honcho derives facts about the
person from it ("<person> is associated with <the Executive's address>").
Every entry point must hand ``memory_text`` — not the prompt — to the person
sync, the per-department sync, episodic extraction and the open-loop pass,
and fall back to ``user_message`` when it is not given.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

PROMPT = "You have an inbound email (message_id=m1).\n\nTo: exec@example.com\n\nHere you go."
MEMORY = "Subject: Q3 appraisal\n\nHere you go."


@pytest.fixture(autouse=True)
def _no_audit_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this module's audit rows out of the default episodic DB (the
    audit-log pollution trap in CLAUDE.md)."""
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda *a, **kw: None, raising=False
    )
    # executive.py binds its own name at import time, which the patch above
    # does not reach.
    monkeypatch.setattr(
        "openexecutive.orchestrator.executive.audit_log", lambda *a, **kw: None
    )


def _make_stream_cm(final_msg: object) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)

    async def _aiter():
        # One streamed text delta, so the committee path has a non-empty
        # draft to review and reaches its post-turn sync.
        yield SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="done"),
        )

    cm.__aiter__ = lambda self=cm: _aiter()
    cm.get_final_message = AsyncMock(return_value=final_msg)
    return cm


def _provider() -> SimpleNamespace:
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn"
    )
    return SimpleNamespace(
        messages_stream=lambda **_kw: _make_stream_cm(final),
        messages_create=AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
            )
        ),
    )


def _drive(entry: str, **kwargs: Any) -> dict[str, list[str]]:
    """Run one turn through ``entry`` and return the text each post-turn pass
    received: the person sync, the department sync, episodic extraction and
    the open-loop pass."""
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    seen: dict[str, list[str]] = {"person": [], "dept": [], "extract": [], "loops": []}

    def _sync_turn(user_message: str, *_a: Any, **_k: Any) -> None:
        seen["person"].append(user_message)

    def _dept_sync(_consulted: Any, user_message: str, *_a: Any, **_k: Any) -> None:
        seen["dept"].append(user_message)

    def _extract(user_message: str, *_a: Any, **_k: Any) -> None:
        seen["extract"].append(user_message)

    def _loops(user_message: str, *_a: Any, **_k: Any) -> None:
        seen["loops"].append(user_message)

    exec_ = Executive()

    async def _run() -> None:
        session = Session(session_id=f"t-memtext-{entry}")
        if entry == "chat":
            await exec_.chat(PROMPT, session, person_id=42, peer_memory_context="", **kwargs)
            return
        stream = getattr(exec_, entry)(
            PROMPT, session, person_id=42, peer_memory_context="", **kwargs
        )
        async for _ in stream:
            pass

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_provider()),
        patch("openexecutive.memory.honcho_client.sync_turn", new=_sync_turn),
        patch(
            "openexecutive.orchestrator.executive._sync_consulted_departments_to_honcho",
            new=_dept_sync,
        ),
        patch("openexecutive.memory.episodic.schedule_extraction", new=_extract),
        patch("openexecutive.attunement.open_loops.schedule_open_loop_pass", new=_loops),
    ):
        asyncio.run(_run())
    return seen


@pytest.mark.parametrize("entry", ["stream_chat", "stream_chat_with_committee", "chat"])
def test_memory_text_is_what_every_post_turn_pass_reads(entry: str) -> None:
    """Extraction and open loops only accept an item with a verbatim quote
    from the text they get; the prompt would let a quoted Executive email
    satisfy that quote."""
    seen = _drive(entry, memory_text=MEMORY)
    assert seen == {"person": [MEMORY], "dept": [MEMORY], "extract": [MEMORY], "loops": [MEMORY]}


@pytest.mark.parametrize("entry", ["stream_chat", "stream_chat_with_committee", "chat"])
def test_without_memory_text_the_message_is_recorded(entry: str) -> None:
    seen = _drive(entry)
    assert seen == {"person": [PROMPT], "dept": [PROMPT], "extract": [PROMPT], "loops": [PROMPT]}


def test_empty_memory_text_is_honoured_not_replaced_by_the_prompt() -> None:
    """An email with no subject, body or attachment yields "" — that must
    record nothing for the person, not fall back to the framed prompt, and
    gives extraction nothing to quote from (should_extract skips it)."""
    seen = _drive("stream_chat", memory_text="")
    assert seen["person"] == [""]
    assert seen["extract"] == []
    assert seen["loops"] == [""]


def test_briefing_approve_line_passes_the_quote_gate_with_a_question_headline() -> None:
    """The UI's line (packages/ui/src/lib/briefing-memory.ts) ends the action
    sentence before the headline, so a "?" in the headline cannot void the
    approval as the principal's decision."""
    from openexecutive.memory.episodic import _is_valid_user_commitment

    line = 'I approve this proposal.\n"Should we renew Acme at 3%?"'
    assert _is_valid_user_commitment("I approve this proposal", line)
    # The shape it replaced put the headline first inside the sentence.
    assert not _is_valid_user_commitment(
        "I approve the proposal", 'I approve the proposal "Should we renew Acme at 3%?".'
    )
