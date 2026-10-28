"""Chat drafts taken out of the person's Drafts once the Executive sends the
email itself (delegation/redrafts.py, orchestrator/mcp_gateway.py)."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import drafts, redrafts
from openexecutive.delegation.gmail import DraftInfo, MailMessage
from openexecutive.memory import episodic
from openexecutive.orchestrator import mcp_gateway
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
OWNER = "olivia@co.example"
DANA = "dana@northpeak.example"
SAM = "sam@northpeak.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    people_registry.invalidate()


class Mailbox:
    def __init__(self) -> None:
        self.saved: dict[str, DraftInfo] = {}
        self.deleted: list[str] = []

    def add(self, draft_id: str, to: list[str], *, version: str | None = None, subject: str = "Pilot dates") -> None:
        message = MailMessage(id=version or f"m-{draft_id}", thread_id="t1", to=to, subject=subject)
        self.saved[draft_id] = DraftInfo(draft_id=draft_id, message=message)

    async def get_draft(self, draft_id: str) -> DraftInfo | None:
        return self.saved.get(draft_id)

    async def delete_draft(self, draft_id: str) -> bool:
        self.deleted.append(draft_id)
        return self.saved.pop(draft_id, None) is not None


def _owner() -> int:
    person_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    return person_id


def _record(person_id: int, draft_id: str, session_id: str | None) -> int:
    return drafts.record(
        person_id, source=drafts.SOURCE_CHAT, thread_id="t1", draft_id=draft_id,
        message_id=f"m-{draft_id}", now=NOW, session_id=session_id,
    )


def test_the_executives_own_send_clears_the_drafts_it_replaced() -> None:
    owner = _owner()
    mailbox = Mailbox()
    for draft_id in ("d1", "d2"):
        mailbox.add(draft_id, [DANA])
        _record(owner, draft_id, "s1")
    mailbox.add("d3", [SAM])
    _record(owner, "d3", "s1")  # to someone else: stays
    mailbox.add("d4", [DANA])
    _record(owner, "d4", "s2")  # another conversation: stays
    mailbox.add("d5", [DANA], subject="Budget for Q4")
    _record(owner, "d5", "s1")  # another email to Dana: stays

    # It went to Dana, copying the writer, about the same thing.
    gone = asyncio.run(redrafts.clear_after_send(
        "s1", {DANA, OWNER}, subject="Re: Pilot dates", client=mailbox, now=NOW,
    ))
    assert gone == 2 and sorted(mailbox.deleted) == ["d1", "d2"]
    assert sorted(mailbox.saved) == ["d3", "d4", "d5"]
    assert drafts.count_waiting(owner, NOW) == 3


def test_a_send_about_something_else_clears_nothing() -> None:
    owner = _owner()
    mailbox = Mailbox()
    mailbox.add("d1", [DANA], subject="Budget for Q4")
    _record(owner, "d1", "s1")
    assert asyncio.run(redrafts.clear_after_send("s1", {DANA}, subject="Running late", client=mailbox, now=NOW)) == 0
    assert mailbox.deleted == []


def test_same_topic() -> None:
    assert redrafts.same_topic("Pilot dates", "RE: Fwd: pilot  dates")
    assert redrafts.same_topic("Alpine Valley Farms", "Fwd: Alpine Valley Farms, LLC appeal")
    assert not redrafts.same_topic("Hi", "Hi there, about the budget")  # too short to mean it
    assert not redrafts.same_topic("Budget", "")


def test_a_redraft_to_the_same_person_about_something_else_is_another_email() -> None:
    owner = _owner()
    mailbox = Mailbox()
    mailbox.add("d1", [DANA], subject="Budget for Q4")
    earlier = drafts.record(
        owner, source=drafts.SOURCE_CHAT, thread_id=None, draft_id="d1", message_id="m-d1", now=NOW, session_id="s1",
    )
    mailbox.add("d2", [DANA], subject="Pilot dates")
    newer = drafts.record(
        owner, source=drafts.SOURCE_CHAT, thread_id=None, draft_id="d2", message_id="m-d2", now=NOW, session_id="s1",
    )
    replaced = asyncio.run(redrafts.replace_earlier(
        owner, mailbox, row_id=newer, thread_id=None, session_id="s1", to=[DANA], subject="Pilot dates", now=NOW,
    ))
    assert replaced == 0 and mailbox.deleted == []
    row = drafts.get_row(earlier)
    assert row is not None and not row.gone_at


def test_a_send_that_only_copies_the_writer_clears_nothing() -> None:
    owner = _owner()
    mailbox = Mailbox()
    mailbox.add("d1", [DANA])
    _record(owner, "d1", "s1")
    assert asyncio.run(redrafts.clear_after_send("s1", {OWNER}, subject="Pilot dates", client=mailbox, now=NOW)) == 0
    assert mailbox.deleted == []


def test_an_edited_draft_stays_and_a_missing_one_stops_waiting() -> None:
    owner = _owner()
    mailbox = Mailbox()
    mailbox.add("d1", [DANA], version="m-edited")
    edited = _record(owner, "d1", "s1")
    missing = _record(owner, "d2", "s1")  # deleted in their mailbox already
    assert asyncio.run(redrafts.clear_after_send("s1", {DANA}, subject="Pilot dates", client=mailbox, now=NOW)) == 0
    assert mailbox.deleted == [] and "d1" in mailbox.saved
    kept, gone = drafts.get_row(edited), drafts.get_row(missing)
    assert kept is not None and not kept.gone_at
    assert gone is not None and gone.gone_at


def test_a_mailbox_that_fails_leaves_the_drafts_alone() -> None:
    owner = _owner()

    class Down(Mailbox):
        async def get_draft(self, draft_id: str) -> DraftInfo | None:
            raise RuntimeError("down")

    row = _record(owner, "d1", "s1")
    assert asyncio.run(redrafts.clear_after_send("s1", {DANA}, client=Down(), now=NOW)) == 0
    kept = drafts.get_row(row)
    assert kept is not None and not kept.gone_at


def test_the_gateway_clears_after_a_send_in_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, set[str], str]] = []

    async def fake(session_id: str, sent_to: set[str], *, subject: str = "", client: Any = None) -> int:
        calls.append((session_id, sent_to, subject))
        return 0

    monkeypatch.setattr(redrafts, "clear_after_send", fake)
    session = Session(session_id="s1")

    async def go() -> None:
        token = current_session.set(session)
        try:
            await mcp_gateway._clear_replaced_drafts(
                "google_workspace__send_gmail_message",
                "google_workspace__send_gmail_message",
                {"to": f"Dana <{DANA}>", "cc": OWNER, "subject": "Notes"},
            )
            # A read is no send.
            await mcp_gateway._clear_replaced_drafts(
                "google_workspace__search_gmail_messages", "google_workspace__search_gmail_messages", {},
            )
        finally:
            current_session.reset(token)
        # No conversation, nothing to clear.
        await mcp_gateway._clear_replaced_drafts(
            "google_workspace__send_gmail_message", "google_workspace__send_gmail_message", {"to": DANA},
        )

    asyncio.run(go())
    assert calls == [("s1", {DANA, OWNER}, "Notes")]
