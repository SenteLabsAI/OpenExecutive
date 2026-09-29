"""What happens around a roster request (``integrations.roster_intake``).

- The sender is acknowledged with one fixed text, and the Gmail gate lets
  exactly that one send through to an address off the roster — nothing a
  model could reuse.
- The principal is told, and can answer by email from their own address
  with the one-time token (a From header alone proves nothing).
- Once answered, held messages are replayed in a fresh context, never with
  the answering turn's session (and so the principal's contacts) bound.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.alerts import store as alerts_store
from openexecutive.integrations import roster_intake
from openexecutive.memory import episodic
from openexecutive.orchestrator.mcp_gateway import MCPGateway, roster_ack_grant
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import roster_requests as rr
from openexecutive.people import store as people_store

EXEC = "exec@acme.com"
OWNER = "olivia@acme.com"
STRANGER = "annamarie@acme.com"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "intake.db"
    for module in (people_store, episodic, alerts_store):
        monkeypatch.setattr(module, "DB_PATH", path)
    people_store.initialize_db()
    alerts_store.initialize_db()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", EXEC)
    monkeypatch.setattr(roster_intake, "_REPLAYERS", {})
    people_registry.invalidate()
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    people_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    owner = people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email=OWNER, preferred_channel="email"
    )
    anna = people_store.upsert_person(full_name="Anna Smith", email="anna@acme.com")
    return SimpleNamespace(owner=owner, anna=anna)


def _gateway() -> tuple[MCPGateway, AsyncMock]:
    gateway = MCPGateway()
    session = MagicMock()
    result = MagicMock()
    result.content = [MagicMock(text="Email sent! Message ID: abc123")]
    session.call_tool = AsyncMock(return_value=result)
    gateway._session = session
    return gateway, session.call_tool


def _send(gateway: MCPGateway, **arguments: Any) -> str:
    args = {"user_google_email": EXEC, **arguments}
    return asyncio.run(
        gateway.call_tool({"name": "google_workspace__send_gmail_message", "arguments": args})
    )


# --------------------------------------------------------------------------- #
# The acknowledgement's one-shot pass through the Gmail gate
# --------------------------------------------------------------------------- #

def _ack_args(**over: Any) -> dict[str, Any]:
    return {
        "to": STRANGER, "subject": roster_intake.ACK_SUBJECT, "body": roster_intake.ACK_TEXT,
        **over,
    }


def test_the_granted_acknowledgement_reaches_an_address_off_the_roster(roster: SimpleNamespace) -> None:
    gateway, sent = _gateway()
    asyncio.run(roster_intake.send_email_ack(gateway, STRANGER))
    assert sent.await_count == 1
    args = sent.await_args.args[1]["arguments"]
    assert args["to"] == STRANGER and args["body"] == roster_intake.ACK_TEXT


@pytest.mark.parametrize("over", [
    {"body": "Anything else at all"},
    {"subject": "Re: your invoice"},
    {"to": "someone.else@evil.com"},
    {"to": f"{STRANGER}, someone.else@evil.com"},
    {"cc": "someone.else@evil.com"},
    {"quote_original": True},
    {"thread_id": "t1"},
])
def test_the_grant_admits_nothing_but_the_exact_acknowledgement(
    roster: SimpleNamespace, over: dict[str, Any]
) -> None:
    gateway, sent = _gateway()
    with roster_ack_grant(to=STRANGER, subject=roster_intake.ACK_SUBJECT, body=roster_intake.ACK_TEXT):
        result = _send(gateway, **_ack_args(**over))
    assert sent.await_count == 0
    assert "error" in json.loads(result)


def test_the_grant_is_spent_by_one_send_and_ends_with_its_scope(roster: SimpleNamespace) -> None:
    gateway, sent = _gateway()
    with roster_ack_grant(to=STRANGER, subject=roster_intake.ACK_SUBJECT, body=roster_intake.ACK_TEXT):
        _send(gateway, **_ack_args())
        second = _send(gateway, **_ack_args())
    after = _send(gateway, **_ack_args())
    assert sent.await_count == 1
    assert "error" in json.loads(second) and "error" in json.loads(after)


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #

def test_intake_holds_cards_notifies_and_acknowledges_once(roster: SimpleNamespace) -> None:
    acks: list[str] = []
    notified: list[int] = []

    async def _ack(text: str) -> None:
        acks.append(text)

    async def _notify(request: rr.RosterRequest, *, acknowledged: bool) -> str:
        assert acknowledged
        notified.append(request.id)
        return "email"

    with patch.object(roster_intake, "notify_principal", new=_notify):
        first = asyncio.run(roster_intake.intake(
            "email", STRANGER, external_id="m1", payload={"message_id": "m1"},
            preview="Hi, it's Annamarie", display_name="Annamarie Chen", send_ack=_ack,
        ))
        again = asyncio.run(roster_intake.intake(
            "email", STRANGER, external_id="m2", payload={"message_id": "m2"}, send_ack=_ack,
        ))
    assert first is not None and again is not None and first.id == again.id
    assert first.on_company_domain and first.suggested_kind == "team"
    assert acks == [roster_intake.ACK_TEXT]  # once per window
    assert notified == [first.id]  # once per request
    card = alerts_store.get_alert_by_external(rr.ALERT_SOURCE, rr.alert_external_id(first.id))
    assert card is not None and card.routed_to_person_id == roster.owner


def test_an_unacknowledged_sender_is_not_said_to_have_been_told(roster: SimpleNamespace) -> None:
    """With no acknowledgement (an email Gmail did not authenticate) the card
    and the principal's prompt say so, instead of claiming the sender was told."""
    told: list[bool] = []

    async def _notify(_request: rr.RosterRequest, *, acknowledged: bool) -> str:
        told.append(acknowledged)
        return "email"

    with patch.object(roster_intake, "notify_principal", new=_notify):
        request = asyncio.run(roster_intake.intake(
            "email", STRANGER, external_id="m1", payload={"message_id": "m1"}, send_ack=None,
        ))
    assert request is not None and told == [False]
    card = alerts_store.get_alert_by_external(rr.ALERT_SOURCE, rr.alert_external_id(request.id))
    assert card is not None
    assert "were told" not in card.body and "haven't been told anything" in card.body
    assert "I told them" not in roster_intake._chat_prompt(request, acknowledged=False)
    _subject, body = roster_intake._email_prompt(request, "RR-X", acknowledged=False)
    assert "I told them" not in body and "haven't told them anything" in body
    assert "I told them" in roster_intake._chat_prompt(request)


def test_intake_without_a_principal_holds_nothing() -> None:
    assert asyncio.run(roster_intake.intake(
        "slack", "U1", external_id="1", payload={}, send_ack=AsyncMock(),
    )) is None


def test_a_failed_acknowledgement_can_be_retried(roster: SimpleNamespace) -> None:
    async def _broken(_text: str) -> None:
        raise RuntimeError("send failed")

    with patch.object(roster_intake, "notify_principal", new=AsyncMock()):
        asyncio.run(roster_intake.intake("slack", "U1", external_id="1", payload={}, send_ack=_broken))
    assert rr.claim_ack("slack", "U1")


def test_a_chat_senders_profile_email_suggests_who_they_are(roster: SimpleNamespace) -> None:
    with patch.object(roster_intake, "notify_principal", new=AsyncMock()):
        req = asyncio.run(roster_intake.intake(
            "slack", "U_ANNA", external_id="1", payload={}, profile_email="Anna@acme.com",
        ))
    assert req is not None and req.suggested_person_id == roster.anna


@pytest.mark.parametrize(("from_addr", "headers", "automated"), [
    ("noreply@shop.com", "", True),
    ("notifications+x@github.com", "", True),
    ("jane@shop.com", "List-Unsubscribe: <mailto:u@shop.com>", True),
    ("jane@shop.com", "Auto-Submitted: auto-replied", True),
    ("jane@shop.com", "Precedence: bulk", True),
    ("jane@shop.com", "Authentication-Results: mx; dmarc=fail", True),
    ("jane@shop.com", "Auto-Submitted: no", False),
    ("jane@shop.com", "", False),
])
def test_machine_mail_gets_no_request(from_addr: str, headers: str, automated: bool) -> None:
    raw = f"Subject: hi\nFrom: {from_addr}\n{headers}\n\n--- BODY ---\nHello"
    assert roster_intake.looks_automated(raw, from_addr) is automated


def test_the_principal_is_emailed_a_token_when_email_is_their_channel(roster: SimpleNamespace) -> None:
    gateway, sent = _gateway()
    req = rr.hold("email", STRANGER, external_id="m1", payload={}, on_company_domain=True).request
    with patch.object(gw_module, "get_active_gateway", return_value=gateway), \
         patch("openexecutive.scheduler.runner.email_ready", return_value=True):
        channel = asyncio.run(roster_intake.notify_principal(req))
    assert channel == "email"
    args = sent.await_args.args[1]["arguments"]
    assert args["to"] == OWNER
    tokens = rr.find_tokens(args["subject"])
    assert tokens and rr.find_pending_by_token(tokens[0]).id == req.id
    assert rr.get_request(req.id).confirm_message_id == "abc123"


def test_an_outside_email_sender_waits_on_the_card(roster: SimpleNamespace) -> None:
    req = rr.hold("email", "someone@elsewhere.com", external_id="m1", payload={}).request
    assert asyncio.run(roster_intake.notify_principal(req)) is None


# --------------------------------------------------------------------------- #
# The principal's email answer
# --------------------------------------------------------------------------- #

def _answer_mail(from_addr: str, text: str, token: str, extra_headers: str = "") -> str:
    return (
        f"Subject: Re: Who is Annamarie? [{token}]\nFrom: {from_addr}\n{extra_headers}\n"
        f"--- BODY ---\n{text}\n\nOn Tue, Exec wrote:\n> Reference {token}\n"
    )


@pytest.fixture
def pending(roster: SimpleNamespace) -> tuple[rr.RosterRequest, str]:
    req = rr.hold(
        "email", STRANGER, external_id="m1", payload={"message_id": "m1"},
        display_name="Annamarie", on_company_domain=True,
    ).request
    return req, rr.issue_email_token(req.id)


def _answer(raw: str, from_addr: str, parsed: dict[str, Any]) -> tuple[bool, AsyncMock, list[str]]:
    gateway, sent = _gateway()
    seen: list[str] = []

    async def _parse(text: str) -> dict[str, Any]:
        seen.append(text)
        return parsed

    with patch.object(roster_intake, "_parse_answer", new=_parse), \
         patch.object(roster_intake, "schedule_replay"):
        handled = asyncio.run(roster_intake.try_email_roster_answer(gateway, raw, from_addr, "r1"))
    return handled, sent, seen


def test_the_principal_adds_them_by_email(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    raw = _answer_mail(OWNER, "That's Annamarie Chen, add her", token)
    handled, sent, seen = _answer(
        raw, OWNER, {"decision": "approve", "name": "Annamarie Chen", "kind": None}
    )
    assert handled
    done = rr.get_request(req.id)
    assert done.status == "approved" and done.resolved_via == "email"
    # Kind unsaid: a company-domain sender joins the team.
    assert people_store.get_person(done.resolved_person_id).kind == "team"
    # The parser reads the principal's own words, not the quoted request.
    assert seen == ["That's Annamarie Chen, add her"]
    assert sent.await_args.args[1]["arguments"]["to"] == OWNER


def test_this_is_someone_on_the_list_links_them(pending: tuple[rr.RosterRequest, str], roster: SimpleNamespace) -> None:
    req, token = pending
    handled, _sent, _seen = _answer(
        _answer_mail(OWNER, "that's anna", token), OWNER,
        {"decision": "link", "name": "Anna", "kind": None},
    )
    assert handled and rr.get_request(req.id).status == "linked"
    assert people_store.get_person(roster.anna).email_aliases == [STRANGER]


@pytest.mark.parametrize("from_addr", [
    "annamarie@acme.com",  # the stranger
    "olivia+x@acme.com",  # the principal by the company-domain rule, not exactly
    "anna@acme.com",  # a teammate
    "olivia@home.example",  # the principal's alias: the token went to the primary
])
def test_only_the_principals_own_address_answers(pending: tuple[rr.RosterRequest, str], from_addr: str) -> None:
    req, token = pending
    owner = people_store.find_principal_person()
    people_store.set_person_emails(owner.id, ["olivia@home.example"])
    handled, sent, _ = _answer(
        _answer_mail(from_addr, "add her", token), from_addr, {"decision": "approve", "name": "X"}
    )
    assert not handled and sent.await_count == 0
    assert rr.get_request(req.id).status == "pending"


def test_no_live_token_no_answer(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    for raw in (
        _answer_mail(OWNER, "add her", "RR-" + "B" * 20),
        f"Subject: add annamarie\nFrom: {OWNER}\n\n--- BODY ---\nadd her",
    ):
        handled, _sent, _ = _answer(raw, OWNER, {"decision": "approve", "name": "X"})
        assert not handled
    assert rr.get_request(req.id).status == "pending"


def test_a_failed_dmarc_check_is_no_answer(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    raw = _answer_mail(OWNER, "add her", token, "Authentication-Results: mx; dmarc=fail")
    handled, _sent, _ = _answer(raw, OWNER, {"decision": "approve", "name": "X"})
    assert not handled and rr.get_request(req.id).status == "pending"


def test_an_unclear_answer_leaves_them_waiting_and_says_so(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    handled, sent, _ = _answer(_answer_mail(OWNER, "hmm", token), OWNER, {"decision": "unrelated"})
    assert handled and rr.get_request(req.id).status == "pending"
    assert "still waiting" in sent.await_args.args[1]["arguments"]["body"]


def test_a_token_works_once(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    raw = _answer_mail(OWNER, "ignore", token)
    assert _answer(raw, OWNER, {"decision": "decline"})[0]
    assert rr.get_request(req.id).status == "declined"
    assert not _answer(raw, OWNER, {"decision": "approve", "name": "X"})[0]


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def test_replay_runs_in_a_fresh_context_with_no_session(roster: SimpleNamespace) -> None:
    req = rr.hold("slack", "U_NEW", external_id="1", payload={"text": "hi"}).request
    seen: list[Any] = []

    async def _replayer(message: rr.HeldMessage, _request: rr.RosterRequest) -> bool:
        seen.append((message.payload, current_session.get()))
        return True

    roster_intake.register_replayer("slack", _replayer)
    principal_turn = SimpleNamespace(from_web_chat=True, caller_person_id=roster.owner)

    async def _answer_on_the_principals_turn() -> None:
        current_session.set(principal_turn)
        await roster_intake.answer(req.id, "approve", via="web", full_name="Newbie", kind="team")
        await asyncio.sleep(0.05)

    asyncio.run(_answer_on_the_principals_turn())
    assert seen == [({"text": "hi"}, None)]


@pytest.mark.parametrize(("channel", "kind", "expected"), [
    ("slack", "team", "replayed"),
    ("slack", "contact", "dropped"),  # a contact has no chat access
    ("email", "contact", "replayed"),  # their mail takes the private contact path
])
def test_what_replays_depends_on_who_they_became(
    roster: SimpleNamespace, channel: str, kind: str, expected: str
) -> None:
    ref = "U_NEW" if channel == "slack" else "new@elsewhere.com"
    req = rr.hold(channel, ref, external_id="1", payload={"x": 1}).request
    replayer = AsyncMock(return_value=True)
    roster_intake.register_replayer(channel, replayer)
    done = rr.resolve(req.id, "approve", via="web", full_name="New Person", kind=kind)
    asyncio.run(roster_intake.replay_request(done))
    assert replayer.await_count == (1 if expected == "replayed" else 0)


def test_a_decline_replays_nothing(roster: SimpleNamespace) -> None:
    req = rr.hold("slack", "U_NEW", external_id="1", payload={}).request
    replayer = AsyncMock(return_value=True)
    roster_intake.register_replayer("slack", replayer)
    asyncio.run(roster_intake.replay_request(rr.resolve(req.id, "decline", via="web")))
    replayer.assert_not_awaited()


def test_a_sync_route_schedules_the_replay_on_the_app_loop(roster: SimpleNamespace) -> None:
    """A roster change from a worker thread (a sync route) runs its replay on
    the loop bound at boot, still in a fresh context."""
    req = rr.hold("slack", "U_NEW", external_id="1", payload={}).request
    done = rr.resolve(req.id, "approve", via="web", full_name="New", kind="team")
    seen: list[Any] = []

    async def _replayer(_m: rr.HeldMessage, _r: rr.RosterRequest) -> bool:
        seen.append(current_session.get())
        return True

    async def _main() -> None:
        roster_intake.bind_loop()
        roster_intake.register_replayer("slack", _replayer)
        ctx = contextvars.copy_context()
        ctx.run(current_session.set, SimpleNamespace(from_web_chat=True))
        await asyncio.to_thread(ctx.run, roster_intake.schedule_replay, done)
        await asyncio.sleep(0.1)

    asyncio.run(_main())
    assert seen == [None]


def test_an_automatic_reply_is_no_answer(pending: tuple[rr.RosterRequest, str]) -> None:
    req, token = pending
    raw = _answer_mail(OWNER, "I'm out until Monday", token, "Auto-Submitted: auto-replied")
    handled, sent, seen = _answer(raw, OWNER, {"decision": "approve", "name": "X"})
    assert not handled and seen == [] and sent.await_count == 0
    assert rr.get_request(req.id).status == "pending"


def test_a_display_name_never_suggests_who_they_are(roster: SimpleNamespace) -> None:
    # Anyone can call themselves "Anna Smith".
    with patch.object(roster_intake, "notify_principal", new=AsyncMock()):
        req = asyncio.run(roster_intake.intake(
            "email", "attacker@evil.example", external_id="1", payload={},
            display_name="Anna Smith",
        ))
    assert req is not None and req.suggested_person_id is None


def test_the_answer_token_is_hidden_from_every_other_read_of_the_mailbox(
    roster: SimpleNamespace,
) -> None:
    from openexecutive.orchestrator.mcp_gateway import reveal_roster_tokens

    token = "RR-" + "A2" * 10
    gateway = MCPGateway()
    session = MagicMock()
    result = MagicMock()
    result.content = [MagicMock(text=f"Subject: Who is Annamarie? [{token}]")]
    session.call_tool = AsyncMock(return_value=result)
    gateway._session = session
    call = {
        "name": "google_workspace__search_gmail_messages",
        "arguments": {"query": "in:sent", "user_google_email": EXEC},
    }
    assert token not in asyncio.run(gateway.call_tool(call))

    async def _poller_read() -> str:
        with reveal_roster_tokens():
            return await gateway.call_tool(call)

    assert token in asyncio.run(_poller_read())
