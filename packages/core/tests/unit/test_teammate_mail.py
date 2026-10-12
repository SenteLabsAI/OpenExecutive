"""A teammate's mail with the Executive is theirs: the chat reaches the
Executive's mailbox only around it (orchestrator/teammate_mail.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.memory import episodic
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
SEND = "google_workspace__send_gmail_message"
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
    session = SimpleNamespace(unattended=False, mail_ids_seen=set(), caller_person_id=owner)
    token = current_session.set(session)
    yield SimpleNamespace(owner=owner, ben=ben, session=session)
    current_session.reset(token)
    people_registry.invalidate()


def _reader(person_id: int | None):  # type: ignore[no-untyped-def]
    return patch.object(tm, "_reader_id", lambda _s: person_id)


class FakeMailbox:
    """The gateway's call_tool: canned results by tool name, calls recorded."""

    def __init__(self, results: dict[str, str]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, tool_input: dict[str, Any]) -> str:
        self.calls.append(tool_input)
        return self.results.get(tool_input["name"], "ok")


def _run(mailbox: FakeMailbox, name: str, **arguments: Any) -> str:
    return asyncio.run(tm.guard_call_tool(mailbox)({"name": name, "arguments": arguments}))


def _message(sender: str, to: str, body: str = "Numbers attached.", mid: str = "m1") -> str:
    return (
        f"Message ID: {mid}\nSubject: Q3 numbers\nFrom: Someone <{sender}>\n"
        f"To: {to}\nCc: [not present in Gmail response]\n\n--- BODY ---\n{body}\n"
    )


def _listing(*senders: str) -> str:
    lines = [f"Found {len(senders)} messages matching 'q':", "", "📧 MESSAGES:"]
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


def _thread(*messages: tuple[str, str, str]) -> str:
    text = "Thread ID: t1\nSubject: Taxes\nMessages: 2\n\n"
    for i, (sender, to, body) in enumerate(messages, 1):
        text += f"=== Message {i} ===\nFrom: {sender}\nTo: {to}\n\n{body}\n\n"
    return text


# ── searches ──────────────────────────────────────────────────────────────


def test_a_search_leaves_teammates_out_and_asks_for_headers(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({SEARCH: _listing(VENDOR)})
    with _reader(roster.owner):
        _run(mailbox, SEARCH, query="from:ben", user_google_email=EXEC)
    sent = mailbox.calls[0]["arguments"]
    assert sent["include_headers"] is True
    assert sent["query"].startswith("(from:ben) ")
    assert f"-{{from:{TEAMMATE} to:{TEAMMATE} cc:{TEAMMATE}}}" in sent["query"]
    assert OWNER not in sent["query"]


def test_a_listing_drops_the_teammates_entries_and_remembers_the_rest(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({SEARCH: _listing(VENDOR, TEAMMATE)})
    with _reader(roster.owner):
        out = _run(mailbox, SEARCH, query="q")
    assert "Subject 1" in out and "Subject 2" not in out and TEAMMATE not in out
    assert "Left out 1 message involving another teammate" in out
    assert {f"{roster.owner}:m1", f"{roster.owner}:t1"} <= roster.session.mail_ids_seen
    assert not any(i.endswith(":m2") for i in roster.session.mail_ids_seen)


def test_a_listing_without_headers_shows_nothing(roster: SimpleNamespace) -> None:
    bare = "Found 1 messages matching 'q':\n\n📧 MESSAGES:\n  1. Message ID: m9\n     Thread ID: t9\n\n💡 USAGE:\n"
    with _reader(roster.owner):
        out = _run(FakeMailbox({SEARCH: bare}), SEARCH, query="q")
    assert "m9" not in out and not roster.session.mail_ids_seen


# ── reaching mail by id ───────────────────────────────────────────────────


def test_an_id_the_conversation_was_not_shown_is_refused(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({READ: _message(VENDOR, EXEC)})
    with _reader(roster.owner):
        out = _run(mailbox, READ, message_id="ben-private")
    assert "hasn't found" in json.loads(out)["error"]
    assert mailbox.calls == []


def test_a_reply_into_an_unseen_thread_is_refused(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner):
        out = _run(mailbox, SEND, to=OWNER, thread_id="ben-thread", body="x")
    assert "error" in json.loads(out) and mailbox.calls == []


def test_quoting_a_thread_with_a_teammates_message_is_refused(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "t1")
    mailbox = FakeMailbox({THREAD: _thread((VENDOR, EXEC, "invoice"), (TEAMMATE, EXEC, "my bank details"))})
    with _reader(roster.owner):
        out = _run(mailbox, SEND, to=OWNER, thread_id="t1", quote_original=True, body="x")
    assert "can't be quoted" in json.loads(out)["error"]
    assert [c["name"] for c in mailbox.calls] == [THREAD]


def test_the_seen_vendor_message_reads(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    text = _message(VENDOR, EXEC)
    with _reader(roster.owner):
        assert _run(FakeMailbox({READ: text}), READ, message_id="m1") == text


def test_the_inbound_email_a_turn_answers_is_reachable(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "inbound-m", "inbound-t")
    mailbox = FakeMailbox({SEND: "Email sent! Message ID: r1"})
    with _reader(roster.ben):
        out = _run(mailbox, SEND, to=TEAMMATE, thread_id="inbound-t", body="Got it")
    assert out == "Email sent! Message ID: r1"


# ── reads, message by message ─────────────────────────────────────────────


def test_a_teammates_message_is_withheld_from_the_owner(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    with _reader(roster.owner):
        out = _run(FakeMailbox({READ: _message(TEAMMATE, EXEC)}), READ, message_id="m1")
    assert json.loads(out)["withheld"] is True and "Numbers attached" not in out


def test_the_teammate_reads_their_own(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    text = _message(TEAMMATE, EXEC)
    with _reader(roster.ben):
        assert _run(FakeMailbox({READ: text}), READ, message_id="m1") == text


def test_a_teammate_in_the_body_hides_it(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    text = _message(VENDOR, EXEC, body=f"On Monday Ben <{TEAMMATE}> wrote: my bank details")
    with _reader(roster.owner):
        assert json.loads(_run(FakeMailbox({READ: text}), READ, message_id="m1"))["withheld"] is True


def test_a_thread_keeps_only_the_messages_that_are_not_a_teammates(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "t1")
    text = _thread((VENDOR, EXEC, "invoice due"), (TEAMMATE, EXEC, "my bank details"))
    with _reader(roster.owner):
        out = _run(FakeMailbox({THREAD: text}), THREAD, thread_id="t1")
    assert "invoice due" in out and "bank details" not in out
    assert "Left out 1 message" in out


def test_a_cc_on_one_message_does_not_open_the_rest(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "t1")
    text = _thread((TEAMMATE, EXEC, "my bank details"), (EXEC, f"{TEAMMATE}, {OWNER}", "Looping in Olivia"))
    with _reader(roster.owner):
        out = _run(FakeMailbox({THREAD: text}), THREAD, thread_id="t1")
    assert "Looping in Olivia" in out and "bank details" not in out


def test_a_batch_with_one_teammates_message_is_withheld(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1", "m2")
    text = "Retrieved 2 messages:\n\n" + _message(OWNER, EXEC) + "\n---\n\n" + _message(TEAMMATE, EXEC, mid="m2")
    with _reader(roster.owner):
        out = json.loads(_run(FakeMailbox({BATCH: text}), BATCH, message_ids=["m1", "m2"]))
    assert out["withheld"] is True and "one at a time" in out["note"]


# ── Outlook ───────────────────────────────────────────────────────────────


def _item(addr: str, subject: str, mid: str) -> dict[str, Any]:
    return {
        "id": mid,
        "subject": subject,
        "from": {"emailAddress": {"address": addr}},
        "toRecipients": [{"emailAddress": {"address": EXEC}}],
    }


def test_an_outlook_list_selects_the_parties_and_drops_the_teammates_items(roster: SimpleNamespace) -> None:
    text = json.dumps({"value": [_item(VENDOR, "Invoice", "o1"), _item(TEAMMATE, "My taxes", "o2")]})
    mailbox = FakeMailbox({OUTLOOK_LIST: text})
    with _reader(roster.owner):
        out = json.loads(_run(mailbox, OUTLOOK_LIST, select="id,subject,bodyPreview"))
    assert "toRecipients" in mailbox.calls[0]["arguments"]["select"]
    assert [i["subject"] for i in out["value"]] == ["Invoice"]
    assert roster.session.mail_ids_seen == {f"{roster.owner}:o1"}


def test_an_outlook_forward_of_an_unseen_message_is_refused(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner):
        out = _run(mailbox, "microsoft_365__create-forward-draft", messageId="ben-private")
    assert "error" in json.loads(out) and mailbox.calls == []


def test_an_outlook_attachment_download_of_an_unseen_message_is_refused(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner):
        out = _run(mailbox, "microsoft_365__download-bytes", target="/me/messages/ben-private/attachments/a1/$value")
    assert "error" in json.loads(out) and mailbox.calls == []


def test_outlook_search_is_refused(roster: SimpleNamespace) -> None:
    with _reader(roster.owner):
        out = _run(FakeMailbox({}), "microsoft_365__search-query", requests=[])
    assert "not available" in json.loads(out)["error"]


# ── who reads, and when nothing changes ──────────────────────────────────


def test_an_unknown_reader_sees_no_teammates_mail(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    with _reader(None):
        out = _run(FakeMailbox({READ: _message(OWNER, EXEC)}), READ, message_id="m1")
    assert json.loads(out)["withheld"] is True


@pytest.mark.parametrize("authenticated", [True, False])
def test_an_email_turn_reads_as_its_sender_only_when_gmail_vouches(
    roster: SimpleNamespace, authenticated: bool,
) -> None:
    session = SimpleNamespace(
        unattended=False, origin_channel="email", caller_person_id=roster.ben,
        from_web_chat=False, email_sender_authenticated=authenticated,
    )
    assert tm._reader_id(session) == (roster.ben if authenticated else None)


def test_ids_shown_to_one_reader_are_not_the_next_readers(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({SEARCH: _listing(TEAMMATE), READ: _message(TEAMMATE, EXEC)})
    with _reader(roster.ben):
        _run(mailbox, SEARCH, query="q")
    with _reader(roster.owner):
        out = _run(mailbox, READ, message_id="m1")
    assert "hasn't found" in json.loads(out)["error"]


def test_an_outlook_forward_of_a_teammates_message_is_refused(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "o2")
    mailbox = FakeMailbox({"microsoft_365__get-mail-message": json.dumps(_item(TEAMMATE, "My taxes", "o2"))})
    with _reader(roster.owner):
        out = _run(mailbox, "microsoft_365__forward-mail-message", messageId="o2",
                   body={"ToRecipients": [{"emailAddress": {"address": OWNER}}]})
    assert "private to them" in json.loads(out)["error"]
    assert [c["name"] for c in mailbox.calls] == ["microsoft_365__get-mail-message"]


def test_an_unbalanced_query_is_refused(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner):
        out = _run(mailbox, SEARCH, query='subject:"taxes OR (')
    assert "bracket" in json.loads(out)["error"] and mailbox.calls == []


def test_a_trimmed_thread_drops_its_subject(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "t1")
    text = _thread((VENDOR, EXEC, "invoice due"), (TEAMMATE, EXEC, "my bank details"))
    with _reader(roster.owner):
        out = _run(FakeMailbox({THREAD: text}), THREAD, thread_id="t1")
    assert "Taxes" not in out and "Thread ID: t1" in out


def test_a_teammate_shown_their_own_mail_keeps_the_conversation(roster: SimpleNamespace) -> None:
    roster.session.session_id = "s-ben"
    tm.allow_mail_ids(roster.session, "m1")
    with _reader(roster.ben), patch("openexecutive.memory.session_store.mark_mail_private") as mark:
        _run(FakeMailbox({READ: _message(TEAMMATE, EXEC)}), READ, message_id="m1")
    assert mark.call_args.args == ("s-ben", roster.ben)


def test_the_owner_reading_outside_mail_keeps_nothing_private(roster: SimpleNamespace) -> None:
    roster.session.session_id = "s-owner"
    tm.allow_mail_ids(roster.session, "m1")
    with _reader(roster.owner), patch("openexecutive.memory.session_store.mark_mail_private") as mark:
        _run(FakeMailbox({READ: _message(VENDOR, f"{EXEC}, {OWNER}")}), READ, message_id="m1")
    mark.assert_not_called()


def test_a_background_run_reads_as_the_principal(roster: SimpleNamespace) -> None:
    session = SimpleNamespace(unattended=True, caller_person_id=None)
    assert tm._reader_id(session) == roster.owner


def test_a_step_outside_any_session_sees_no_teammates_mail(roster: SimpleNamespace) -> None:
    current_session.set(None)
    text = _message(TEAMMATE, EXEC)
    out = asyncio.run(tm.guard_in_turn(FakeMailbox({READ: text}))(
        {"name": READ, "arguments": {"message_id": "m1"}}
    ))
    assert json.loads(out)["withheld"] is True
    vendor = _message(VENDOR, EXEC)
    out = asyncio.run(tm.guard_in_turn(FakeMailbox({READ: vendor}))(
        {"name": READ, "arguments": {"message_id": "m1"}}
    ))
    assert out == vendor


@pytest.mark.parametrize("arguments", [
    {"body": {"conditions": {"fromAddresses": [{"emailAddress": {"address": TEAMMATE}}]}}},
    {"body": {"actions": {"forwardTo": [{"emailAddress": {"address": OWNER}}]}}},
])
def test_an_outlook_rule_that_could_pass_on_a_teammates_mail_is_refused(
    roster: SimpleNamespace, arguments: dict[str, Any],
) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner):
        out = _run(mailbox, "microsoft_365__create-mail-rule", **arguments)
    assert "rule" in json.loads(out)["error"] and mailbox.calls == []


def test_the_rest_of_the_turn_is_kept_to_the_teammate(roster: SimpleNamespace) -> None:
    roster.session.session_id = "s-ben"
    tm.allow_mail_ids(roster.session, "m1")
    pinned = SimpleNamespace(person_id=roster.ben, touched_mail=False)
    with _reader(roster.ben), patch("openexecutive.memory.session_store.mark_mail_private"), \
            patch("openexecutive.delegation.settings.turn_delegation", lambda _s: pinned):
        _run(FakeMailbox({READ: _message(TEAMMATE, EXEC)}), READ, message_id="m1")
    assert pinned.touched_mail is True


def test_other_tools_pass(roster: SimpleNamespace) -> None:
    text = f"Event with {TEAMMATE}"
    with _reader(roster.owner):
        assert _run(FakeMailbox({"google_workspace__get_events": text}), "google_workspace__get_events") == text


def test_a_roster_failure_refuses(roster: SimpleNamespace) -> None:
    mailbox = FakeMailbox({})
    with _reader(roster.owner), patch.object(people_store, "list_people", side_effect=RuntimeError):
        out = _run(mailbox, READ, message_id="m1")
    assert "roster" in json.loads(out)["error"] and mailbox.calls == []


def test_a_quoted_header_line_in_a_body_does_not_count(roster: SimpleNamespace) -> None:
    tm.allow_mail_ids(roster.session, "m1")
    text = _message(TEAMMATE, EXEC, body=f"Forwarding this:\nTo: {OWNER}\nmy bank details")
    with _reader(roster.owner):
        out = _run(FakeMailbox({READ: text}), READ, message_id="m1")
    assert json.loads(out)["withheld"] is True


def test_a_teammate_named_only_in_an_outside_body_keeps_nothing_private(roster: SimpleNamespace) -> None:
    roster.session.session_id = "s-ben"
    tm.allow_mail_ids(roster.session, "m1")
    text = _message(VENDOR, EXEC, body=f"cc'ing {TEAMMATE} later")
    with _reader(roster.ben), patch("openexecutive.memory.session_store.mark_mail_private") as mark:
        _run(FakeMailbox({READ: text}), READ, message_id="m1")
    mark.assert_not_called()
