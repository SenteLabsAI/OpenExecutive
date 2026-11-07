"""Mail sources (integrations/mail_sources.py): mail read from somewhere other
than the Executive's own mailbox, handled under the mailbox's rules and
answered through the source."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import openexecutive.integrations.email_poller as poller
from openexecutive.integrations import mail_sources
from openexecutive.integrations.mail_sources import InboundMail, MailSource
from openexecutive.integrations.workspace.mail import InboundMessage

OWN = "exec@platform.example"
OWNER = "owner@acme.example"
TEAMMATE = "sam@acme.example"
CONTACT = "jordan@client.example"
STRANGER = "someone@elsewhere.example"

PEOPLE = {
    OWNER: SimpleNamespace(id=1, is_principal=True, full_name="Olive Owner", role="CEO"),
    TEAMMATE: SimpleNamespace(id=2, is_principal=False, full_name="Sam Team", role="Ops"),
}
CONTACTS = {CONTACT: SimpleNamespace(id=3, is_principal=False, full_name="Jordan Client", role="Buyer")}


def _resolve(addr: str, include_contacts: bool = False) -> Any:
    addr = addr.lower()
    if addr in PEOPLE:
        return PEOPLE[addr]
    return CONTACTS.get(addr) if include_contacts else None


class _Sent:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.error = error

    async def __call__(self, to: str, body: str, in_thread: bool) -> str | None:
        self.calls.append((to, body, in_thread))
        return self.error


def _mail(sender: str, *, proven: bool, send: _Sent, body: str = "Can you check the Q3 numbers?",
          attachments: Any = None, thread: str = "t1") -> InboundMail:
    async def _prove() -> bool:
        return proven

    return InboundMail(
        message=InboundMessage(
            message_id="m1", thread_id=thread, from_addr=sender, from_name="", subject="Q3",
            to=[OWN], body_text=body,
        ),
        source="relay",
        own_address=OWN,
        send_answer=send,
        prove_sender=_prove,
        read_attachments=attachments,
        hold_payload={"id": "m1"},
        session_id=f"email:relay:{thread}",
    )


@pytest.fixture
def turn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The rules' collaborators stubbed; the turn's arguments and every audit
    row are captured."""
    seen: dict[str, Any] = {"rows": [], "turns": []}

    class _Exec:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def chat(self, **kw: Any) -> str:
            seen["turns"].append(kw)
            return "Here are the Q3 numbers."

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        seen["rows"].append((event_type, summary, kw))

    from openexecutive.memory.company_profile import CompanyProfile

    settings = SimpleNamespace(exec_email_address="exec@mailbox.example", email_turn_timeout_s=30.0)
    monkeypatch.setattr(poller, "get_settings", lambda: settings)
    monkeypatch.setattr("openexecutive.audit.log_event", _log)
    monkeypatch.setattr("openexecutive.people.identity.resolve_email_sender", _resolve)
    monkeypatch.setattr("openexecutive.orchestrator.people_tools.team_member_id", lambda pid: pid)
    monkeypatch.setattr("openexecutive.integrations.fact_confirmation.principal_address", lambda: OWNER)
    monkeypatch.setattr("openexecutive.people.store.find_principal_person", lambda: PEOPLE[OWNER])
    monkeypatch.setattr("openexecutive.orchestrator.executive.Executive", _Exec)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_k: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile", lambda: CompanyProfile()
    )
    monkeypatch.setattr(
        "openexecutive.integrations.inbound_hydration.hydrate_user_message",
        lambda **kw: kw["user_message"],
    )
    monkeypatch.setattr("openexecutive.orchestrator.disclosure.enabled", lambda: False)
    async def _run() -> None:
        return None

    monkeypatch.setattr(mail_sources, "_SOURCES", {"relay": MailSource(name="relay", run=_run)})
    monkeypatch.setattr("openexecutive.scheduler.pause.is_paused", lambda: False)
    seen["intake"] = AsyncMock(return_value=SimpleNamespace(ack_sent_at=None))
    monkeypatch.setattr("openexecutive.integrations.roster_intake.intake", seen["intake"])
    return seen


def _handle(mail: InboundMail) -> str:
    return asyncio.run(mail_sources.handle_inbound(mail, gateway=None))


def test_a_proven_teammate_is_answered_in_the_thread(turn: dict[str, Any]) -> None:
    sent = _Sent()
    assert _handle(_mail(TEAMMATE, proven=True, send=sent)) == "answered"
    assert sent.calls == [(TEAMMATE, "Here are the Q3 numbers.", True)]
    [kw] = turn["turns"]
    session = kw["session"]
    assert session.origin_channel == "email" and session.session_id == "email:relay:t1"
    assert session.email_sender_authenticated is True and session.email_authenticated is False
    assert session.private_to_principal is False
    # A source's ids open nothing in the Executive's own mailbox.
    assert not session.mail_ids_seen
    assert kw["person_id"] == 2
    assert f"emailed to {TEAMMATE}" in kw["user_message"]
    assert "<untrusted_content" in kw["user_message"]
    inbound = [r for r in turn["rows"] if r[0] == "integration_inbound"]
    assert inbound and inbound[-1][2]["details"]["provider"] == "relay"
    assert [r[0] for r in turn["rows"]][-1] == "integration_outbound"


def test_an_unproven_teammate_is_answered_to_the_principal(turn: dict[str, Any]) -> None:
    sent = _Sent()
    assert _handle(_mail(TEAMMATE, proven=False, send=sent)) == "answered"
    [(to, _body, in_thread)] = sent.calls
    assert (to, in_thread) == (OWNER, False)
    [kw] = turn["turns"]
    assert kw["session"].private_to_principal is True
    assert "nothing proves it" in kw["user_message"]
    assert all(r[2].get("private") for r in turn["rows"] if r[0] == "integration_inbound")


def test_a_contact_is_summarised_for_the_principal(turn: dict[str, Any]) -> None:
    sent = _Sent()
    assert _handle(_mail(CONTACT, proven=True, send=sent)) == "answered"
    assert [c[0] for c in sent.calls] == [OWNER]
    [kw] = turn["turns"]
    assert kw["session"].private_to_principal is True
    assert "one of the principal's contacts: Jordan Client (Buyer)" in kw["user_message"]
    assert "emailed to Olive Owner, the principal" in kw["user_message"]


def test_a_stranger_is_held_silently_with_no_turn(turn: dict[str, Any]) -> None:
    sent = _Sent()
    assert _handle(_mail(STRANGER, proven=True, send=sent)) == "held for the principal"
    assert sent.calls == [] and turn["turns"] == []
    intake = turn["intake"]
    intake.assert_awaited_once()
    assert intake.await_args.kwargs["payload"] == {"id": "m1", "mail_source": "relay"}
    assert intake.await_args.kwargs["send_ack"] is None


def test_a_stranger_the_queue_refuses_is_logged_as_not_held(turn: dict[str, Any]) -> None:
    turn["intake"].return_value = None
    sent = _Sent()
    outcome = _handle(_mail(STRANGER, proven=True, send=sent))
    assert outcome.startswith("not held")
    assert sent.calls == [] and turn["turns"] == []
    [row] = [r for r in turn["rows"] if r[0] == "integration_inbound"]
    assert row[2]["details"]["outcome"] == "not_held" and "not held" in row[1]


def test_a_stranger_is_never_answered_when_the_privacy_check_fails(
    turn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(poller, "_private_to_principal_mail", _boom)
    sent = _Sent()
    assert _handle(_mail(STRANGER, proven=True, send=sent)) == "held for the principal"
    assert sent.calls == [] and turn["turns"] == []
    assert all(r[2].get("private") for r in turn["rows"])


def test_a_contact_gone_before_the_turn_is_answered_to_no_one(
    turn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _fading(addr: str, include_contacts: bool = False) -> Any:
        calls["n"] += 1
        return _resolve(addr, include_contacts) if calls["n"] <= 4 else None

    monkeypatch.setattr("openexecutive.people.identity.resolve_email_sender", _fading)
    sent = _Sent()
    assert _handle(_mail(CONTACT, proven=True, send=sent)) == "no one to answer"
    assert sent.calls == [] and turn["turns"] == []


def test_nothing_is_done_while_paused(turn: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.scheduler.pause.is_paused", lambda: True)
    sent = _Sent()
    assert _handle(_mail(TEAMMATE, proven=True, send=sent)) == poller.PAUSED
    assert sent.calls == [] and turn["turns"] == [] and turn["rows"] == []


def test_an_unregistered_source_is_refused(turn: dict[str, Any]) -> None:
    mail = _mail(TEAMMATE, proven=True, send=_Sent())
    mail.source = "typo"
    with pytest.raises(ValueError):
        _handle(mail)
    assert turn["turns"] == []


def test_the_principals_proven_mail_is_theirs(turn: dict[str, Any]) -> None:
    sent = _Sent()
    _handle(_mail(OWNER, proven=True, send=sent))
    assert turn["turns"][0]["session"].email_authenticated is True
    assert sent.calls[0][0] == OWNER


def test_the_principals_unproven_mail_is_not(turn: dict[str, Any]) -> None:
    _handle(_mail(OWNER, proven=False, send=_Sent()))
    assert turn["turns"][0]["session"].email_authenticated is False


def test_mail_from_the_address_itself_is_left_alone(turn: dict[str, Any]) -> None:
    sent = _Sent()
    assert _handle(_mail(OWN, proven=True, send=sent)) == "skipped"
    assert sent.calls == [] and turn["turns"] == [] and turn["rows"] == []


def test_attachments_are_read_for_a_known_sender_only(turn: dict[str, Any]) -> None:
    reader = AsyncMock(return_value="[Attached q3.csv]\nrevenue,10")
    _handle(_mail(TEAMMATE, proven=True, send=_Sent(), attachments=reader))
    assert "--- ATTACHMENT TEXT ---\n[Attached q3.csv]" in turn["turns"][0]["user_message"]
    tokened = AsyncMock(return_value="Reply with RR-ABCDEFGHJKMNPQRSTVWX to confirm")
    _handle(_mail(TEAMMATE, proven=True, send=_Sent(), attachments=tokened))
    assert "RR-ABCDEFGHJKMNPQRSTVWX" not in turn["turns"][-1]["user_message"]
    stranger_reader = AsyncMock(return_value="x")
    _handle(_mail(STRANGER, proven=True, send=_Sent(), attachments=stranger_reader))
    stranger_reader.assert_not_awaited()


def test_a_sources_images_reach_the_turn(turn: dict[str, Any]) -> None:
    from openexecutive.integrations.email_attachments import EmailAttachments

    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
    reader = AsyncMock(return_value=EmailAttachments("[Attached q3.csv]", [block]))
    _handle(_mail(TEAMMATE, proven=True, send=_Sent(), attachments=reader))
    assert turn["turns"][0]["attachment_blocks"] == [block]
    assert "--- ATTACHMENT TEXT ---\n[Attached q3.csv]" in turn["turns"][0]["user_message"]


def test_a_failed_send_is_the_outcome_and_audited(turn: dict[str, Any]) -> None:
    outcome = _handle(_mail(TEAMMATE, proven=True, send=_Sent(error="relay refused")))
    assert outcome == "answer not sent: relay refused"
    assert turn["rows"][-1][0] == "integration_outbound_blocked"


def test_a_proof_that_raises_reads_as_unproven(turn: dict[str, Any]) -> None:
    sent = _Sent()
    mail = _mail(TEAMMATE, proven=True, send=sent)

    async def _boom() -> bool:
        raise RuntimeError("no verdict")

    mail.prove_sender = _boom
    _handle(mail)
    assert sent.calls[0][0] == OWNER


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, MailSource]:
    sources: dict[str, MailSource] = {}
    monkeypatch.setattr(mail_sources, "_SOURCES", sources)
    monkeypatch.setattr("openexecutive.integrations.roster_intake._REPLAYERS", {})
    return sources


def test_only_enabled_sources_start(registry: dict[str, MailSource]) -> None:
    ran: list[str] = []

    def _source(name: str, enabled: Any) -> MailSource:
        async def _run() -> None:
            ran.append(name)

        return MailSource(name=name, run=_run, enabled=enabled)

    def _boom() -> bool:
        raise RuntimeError("misconfigured")

    mail_sources.register_mail_source(_source("on", lambda: True))
    mail_sources.register_mail_source(_source("off", lambda: False))
    mail_sources.register_mail_source(_source("broken", _boom))

    async def _start() -> None:
        tasks = mail_sources.start_mail_sources()
        await asyncio.gather(*tasks)

    asyncio.run(_start())
    assert ran == ["on"]
    from openexecutive.integrations import roster_intake

    assert roster_intake._REPLAYERS["email"] is mail_sources.replay_held_mail


def test_nothing_starts_when_nothing_is_registered(registry: dict[str, MailSource]) -> None:
    assert mail_sources.start_mail_sources() == []


@pytest.mark.parametrize("name", ["", "google", "microsoft"])
def test_a_source_cant_take_the_mailboxs_name(registry: dict[str, MailSource], name: str) -> None:
    async def _run() -> None:
        return None

    with pytest.raises(ValueError):
        mail_sources.register_mail_source(MailSource(name=name, run=_run))


def test_held_mail_goes_back_to_what_read_it(registry: dict[str, MailSource]) -> None:
    replayed: list[dict[str, Any]] = []

    async def _run() -> None:
        return None

    async def _replay(payload: dict[str, Any]) -> bool:
        replayed.append(payload)
        return True

    mail_sources.register_mail_source(MailSource(name="relay", run=_run, replay=_replay))
    held = SimpleNamespace(payload={"id": "m1", "mail_source": "relay"})
    assert asyncio.run(mail_sources.replay_held_mail(held, None)) is True
    assert replayed == [{"id": "m1", "mail_source": "relay"}]

    gone = SimpleNamespace(payload={"id": "m2", "mail_source": "removed"})
    assert asyncio.run(mail_sources.replay_held_mail(gone, None)) is False

    mailbox = SimpleNamespace(payload={"message_id": "g1", "thread_id": "t1"})
    with patch.object(poller, "replay_held_email", new=AsyncMock(return_value=True)) as replay:
        assert asyncio.run(mail_sources.replay_held_mail(mailbox, None)) is True
    replay.assert_awaited_once()


def test_a_source_session_never_shares_the_mailboxs(turn: dict[str, Any]) -> None:
    mail = _mail(TEAMMATE, proven=True, send=_Sent())
    mail.session_id = ""
    _handle(mail)
    assert turn["turns"][0]["session"].session_id == "email:relay:t1"


def test_a_crashed_source_is_logged(
    registry: dict[str, MailSource], caplog: pytest.LogCaptureFixture
) -> None:
    async def _run() -> None:
        raise RuntimeError("network down")

    mail_sources.register_mail_source(MailSource(name="relay", run=_run))

    async def _start() -> None:
        tasks = mail_sources.start_mail_sources()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    # The app's logging setup stops ``openexecutive`` loggers propagating, so
    # once any test has built the app, caplog only sees what reaches it here.
    mail_sources.logger.addHandler(caplog.handler)
    try:
        asyncio.run(_start())
    finally:
        mail_sources.logger.removeHandler(caplog.handler)
    assert "mail-source-relay stopped" in caplog.text



def test_the_next_turn_on_a_thread_sees_what_was_mailed_on_it(turn: dict[str, Any]) -> None:
    # With only the quoted chain to go on, a later turn could say there was
    # no draft right after the first answer sent it.
    _handle(_mail(TEAMMATE, proven=True, send=_Sent()))
    assert "EARLIER REPLIES" not in turn["turns"][0]["user_message"]
    _handle(_mail(TEAMMATE, proven=True, send=_Sent()))
    message = turn["turns"][1]["user_message"]
    assert "--- YOUR EARLIER REPLIES ON THIS THREAD ---" in message
    assert "Here are the Q3 numbers." in message.split("EARLIER REPLIES ON THIS THREAD")[1]
    assert 'source="earlier_reply"' in message


def test_only_a_proven_sender_sees_what_went_to_them_on_other_threads(turn: dict[str, Any]) -> None:
    # A forged From must not read back what was mailed to the real person.
    _handle(_mail(TEAMMATE, proven=True, send=_Sent()))
    _handle(_mail(TEAMMATE, proven=False, send=_Sent(), thread="t2"))
    assert "OTHER THREADS" not in turn["turns"][1]["user_message"]
    _handle(_mail(TEAMMATE, proven=True, send=_Sent(), thread="t3"))
    message = turn["turns"][2]["user_message"]
    assert "--- WHAT YOU SENT THIS PERSON ON OTHER THREADS ---" in message
    assert "Here are the Q3 numbers." in message.split("ON OTHER THREADS")[1]


def test_an_answer_redirected_to_the_principal_is_not_the_threads(turn: dict[str, Any]) -> None:
    # An unproven sender's answer goes to the principal, out of the thread.
    _handle(_mail(TEAMMATE, proven=False, send=_Sent()))
    _handle(_mail(TEAMMATE, proven=False, send=_Sent()))
    _handle(_mail(TEAMMATE, proven=True, send=_Sent()))
    assert all("EARLIER REPLIES" not in kw["user_message"] for kw in turn["turns"])


def test_only_what_went_to_that_sender_on_that_thread_is_shown(tmp_path: Any) -> None:
    from openexecutive.integrations import email_replies

    db = tmp_path / "r.db"
    email_replies.record("email:t1", "ann@example.com", "To Ann", db_path=db)
    email_replies.record("email:t1", "boss@example.com", "To the boss", db_path=db)
    email_replies.record("email:t2", "ann@example.com", "Another thread", db_path=db)
    for i in range(email_replies.KEEP + 2):
        email_replies.record("email:t3", "ann@example.com", f"reply {i}", db_path=db)
    shown = email_replies.block("email:t1", "Ann@Example.com", db_path=db)
    assert "To Ann" in shown
    assert "To the boss" not in shown and "Another thread" not in shown
    assert [b for _w, b in email_replies.recent("email:t3", "ann@example.com", db_path=db)] == [
        f"reply {i}" for i in range(email_replies.KEEP - 1, email_replies.KEEP + 2)
    ]
    # A thread with nothing on it shows only what went to Ann elsewhere.
    assert "ON THIS THREAD" not in email_replies.block("email:none", "ann@example.com", db_path=db)
    assert email_replies.block("email:none", "nobody@example.com", db_path=db) == ""


def test_knowledge_is_searched_for_what_the_sender_wrote() -> None:
    raw = (
        "Subject: Re: launch email\nFrom: Sam <sam@example.com>\nTo: exec@example.com\n"
        "Date: Thu, 5 Nov 2026 22:47:00 -0500\nMessage-ID: <x@example.com>\n\n--- BODY ---\n"
        "Send me the current email we are going to use?\n\n"
        "On Thu, Nov 5, 2026 at 10:29 PM Exec <exec@example.com> wrote:\n> Understood.\n"
    )
    query = poller._retrieval_query(raw)
    # A reply searches what was asked, not the old subject.
    assert query == "Send me the current email we are going to use?"
    assert "Message-ID" not in query and "Understood" not in query
    assert poller._retrieval_query("") == ""
    # A new email keeps its subject, without "Fwd:".
    fresh = raw.replace("Subject: Re: launch email", "Subject: Fwd: Re: Proposal")
    assert poller._retrieval_query(fresh).startswith("Proposal\nSend me")
    fresh = raw.replace("Subject: Re: launch email", "Subject: Pricing")
    assert poller._retrieval_query(fresh).startswith("Pricing\nSend me")
