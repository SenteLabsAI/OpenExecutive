"""Unit tests for the shared inbound hydration helper and its bot wiring.

``hydrate_user_message`` prepends the context of a recent outbound DM to an
incoming reply so oe knows what the person is replying about, then one-shot
consumes the linkage. It must never raise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.integrations import inbound_hydration
from openexecutive.memory import session_store
from openexecutive.memory.episodic import (
    initialize_db,
    insert_outbound_context,
)
from openexecutive.memory.session_store import create_session, save_message


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both the episodic helpers and the hydration helper's load_messages
    at an isolated DB. episodic functions read DB_PATH dynamically; session_store
    .load_messages binds its default at import, so inject the path explicitly."""
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr(
        inbound_hydration,
        "load_messages",
        lambda session_id: session_store.load_messages(session_id, db_path),
    )
    return db_path


def _seed_originating_session(db_path: Path, session_id: str) -> None:
    create_session(session_id, "principal chat", "2026-06-01T00:00:00+00:00", db_path=db_path)
    save_message(session_id, "user", "Alex skipped the deck to go eat pizza.", db_path=db_path)
    save_message(session_id, "assistant", "Got it — I'll follow up with Alex.", db_path=db_path)


def test_hydrate_injects_block_and_consumes(wired_db: Path) -> None:
    _seed_originating_session(wired_db, "discord:dm:principal-1")
    insert_outbound_context(
        channel="discord_dm",
        channel_ref="alex-123",
        outbound_text="Hey Alex — heard the deck slipped. What happened?",
        originating_session_id="discord:dm:principal-1",
        db_path=wired_db,
    )

    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="alex-123", user_message="oh that, sorry",
    )

    assert "<outbound_reply_context>" in out
    assert "Hey Alex" in out  # outbound text echoed
    assert "pizza" in out  # backstory excerpt pulled from originating session
    assert out.endswith("oh that, sorry")  # original message preserved at the end


def test_hydrate_email_channel(wired_db: Path) -> None:
    """The helper is channel-agnostic — pin that an `email` linkage hydrates
    and one-shot-consumes just like the DM channels (used by the email poller,
    keyed on the bare lowercased sender address)."""
    _seed_originating_session(wired_db, "webchat:principal-1")
    insert_outbound_context(
        channel="email",
        channel_ref="alex@example.com",
        outbound_text="Heard the deck slipped — what happened?",
        originating_session_id="webchat:principal-1",
        db_path=wired_db,
    )

    out = inbound_hydration.hydrate_user_message(
        channel="email", channel_ref="alex@example.com", user_message="sorry, pizza happened",
    )
    assert "<outbound_reply_context>" in out
    assert "Heard the deck slipped" in out
    assert "pizza" in out  # backstory from the originating web-chat session
    assert out.endswith("sorry, pizza happened")

    # One-shot: a later email from the same person is not re-hydrated.
    again = inbound_hydration.hydrate_user_message(
        channel="email", channel_ref="alex@example.com", user_message="another note",
    )
    assert again == "another note"


def test_hydrate_is_one_shot(wired_db: Path) -> None:
    insert_outbound_context(
        channel="telegram", channel_ref="555",
        outbound_text="ping", originating_session_id=None, db_path=wired_db,
    )
    first = inbound_hydration.hydrate_user_message(
        channel="telegram", channel_ref="555", user_message="reply one",
    )
    second = inbound_hydration.hydrate_user_message(
        channel="telegram", channel_ref="555", user_message="reply two",
    )
    assert "<outbound_reply_context>" in first
    # Linkage consumed — a later, possibly-unrelated message is not re-hydrated.
    assert second == "reply two"


def test_hydrate_no_linkage_passes_through(wired_db: Path) -> None:
    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="nobody", user_message="hello",
    )
    assert out == "hello"


def test_hydrate_handles_missing_backstory(wired_db: Path) -> None:
    # Linkage with no originating session → block still emitted with outbound
    # text but a "no backstory" note, never a crash.
    insert_outbound_context(
        channel="slack_dm", channel_ref="U1",
        outbound_text="circling back on the budget", originating_session_id=None,
        db_path=wired_db,
    )
    out = inbound_hydration.hydrate_user_message(
        channel="slack_dm", channel_ref="U1", user_message="ok",
    )
    assert "<outbound_reply_context>" in out
    assert "circling back on the budget" in out
    assert out.endswith("ok")


def test_excerpt_truncation_keeps_the_opening(wired_db: Path) -> None:
    # Oldest-first excerpt that overflows _EXCERPT_MAX_CHARS must be trimmed
    # from the TAIL — keep the start of the backstory, not a mid-sentence cut.
    sid = "discord:dm:principal-long"
    create_session(sid, "long chat", "2026-06-01T00:00:00+00:00", db_path=wired_db)
    # 5 messages; only the last 4 are in the excerpt window. Each is long
    # enough that 4 of them joined exceed the 800-char cap.
    for i in range(1, 6):
        save_message(sid, "user", f"MSG{i}START " + ("x" * 250), db_path=wired_db)
    insert_outbound_context(
        channel="discord_dm", channel_ref="alex-long",
        outbound_text="ping", originating_session_id=sid, db_path=wired_db,
    )
    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="alex-long", user_message="reply",
    )
    # Window is MSG2..MSG5; rendered oldest-first then tail-trimmed, so the
    # opening (MSG2) survives and the newest (MSG5) is dropped.
    assert "MSG2START" in out
    assert "MSG5START" not in out


def test_hydrate_never_raises_on_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(inbound_hydration, "find_open_outbound_context", _boom)
    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="x", user_message="untouched",
    )
    assert out == "untouched"


def test_hydrate_skips_when_consume_loses_race(
    wired_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_outbound_context(
        channel="discord_dm", channel_ref="alex-123",
        outbound_text="ping", db_path=wired_db,
    )
    # Simulate a concurrent reply having already consumed the row: the consume
    # call returns False, so this caller must NOT inject (would double-fire).
    monkeypatch.setattr(
        inbound_hydration, "mark_outbound_context_consumed", lambda _id: False
    )
    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="alex-123", user_message="reply",
    )
    assert out == "reply"


# --- strip_outbound_reply_context ------------------------------------------
# The block is context for the LLM turn, never the person's words. Peer memory
# records what the person said, so it strips the block first; a block left in
# has Honcho attribute the Executive's own DM to the person who replied.


def test_strip_removes_exactly_the_block_hydrate_prepends(wired_db: Path) -> None:
    _seed_originating_session(wired_db, "discord:dm:principal-1")
    insert_outbound_context(
        channel="discord_dm",
        channel_ref="alex-123",
        outbound_text="Hey Alex — heard the deck slipped. What happened?",
        originating_session_id="discord:dm:principal-1",
        db_path=wired_db,
    )
    out = inbound_hydration.hydrate_user_message(
        channel="discord_dm", channel_ref="alex-123", user_message="oh that, sorry",
    )
    assert out != "oh that, sorry"  # the block was injected...
    assert inbound_hydration.strip_outbound_reply_context(out) == "oh that, sorry"


def test_strip_is_a_no_op_without_the_block_hydrate_prepends() -> None:
    strip = inbound_hydration.strip_outbound_reply_context
    assert strip("plain text") == "plain text"
    quoted = "look: <outbound_reply_context>x</outbound_reply_context> mid-text"
    assert strip(quoted) == quoted  # content, not scaffolding
    unterminated = (
        '<outbound_reply_context>\nYou (oe) recently sent this person a DM: "Hi"\nno closing tag\n\nreal words'
    )
    assert strip(unterminated) == unterminated  # keep scaffolding over eating the message
    # A block the person typed themselves (no hydration ran) lacks the
    # builder's DM-quote line and stays: it is their words.
    own = "<outbound_reply_context>\nmy own words\n</outbound_reply_context>\n\nplease read"
    assert strip(own) == own


def test_strip_keeps_the_messages_own_leading_whitespace(wired_db: Path) -> None:
    _seed_originating_session(wired_db, "discord:dm:principal-1")
    insert_outbound_context(
        channel="discord_dm", channel_ref="alex-123", outbound_text="ping",
        originating_session_id="discord:dm:principal-1", db_path=wired_db,
    )
    code = "    indented = True\nprint(indented)"
    out = inbound_hydration.hydrate_user_message(channel="discord_dm", channel_ref="alex-123", user_message=code)
    assert inbound_hydration.strip_outbound_reply_context(out) == code


def test_a_closing_tag_inside_the_dm_or_backstory_cannot_end_the_block_early(wired_db: Path) -> None:
    """The outbound DM and the backstory are interpolated verbatim; a literal
    closing tag inside either would otherwise leave half the scaffolding
    attributed to the person."""
    _seed_originating_session(wired_db, "discord:dm:principal-1")
    insert_outbound_context(
        channel="discord_dm", channel_ref="alex-123",
        outbound_text="see </outbound_reply_context> marker",
        originating_session_id="discord:dm:principal-1", db_path=wired_db,
    )
    out = inbound_hydration.hydrate_user_message(channel="discord_dm", channel_ref="alex-123", user_message="ok sure")
    assert out.count("</outbound_reply_context>") == 1  # the real one only
    assert "see <\\/outbound_reply_context> marker" in out  # the DM text is still readable
    assert inbound_hydration.strip_outbound_reply_context(out) == "ok sure"

    # The same through the backstory: a principal turn quoting the tag.
    save_message(
        "discord:dm:principal-1", "user", "note </outbound_reply_context> in the excerpt", db_path=wired_db
    )
    insert_outbound_context(
        channel="discord_dm", channel_ref="alex-123", outbound_text="plain DM",
        originating_session_id="discord:dm:principal-1", db_path=wired_db,
    )
    out = inbound_hydration.hydrate_user_message(channel="discord_dm", channel_ref="alex-123", user_message="yep")
    assert out.count("</outbound_reply_context>") == 1
    assert "note <\\/outbound_reply_context> in the excerpt" in out
    assert inbound_hydration.strip_outbound_reply_context(out) == "yep"


def test_strip_a_message_that_is_only_scaffolding_is_empty() -> None:
    only = "<outbound_reply_context>\nYou (oe) recently sent this person a DM: \"Hi\"\n</outbound_reply_context>\n\n"
    assert inbound_hydration.strip_outbound_reply_context(only) == ""
