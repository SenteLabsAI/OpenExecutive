"""A follow-up overtaken by events stays current: when it fires the run sees
what its setter wrote since (scheduler.runner._since_scheduled_block) and
stands down when that shows it is handled, and every chat turn shows the
speaker's pending ones (correction_tools.pending_followups_block) so the
turn that settles one can cancel it."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.memory import episodic
from openexecutive.memory.session_store import create_session, own_messages_since, save_message
from openexecutive.orchestrator import correction_tools as ct
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.scheduler import runner


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="olivia@co.example", telegram_chat_id="111",
    )
    teammate = people_store.upsert_person(full_name="Ben Teammate", slack_user_id="UBEN")
    people_registry.invalidate()
    return SimpleNamespace(principal=people_store.get_person(principal),
                           teammate=people_store.get_person(teammate))


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def _message(db: Path, session_id: str, owner: int | None, role: str, text: str,
             at: timedelta, sender: int | None = None) -> None:
    create_session(session_id, "t", _iso(-timedelta(days=3)), caller_person_id=owner, db_path=db)
    save_message(session_id, role, text, db_path=db, sender_person_id=sender)
    with episodic._get_conn(db) as conn:
        conn.execute(
            "UPDATE chat_messages SET created_at = ? WHERE id = (SELECT MAX(id) FROM chat_messages)",
            (_iso(at),),
        )


def _followup(session_id: str | None, *, ref: str = "111", created_by: int | None = None) -> int:
    action_id = episodic.insert_scheduled_action(
        run_at=_iso(timedelta(days=1)), channel="telegram", channel_ref=ref,
        intent_text="Check whether Dana replied about the lease; if so, compare the terms.",
        originating_session_id=session_id, created_by_person_id=created_by,
    )
    with episodic._get_conn(db_path=episodic.DB_PATH) as conn:
        conn.execute("UPDATE scheduled_actions SET created_at = ? WHERE id = ?",
                     (_iso(-timedelta(days=1)), action_id))
    return action_id


def test_own_messages_since_keeps_only_their_words_after_the_time(
    db: Path, roster: SimpleNamespace
) -> None:
    me, ben = roster.principal.id, roster.teammate.id
    _message(db, "web-1", me, "user", "too early", -timedelta(days=2), sender=me)
    _message(db, "web-1", me, "user", "Dana   replied,\nlet's compare", -timedelta(hours=3), sender=me)
    _message(db, "web-1", me, "assistant", "Here is the comparison", -timedelta(hours=3))
    _message(db, "shared", ben, "user", "Ben's words", -timedelta(hours=2), sender=ben)
    _message(db, "old", me, "user", "before senders", -timedelta(hours=1))
    rows = own_messages_since(me, _iso(-timedelta(days=1)), db_path=db)
    assert [r["text"] for r in rows] == ["Dana replied, let's compare", "before senders"]
    assert own_messages_since(me, _iso(-timedelta(days=1)), limit=1, db_path=db)[0]["text"] == "before senders"
    assert own_messages_since(me, _iso(-timedelta(days=1)), max_chars=6, db_path=db)[0]["text"] == "Dana…"


def _fire(action_id: int, reply: object) -> tuple[AsyncMock, str]:
    [action] = [a for a in episodic.claim_due_actions(datetime.now(UTC) + timedelta(days=2))
                if a.id == action_id]
    with patch("openexecutive.orchestrator.executive.Executive.chat",
               new_callable=AsyncMock, return_value=reply) as chat, patch(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        return_value=MagicMock(is_empty=lambda: True),
    ), patch("openexecutive.knowledge.retriever.retrieve", return_value=""), patch(
        "openexecutive.memory.episodic.format_for_prompt", return_value="",
    ):
        asyncio.run(runner._execute_action(action, gateway=None))
    return chat, str(chat.call_args.kwargs["user_message"])


def test_a_handled_followup_is_not_sent(db: Path, roster: SimpleNamespace) -> None:
    me = roster.principal.id
    create_session("tg-1", "t", _iso(-timedelta(days=2)), caller_person_id=me, db_path=db)
    action_id = _followup("tg-1", created_by=me)
    _message(db, "web-2", me, "user", "Dana replied with new terms, compare them", -timedelta(hours=2), sender=me)
    _, prompt = _fire(action_id, "ALREADY_HANDLED: they compared Dana's terms yesterday")
    assert "<since_scheduled>" in prompt and "Dana replied with new terms" in prompt
    row = episodic.get_scheduled_action(action_id)
    assert row is not None and row.status == "cancelled"
    assert row.last_error == "skipped: already handled (they compared Dana's terms yesterday)"


def test_a_followup_still_due_runs_as_before(db: Path, roster: SimpleNamespace) -> None:
    me = roster.principal.id
    create_session("tg-1", "t", _iso(-timedelta(days=2)), caller_person_id=me, db_path=db)
    action_id = _followup("tg-1")  # owner from the conversation
    _message(db, "web-2", me, "user", "What's on my calendar?", -timedelta(hours=2), sender=me)
    _, prompt = _fire(action_id, "Sent.")
    assert "What's on my calendar?" in prompt
    assert episodic.get_scheduled_action(action_id).status == "done"  # type: ignore[union-attr]


def test_no_chat_since_or_someone_elses_address_shows_nothing(
    db: Path, roster: SimpleNamespace
) -> None:
    me = roster.principal.id
    create_session("tg-1", "t", _iso(-timedelta(days=2)), caller_person_id=me, db_path=db)
    action_id = _followup("tg-1", created_by=me)
    _, prompt = _fire(action_id, "Sent.")
    assert "<since_scheduled>" not in prompt
    # Addressed to someone else: their chat is not quoted, whatever it says.
    _message(db, "web-2", me, "user", "private words", -timedelta(hours=2), sender=me)
    other = _followup("tg-1", ref="999", created_by=me)
    _, prompt = _fire(other, "Sent.")
    assert "private words" not in prompt


def test_the_turn_shows_the_speakers_pending_followups(db: Path, roster: SimpleNamespace) -> None:
    mine = _followup("s-1")
    _followup("someone-else")
    session = Session(session_id="s-1", from_web_chat=True, caller_person_id=roster.principal.id,
                      web_caller_signed_in=True)
    block = ct.pending_followups_block(session)
    assert f"#{mine}," in block and "cancel_followup" in block
    assert block.count("\n- #") == 1
    session.unattended = True
    assert ct.pending_followups_block(session) == ""


def test_a_teammates_handled_followup_is_not_sent(db: Path, roster: SimpleNamespace) -> None:
    ben = roster.teammate.id
    action_id = episodic.insert_scheduled_action(
        run_at=_iso(timedelta(days=1)), channel="slack_dm", channel_ref="UBEN",
        intent_text="Check whether the vendor answered.", originating_session_id="ben-1",
        created_by_person_id=ben,
    )
    with episodic._get_conn(db) as conn:
        conn.execute("UPDATE scheduled_actions SET created_at = ? WHERE id = ?",
                     (_iso(-timedelta(days=1)), action_id))
    _message(db, "ben-2", ben, "user", "The vendor answered, all sorted", -timedelta(hours=1), sender=ben)
    with patch.object(runner, "_send_on_plan", new_callable=AsyncMock) as send:
        _, prompt = _fire(action_id, "ALREADY_HANDLED: the vendor answered")
    send.assert_not_called()
    assert "The vendor answered, all sorted" in prompt
    assert episodic.get_scheduled_action(action_id).status == "cancelled"  # type: ignore[union-attr]
