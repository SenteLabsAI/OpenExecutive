"""The <briefing> open-alert digest is injected into the user turn (and only
when non-empty), never into a cached system block — mirroring <past_decisions>.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")


def _user_turn_texts(messages: list[dict]) -> list[str]:
    """Collect the text blocks of the final (user) turn."""
    user = messages[-1]
    assert user["role"] == "user"
    return [p["text"] for p in user["content"] if p.get("type") == "text"]


def test_briefing_block_present_when_provided() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    exec_ = Executive()
    session = Session(session_id="t-brief-1")
    messages = exec_._build_messages(
        session,
        "hi",
        briefing_context="[7] (action) Gulf Coast Port Cyberattack — blocked",
    )
    texts = _user_turn_texts(messages)
    assert any(
        "<briefing>\n[7] (action) Gulf Coast Port Cyberattack — blocked\n</briefing>" in t
        for t in texts
    ), texts


def test_briefing_block_absent_when_empty() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    exec_ = Executive()
    session = Session(session_id="t-brief-2")
    messages = exec_._build_messages(session, "hi", briefing_context="")
    texts = _user_turn_texts(messages)
    assert not any("<briefing>" in t for t in texts), texts


# --------------------------------------------------------------------- #
# <channel> — same placement contract (#136)
# --------------------------------------------------------------------- #


def test_channel_block_present_when_provided() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    exec_ = Executive()
    session = Session(session_id="s1")
    messages = exec_._build_messages(
        session,
        "hi",
        channel_context_block="You are talking to this person over Slack.",
    )
    texts = _user_turn_texts(messages)
    assert (
        "<channel>\nYou are talking to this person over Slack.\n</channel>" in texts
    ), texts


def test_channel_block_absent_when_empty() -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    exec_ = Executive()
    session = Session(session_id="s1")
    messages = exec_._build_messages(session, "hi", channel_context_block="")
    texts = _user_turn_texts(messages)
    assert not any("<channel>" in t for t in texts), texts


def test_channel_block_never_lands_in_a_cached_system_block() -> None:
    """Per-request content in a cache_control block invalidates the prompt
    prefix on every turn — a ~10x cost regression (see CLAUDE.md)."""
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    exec_ = Executive()
    session = Session(session_id="s1")
    marker = "CHANNEL-MARKER-DO-NOT-CACHE"
    messages = exec_._build_messages(session, "hi", channel_context_block=marker)

    # It rides in the user turn...
    assert any(marker in t for t in _user_turn_texts(messages))
    # ...and in no cache-controlled block anywhere in the request.
    for msg in messages:
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("cache_control"):
                assert marker not in str(part.get("text", ""))
