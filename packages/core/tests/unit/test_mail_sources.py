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
          attachments: Any = None) -> InboundMail:
    async def _prove() -> bool:
        return proven

    return InboundMail(
        message=InboundMessage(
            message_id="m1", thread_id="t1", from_addr=sender, from_name="", subject="Q3",
            to=[OWN], body_text=body,
        ),
        source="relay",
        own_address=OWN,
        send_answer=send,
        prove_sender=_prove,
        read_attachments=attachments,
        hold_payload={"id": "m1"},
        session_id="email:relay:t1",
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
