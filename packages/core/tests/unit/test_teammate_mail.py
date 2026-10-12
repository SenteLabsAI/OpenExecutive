"""A teammate's mail with the Executive is theirs: the chat's mailbox reads
leave it out for everyone else (orchestrator/teammate_mail.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.memory import episodic
from openexecutive.orchestrator import people_tools
from openexecutive.orchestrator import teammate_mail as tm
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = "olivia@co.example"
TEAMMATE = "ben@co.example"
VENDOR = "billing@northwind.example"
EXEC = "exec@co.example"

SEARCH = "google_workspace__search_gmail_messages"
READ = "google_workspace__get_gmail_message_content"
THREAD = "google_workspace__get_gmail_thread_content"
BATCH = "google_workspace__get_gmail_messages_content_batch"
OUTLOOK_LIST = "microsoft_365__list-mail-messages"


@pytest.fixture(autouse=True)
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    monkeypatch.setattr(episodic, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    people_registry.invalidate()
    owner = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    ben = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAMMATE)
    token = current_session.set(SimpleNamespace(unattended=False))
    yield SimpleNamespace(owner=owner, ben=ben)
    current_session.reset(token)
    people_registry.invalidate()


def _asker(person_id: int | None):  # type: ignore[no-untyped-def]
    return patch.object(people_tools, "_verified_asker_id", lambda: person_id)


def _message(sender: str, to: str, body: str = "Numbers attached.") -> str:
    return (
        f"Message ID: m1\nSubject: Q3 numbers\nFrom: Someone <{sender}>\n"
        f"To: {to}\nCc: [not present in Gmail response]\n\n--- BODY ---\n{body}\n"
    )


def _listing(*senders: str) -> str:
    lines = ["Found 2 messages matching 'q':", "", "📧 MESSAGES:"]
    for i, sender in enumerate(senders, 1):
        lines += [
            f"  {i}. Message ID: m{i}",
            f"     Subject: Subject {i}",
            f"     From: Person {i} <{sender}>",
            "     Date: Mon, 5 Oct 2026",
            f"     Web Link: https://mail.google.com/m{i}",
            f"     Thread ID: t{i}",
            f"     Thread Link: https://mail.google.com/t{i}",
            "",
        ]
    lines += ["💡 USAGE:", "  • Pass the Message IDs as a list"]
    return "\n".join(lines)


def test_the_owner_cannot_read_a_teammates_email_to_the_executive(roster: SimpleNamespace) -> None:
    with _asker(roster.owner):
        out = tm.filter_mail_result(READ, _message(TEAMMATE, EXEC))
    assert json.loads(out)["withheld"] is True
    assert "Numbers attached" not in out


def test_the_teammate_reads_their_own(roster: SimpleNamespace) -> None:
    text = _message(TEAMMATE, EXEC)
    with _asker(roster.ben):
        assert tm.filter_mail_result(READ, text) == text


def test_mail_the_reader_is_on_stays(roster: SimpleNamespace) -> None:
    text = _message(TEAMMATE, f"{EXEC}, Olivia <{OWNER}>")
    with _asker(roster.owner):
        assert tm.filter_mail_result(READ, text) == text


def test_outside_mail_stays(roster: SimpleNamespace) -> None:
    text = _message(VENDOR, EXEC)
    with _asker(roster.owner):
        assert tm.filter_mail_result(READ, text) == text


def test_a_teammate_in_the_body_hides_it(roster: SimpleNamespace) -> None:
    text = _message(VENDOR, EXEC, body=f"On Monday Ben <{TEAMMATE}> wrote: my bank details")
    with _asker(roster.owner):
        assert json.loads(tm.filter_mail_result(READ, text))["withheld"] is True


def test_a_thread_with_the_teammate_is_withheld_whole(roster: SimpleNamespace) -> None:
    text = (
        "Thread ID: t1\nSubject: Taxes\n\n=== Message 1 ===\n"
        f"From: Ben <{TEAMMATE}>\nTo: {EXEC}\n\nmy tax papers\n\n=== Message 2 ===\n"
        f"From: {EXEC}\nTo: {TEAMMATE}\n\nGot them\n"
    )
    with _asker(roster.owner):
        out = tm.filter_mail_result(THREAD, text)
    assert "tax papers" not in out


def test_a_listing_drops_only_the_teammates_entries(roster: SimpleNamespace) -> None:
    with _asker(roster.owner):
        out = tm.filter_mail_result(SEARCH, _listing(VENDOR, TEAMMATE))
    assert VENDOR in out and "Subject 1" in out
    assert TEAMMATE not in out and "Subject 2" not in out
    assert "Left out 1 message involving another teammate" in out


def test_a_batch_with_one_teammates_message_is_withheld(roster: SimpleNamespace) -> None:
    text = "Retrieved 2 messages:\n\n" + _message(f"{OWNER}", EXEC) + "\n---\n\n" + _message(TEAMMATE, EXEC)
    with _asker(roster.owner):
        out = json.loads(tm.filter_mail_result(BATCH, text))
    assert out["withheld"] is True and "one at a time" in out["note"]


def test_an_outlook_list_drops_the_teammates_items(roster: SimpleNamespace) -> None:
    def item(addr: str, subject: str) -> dict:  # type: ignore[type-arg]
        return {
            "subject": subject,
            "from": {"emailAddress": {"address": addr}},
            "toRecipients": [{"emailAddress": {"address": EXEC}}],
        }

    text = json.dumps({"value": [item(VENDOR, "Invoice"), item(TEAMMATE, "My taxes")]})
    with _asker(roster.owner):
        out = json.loads(tm.filter_mail_result(OUTLOOK_LIST, text))
    assert [i["subject"] for i in out["value"]] == ["Invoice"]
    assert "private" in out["private_note"]


def test_an_unverified_reader_sees_no_teammates_mail(roster: SimpleNamespace) -> None:
    with _asker(None), patch.object(people_tools, "is_principal_on_verified_surface", lambda _s: False):
        out = tm.filter_mail_result(READ, _message(OWNER, EXEC))
    assert json.loads(out)["withheld"] is True


def test_an_unattended_run_is_untouched(roster: SimpleNamespace) -> None:
    text = _message(TEAMMATE, EXEC)
    current_session.set(SimpleNamespace(unattended=True))
    with _asker(None):
        assert tm.filter_mail_result(READ, text) == text


def test_other_tools_pass(roster: SimpleNamespace) -> None:
    text = f"Event with {TEAMMATE}"
    with _asker(roster.owner):
        assert tm.filter_mail_result("google_workspace__get_events", text) == text


def test_a_roster_failure_withholds(roster: SimpleNamespace) -> None:
    with _asker(roster.owner), patch.object(people_store, "list_people", side_effect=RuntimeError):
        out = tm.filter_mail_result(READ, _message(VENDOR, EXEC))
    assert json.loads(out)["withheld"] is True


def test_guard_call_tool_filters_the_gateway_result(roster: SimpleNamespace) -> None:
    call = AsyncMock(return_value=_message(TEAMMATE, EXEC))
    with _asker(roster.owner):
        out = asyncio.run(tm.guard_call_tool(call)({"name": READ, "arguments": {"message_id": "m1"}}))
    assert json.loads(out)["withheld"] is True
    call.assert_awaited_once()
