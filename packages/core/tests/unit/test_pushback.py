"""The model for one answer after pushback (orchestrator.pushback)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from openexecutive.memory.episodic import initialize_db  # noqa: E402
from openexecutive.memory.session_store import (  # noqa: E402
    create_session,
    save_message,
    set_message_feedback,
)
from openexecutive.orchestrator import pushback  # noqa: E402

SID = "s-pushback"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    initialize_db(path)
    create_session(SID, "Taxes", datetime.now(UTC).isoformat(), db_path=path)
    # No claim-check rows unless a test adds them.
    events: list[Any] = []
    logger = SimpleNamespace(query=lambda **_: list(events))
    monkeypatch.setattr("openexecutive.audit.logger.get_audit_logger", lambda: logger)
    monkeypatch.setattr(pushback, "_test_events", events, raising=False)
    return path


def _exchange(db: Path, asked: str = "Draft the email to Kyle", reply: str = "Done.") -> int:
    save_message(SID, "user", asked, db_path=db)
    return save_message(SID, "assistant", reply, db_path=db)


@pytest.mark.parametrize(
    "text",
    [
        "That's not what I asked",
        "that’s not what I asked for",
        "Why can't you attach them?",
        "You should be able to make these adjustments",
        "try again",
        "It didn't work",
        "Your safety rules basically make this system useless",
        "you still haven't sent it",
        "No, that's wrong",
    ],
)
def test_said_wrong_matches_pushback(text: str) -> None:
    assert pushback.said_wrong(text)


@pytest.mark.parametrize(
    "text",
    [
        "What went wrong with the St. Albans deal?",
        "Go",
        "Draft a reply to Andrew",
        "Can you summarize the tax bills?",
        "Why did the price drop?",
    ],
)
def test_said_wrong_leaves_ordinary_messages(text: str) -> None:
    assert not pushback.said_wrong(text)


def test_asked_again_needs_the_same_longer_ask() -> None:
    assert pushback.asked_again(
        "Search my email for St Albans, then open foreactlabs.com",
        "Search my email for St Albans then open foreactlabs.com",
    )
    assert not pushback.asked_again("go", "go")
    assert not pushback.asked_again(
        "Search my email for Pine View", "Draft the property tax email to Kyle"
    )


def test_last_user_text_skips_tool_results_and_voice_prefix() -> None:
    history = [
        {"role": "user", "content": "[Voice note, transcribed] Rename the PDFs please"},
        {"role": "assistant", "content": [{"type": "text", "text": "Say go."}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
    ]
    assert pushback.last_user_text(history) == "Rename the PDFs please"


def test_typed_thumbs_down_is_pushback(db: Path) -> None:
    assert pushback.reason_for(SID, "👎", [], db_path=db) == "thumbs_down"
    assert pushback.reason_for(SID, "Thumbs down.", [], db_path=db) == "thumbs_down"


def test_thumbs_down_on_the_last_reply(db: Path) -> None:
    reply_id = _exchange(db)
    assert pushback.reason_for(SID, "Ok, and the scans?", [], db_path=db) is None
    set_message_feedback(SID, reply_id, "down", db_path=db)
    assert pushback.reason_for(SID, "Ok, and the scans?", [], db_path=db) == "thumbs_down"


def test_thumbs_down_on_an_older_reply_doesnt_count(db: Path) -> None:
    old = _exchange(db)
    set_message_feedback(SID, old, "down", db_path=db)
    _exchange(db, "Thanks", "Anytime.")
    assert pushback.reason_for(SID, "What's on tap tomorrow?", [], db_path=db) is None


def test_asked_again_from_history(db: Path) -> None:
    history = [
        {"role": "user", "content": "Rename all 11 PDFs as you proposed"},
        {"role": "assistant", "content": "Say go."},
    ]
    assert (
        pushback.reason_for(SID, "Rename all 11 PDFs as you proposed.", history, db_path=db)
        == "asked_again"
    )


def test_claim_check_on_the_last_reply(db: Path) -> None:
    # The check runs while the turn is under way: after the message, before
    # the reply is saved.
    save_message(SID, "user", "Clear the Aprio items", db_path=db)
    pushback._test_events.append(SimpleNamespace(ts=datetime.now(UTC).isoformat()))
    save_message(SID, "assistant", "Closed.", db_path=db)
    assert pushback.reason_for(SID, "Thanks", [], db_path=db) == "claimed"


def test_claim_check_before_the_last_message_doesnt_count(db: Path) -> None:
    pushback._test_events.append(
        SimpleNamespace(ts=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    )
    _exchange(db)
    assert pushback.reason_for(SID, "Thanks", [], db_path=db) is None


def test_unreadable_chat_is_no_pushback(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "x.db"
    assert pushback.reason_for(SID, "Ok", [], db_path=missing) is None


def test_pushback_model_pick_order() -> None:
    assert pushback.pushback_model(None, "") is None
    assert pushback.pushback_model(None, "claude-opus-5-5") == "claude-opus-5-5"
    assert pushback.pushback_model("openai/gpt-6-astra", "claude-opus-5-5") == "openai/gpt-6-astra"
    assert pushback.pushback_model(pushback.PUSHBACK_OFF, "claude-opus-5-5") is None


def test_for_turn(db: Path) -> None:
    kwargs: dict[str, Any] = {
        "session_id": SID,
        "history": [],
        "normal_model": "claude-sonnet-5-5",
        "override_pick": None,
        "db_path": db,
    }
    hit = pushback.for_turn(text="That's not what I asked", default="claude-opus-5-5", **kwargs)
    assert hit == pushback.Pushback(model="claude-opus-5-5", reason="said_wrong")
    # Off, the same model, or no pushback: the normal model.
    assert pushback.for_turn(text="That's not what I asked", default="", **kwargs) is None
    assert pushback.for_turn(text="That's not what I asked", default="claude-sonnet-5-5", **kwargs) is None
    assert pushback.for_turn(text="Thanks", default="claude-opus-5-5", **kwargs) is None


def test_executive_picks_the_pushback_model(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import executive
    from openexecutive.orchestrator.session import Session

    monkeypatch.setattr(
        "openexecutive.agents.overrides.get_override",
        lambda _agent: SimpleNamespace(pushback_model=None),
    )
    session = Session(session_id=SID)
    settings = SimpleNamespace(pushback_model="claude-opus-5-5")
    monkeypatch.setattr(pushback, "reason_for", lambda *a, **k: "said_wrong")
    hit = executive._pushback_for_turn(settings, session, "Why can't you?", "claude-sonnet-5-5")
    assert hit is not None and hit.model == "claude-opus-5-5"

    monkeypatch.setattr(
        "openexecutive.agents.overrides.get_override",
        lambda _agent: SimpleNamespace(pushback_model=pushback.PUSHBACK_OFF),
    )
    assert executive._pushback_for_turn(settings, session, "Why can't you?", "claude-sonnet-5-5") is None
