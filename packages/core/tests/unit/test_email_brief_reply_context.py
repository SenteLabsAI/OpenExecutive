"""A reply by email to the morning brief or evening digest sees what it said.

A chat app keeps the brief in the conversation the reply joins
(``test_chat_deliveries_in_history``). An email reply starts a turn of its
own, so the scheduler records the emailed brief the way a DM the Executive
sends is recorded, and ``inbound_hydration`` hands it to the reply's turn.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.integrations import inbound_hydration
from openexecutive.memory.episodic import _get_conn, initialize_db
from openexecutive.people.models import Person
from openexecutive.scheduler import runner

DIGEST = (
    "Evening digest\n\n"
    "Done for you\n"
    "- Moved your Thursday call with Lumen Freight to 3 PM.\n"
    "- Sent the pricing summary to Priya.\n"
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "episodic.db"
    initialize_db(path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", path)
    return path


def _owner() -> Person:
    return Person(id=7, full_name="Dana Reyes", is_principal=True, email="Dana@Example.com")


def _email(monkeypatch: pytest.MonkeyPatch, text: str, error: str | None = None) -> bool:
    async def fake_email(_person: Person, _text: str, _label: str) -> str | None:
        return error

    monkeypatch.setattr(runner, "_email_principal", fake_email)
    delivery = asyncio.run(runner._send_on_plan(_owner(), ["email"], text, label="Evening digest"))
    return delivery.ok


def _reply(sender: str, words: str = "Why did you do the second one?") -> str:
    return inbound_hydration.hydrate_user_message(
        channel="email", channel_ref=sender, user_message=words,
    )


def test_the_owners_email_reply_carries_the_digest(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _email(monkeypatch, DIGEST)
    out = _reply("dana@example.com")
    assert out.startswith("<outbound_reply_context>")
    assert "Sent the pricing summary to Priya." in out
    assert out.endswith("Why did you do the second one?")
    # Marked as the Executive's message, never the person's words.
    assert inbound_hydration.strip_outbound_reply_context(out) == "Why did you do the second one?"
    # One reply consumes it, like any DM's.
    assert _reply("dana@example.com", "and the first?") == "and the first?"


def test_someone_elses_email_does_not_see_it(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _email(monkeypatch, DIGEST)
    assert _reply("sam@example.com") == "Why did you do the second one?"


def test_a_reply_after_the_window_does_not_see_it(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _email(monkeypatch, DIGEST)
    old = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    with _get_conn(db) as conn:
        conn.execute("UPDATE outbound_context SET created_at = ?", (old,))
    assert _reply("dana@example.com") == "Why did you do the second one?"


def test_an_email_that_did_not_go_is_not_recorded(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not _email(monkeypatch, DIGEST, error="the MCP gateway is not running")
    assert _reply("dana@example.com") == "Why did you do the second one?"


def test_a_long_brief_keeps_more_than_a_dm(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    items = "".join(f"- Item {n}: followed up with the Lumen Freight team.\n" for n in range(80))
    brief = f"Morning brief\n\n{items}"
    assert len(brief) > 2000
    assert _email(monkeypatch, brief)
    with _get_conn(db) as conn:
        kept = conn.execute("SELECT outbound_text FROM outbound_context").fetchone()[0]
    assert kept == brief[: runner._EMAIL_REPLY_CONTEXT_CHARS]
    assert "Item 60" in kept
