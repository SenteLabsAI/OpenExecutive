"""Do what it says (orchestrator.claim_check): a reply that claims a change no
tool call made gets one more round."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.orchestrator import claim_check
from openexecutive.orchestrator.executive import Executive
from tests.unit._agent_loop_fakes import FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock


@pytest.fixture(autouse=True)
def _claim_check_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "true")
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)


def _run(provider: ScriptedProvider, message: str = "Remove it from the brief") -> list[Any]:
    async def _go() -> list[Any]:
        items: list[Any] = []
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for item in Executive()._stream_agent_loop(
                system_blocks=[],
                messages=[{"role": "user", "content": message}],
                model="claude-test",
            ):
                items.append(item)
        return items

    return asyncio.run(_go())


def _verdict(payload: dict[str, Any]):
    calls: list[str] = []

    async def fake(model: str, turn: str) -> dict[str, Any]:
        calls.append(turn)
        return payload

    return fake, calls


# --- the pure parts ----------------------------------------------------------


def test_word_check_skips_replies_that_claim_nothing() -> None:
    assert not claim_check.may_claim("Revenue was up 4% on last quarter.")
    assert not claim_check.may_claim("   ")
    assert claim_check.may_claim("Done.")
    assert claim_check.may_claim("I'll treat the draft as closed.")
    assert claim_check.may_claim("Removed it from the brief")


def test_claim_must_be_in_the_reply_word_for_word() -> None:
    reply = "Done — I’ll treat the draft as closed."
    assert claim_check.accept_claim({"unbacked": True, "claim": "I'll treat the draft as closed"}, reply)
    assert claim_check.accept_claim({"unbacked": True, "claim": "I sent the email"}, reply) is None
    assert claim_check.accept_claim({"unbacked": False, "claim": "Done"}, reply) is None
    assert claim_check.accept_claim({"unbacked": True, "claim": ""}, reply) is None
    assert claim_check.accept_claim({"unbacked": True, "claim": "x" * 300}, "x" * 300) is None


def test_last_person_message_skips_tool_result_turns() -> None:
    messages = [
        {"role": "user", "content": "I sent it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    ]
    assert claim_check.last_person_message(messages) == "I sent it"


def test_check_is_off_without_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "false")
    fake, calls = _verdict({"unbacked": True, "claim": "Done"})
    with patch.object(claim_check, "_call_model", fake):
        assert asyncio.run(claim_check.unbacked_claim("x", "Done", [])) is None
    assert calls == []


def test_a_failed_check_lets_the_reply_through() -> None:
    async def boom(model: str, turn: str) -> dict[str, Any]:
        raise RuntimeError("provider down")

    with patch.object(claim_check, "_call_model", boom):
        assert asyncio.run(claim_check.unbacked_claim("x", "Done", [])) is None


def test_the_check_sees_the_turns_calls() -> None:
    fake, calls = _verdict({"unbacked": False, "claim": ""})
    with patch.object(claim_check, "_call_model", fake):
        asyncio.run(claim_check.unbacked_claim(
            "close it", "Closed it.", [("close_open_loop", '{"status": "closed"}')],
        ))
    assert "close_open_loop" in calls[0]
    assert "<assistant_reply>\nClosed it." in calls[0]


# --- in the agent loop -------------------------------------------------------


def test_unbacked_claim_gets_one_more_round_with_the_nudge() -> None:
    provider = ScriptedProvider([
        FinalMsg([TextBlock("Done.")], "end_turn"),
        FinalMsg([TextBlock("I can't remove items from the brief yet.")], "end_turn"),
    ])
    fake, calls = _verdict({"unbacked": True, "claim": "Done"})
    with patch.object(claim_check, "_call_model", fake):
        items = _run(provider)

    assert len(provider.calls) == 2
    second = provider.calls[1]["messages"]
    assert second[-2] == {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
    nudge = second[-1]["content"][0]["text"]
    assert "“Done”" in nudge and "no tool call" in nudge
    # The break between the two replies comes from the next round's text
    # (test_reply_rounds.py); the retry itself streams nothing of its own.
    assert "\n\n" not in items
    # Checked once a turn: the second reply ends it without another check.
    assert len(calls) == 1


def test_backed_or_plain_reply_ends_the_turn() -> None:
    provider = ScriptedProvider([FinalMsg([TextBlock("Done.")], "end_turn")])
    fake, calls = _verdict({"unbacked": False, "claim": ""})
    with patch.object(claim_check, "_call_model", fake):
        _run(provider)
    assert len(provider.calls) == 1
    assert len(calls) == 1


def test_reply_without_claim_words_makes_no_check_call() -> None:
    provider = ScriptedProvider([FinalMsg([TextBlock("Revenue was up 4%.")], "end_turn")])
    fake, calls = _verdict({"unbacked": True, "claim": "Revenue"})
    with patch.object(claim_check, "_call_model", fake):
        _run(provider, "How did revenue do?")
    assert calls == []
    assert len(provider.calls) == 1


def test_tool_calls_this_turn_reach_the_check() -> None:
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("t1", "list_open_loops", {})], "tool_use"),
        FinalMsg([TextBlock("Closed it.")], "end_turn"),
    ])
    fake, calls = _verdict({"unbacked": False, "claim": ""})

    async def handler(_input: dict[str, Any]) -> str:
        return '{"loops": []}'

    with (
        patch.object(claim_check, "_call_model", fake),
        patch.dict(
            "openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
            {"list_open_loops": handler},
        ),
    ):
        _run(provider, "I sent it")
    assert len(calls) == 1
    assert "- list_open_loops:" in calls[0]


def test_the_turns_calls_reach_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """``calls_out`` hands the turn's calls to the conversation review's record."""
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "false")
    provider = ScriptedProvider([
        FinalMsg([ToolUseBlock("t1", "list_open_loops", {})], "tool_use"),
        FinalMsg([TextBlock("Nothing is open.")], "end_turn"),
    ])

    async def handler(_input: dict[str, Any]) -> str:
        return '{"loops": []}'

    calls: list[tuple[str, str]] = []

    async def _go() -> None:
        with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
            async for _ in Executive()._stream_agent_loop(
                system_blocks=[], messages=[{"role": "user", "content": "anything open?"}],
                model="claude-test", calls_out=calls,
            ):
                pass

    with patch.dict("openexecutive.orchestrator.executive._ALL_SKILL_HANDLERS",
                    {"list_open_loops": handler}):
        asyncio.run(_go())
    assert calls == [("list_open_loops", '{"loops": []}')]


def test_the_check_sees_what_each_call_was_given() -> None:
    # A sheet write's result says only "updated range A9:B10"; the row it
    # moved and the formula it wrote are in what it was given.
    label = claim_check.call_label("call_tool", {
        "name": "google_workspace__modify_sheet_values",
        "arguments": {"range_name": "A9:B10", "values": [["Supplies", 600], ["Total", "=SUM(B2:B9)"]]},
    })
    assert label.startswith("google_workspace__modify_sheet_values ")
    assert "=SUM(B2:B9)" in label
    assert claim_check.call_label("list_open_loops", {}) == "list_open_loops"
    long = claim_check.call_label("note", {"text": "x" * 5000})
    assert len(long) < claim_check.CALL_INPUT_CHARS + 40
    rendered = claim_check.render_calls([(label, "Successfully updated range 'A9:B10'")])
    assert "=SUM(B2:B9)" in rendered
