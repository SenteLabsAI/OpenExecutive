"""Standing facts asked for by email (``integrations.fact_confirmation``).

The principal's own authenticated email may ask for a fact, a retirement or a
company-profile edit. The request is checked exactly as in chat, then held:
nothing changes until a one-time token, emailed to the principal's own
address, comes back in their reply. A From line alone proves nothing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openexecutive.orchestrator.mcp_gateway as gw_module
from openexecutive.delegation.settings import TurnDelegation
from openexecutive.integrations import email_poller as poller
from openexecutive.integrations import fact_confirmation as fc
from openexecutive.memory import episodic, facts
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import fact_tools
from openexecutive.orchestrator.mcp_gateway import MCPGateway, hide_roster_tokens
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

EXEC = "exec@northwind.test"
OWNER = "owner@northwind.test"
SAID = "Maple House is 48 units, not 52. Also we're 42 people now."


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    path = tmp_path / "facts.db"
    for module in (people_store, episodic):
        monkeypatch.setattr(module, "DB_PATH", path)
    people_store.initialize_db()
    episodic.initialize_db(path)
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audits.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", EXEC)
    people_registry.invalidate()
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    token = current_session.set(None)
    yield audits
    current_session.reset(token)
    people_registry.invalidate()


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """A real gateway (so its recipient checks run) with its transport mocked;
    returns the transport's call_tool to inspect what was sent."""
    gw = MCPGateway()
    transport = MagicMock()
    result = MagicMock()
    result.content = [MagicMock(text="Email sent! Message ID: abc123")]
    transport.call_tool = AsyncMock(return_value=result)
    gw._session = transport
    monkeypatch.setattr(gw_module, "get_active_gateway", lambda: gw)
    return transport.call_tool


def _sent(call_tool: AsyncMock) -> list[dict[str, Any]]:
    return [
        c.args[1]["arguments"] for c in call_tool.await_args_list
        if c.args[1]["tool_name"] == "google_workspace__send_gmail_message"
    ]


def _email_turn(monkeypatch: pytest.MonkeyPatch, *, sender: str = OWNER,
                authenticated: bool = True, private: bool = False, said: str = SAID) -> SimpleNamespace:
    monkeypatch.setattr(
        "openexecutive.orchestrator.people_tools.is_principal_on_verified_surface",
        lambda session: False,
    )
    session = SimpleNamespace(
        session_id="email-thread-1", caller_person_id=1, origin_channel="", from_web_chat=False,
        unattended=False, private_to_principal=private, company_profile=None,
        email_from=sender, email_authenticated=authenticated,
        turn_delegation=TurnDelegation(speaker_text=said, session_id="email-thread-1"),
    )
    token = current_session.set(session)  # type: ignore[arg-type]
    monkeypatch.setattr("openexecutive.orchestrator.fact_tools._session", lambda: session)
    del token
    return session


def _remember() -> dict[str, Any]:
    return json.loads(asyncio.run(fact_tools.handle_remember_fact({
        "subject": "Maple House unit count", "statement": "Maple House has 48 units.",
        "previous_value": "52 units", "source_quote": "Maple House is 48 units, not 52",
    })))


def _token_from(sent: dict[str, Any]) -> str:
    [token] = facts.find_confirmation_tokens(sent["subject"])
    return token


def _reply(token: str, text: str, *, sender: str = OWNER, headers: str = "") -> str:
    return (
        f"Subject: Re: Confirm a change to what I keep as fact [{token}]\nFrom: {sender}\n{headers}\n"
        f"--- BODY ---\n{text}\n\nOn Tue, Exec wrote:\n> (Reference {token} — keep it in your reply.)\n"
    )


GMAIL_PASS = (
    "Authentication-Results: mx.google.com;\r\n       dkim=pass header.i=@northwind.test;"
    "\r\n       spf=pass smtp.mailfrom=owner@northwind.test;"
    "\r\n       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=northwind.test"
)


def _raw_mime(*headers: str, sender: str = OWNER) -> str:
    """What get_gmail_message_content(body_format="raw") returns: the printed
    headers, then the RFC 5322 message with Gmail's stamp on top."""
    lines = "\r\n".join([*headers, f"From: Olivia <{sender}>", "Subject: Re: units"])
    return f"Subject: Re: units\nFrom: {sender}\n\n--- RAW MIME ---\n{lines}\r\n\r\nCONFIRM\r\n"


def _mailbox(*headers: str, sender: str = OWNER) -> SimpleNamespace:
    """The Executive's mailbox, answering the raw read of the reply."""
    return SimpleNamespace(call_tool=AsyncMock(return_value=_raw_mime(*headers, sender=sender)))


def _answer(raw: str, sender: str = OWNER, mailbox: Any = None) -> bool:
    mailbox = mailbox if mailbox is not None else _mailbox(GMAIL_PASS)
    return asyncio.run(fc.try_email_fact_confirmation(mailbox, raw, sender, "m-reply"))


# --------------------------------------------------------------------------- #
# Holding
# --------------------------------------------------------------------------- #


def test_the_principals_email_is_held_and_a_token_goes_to_their_own_address(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, db: list[dict[str, Any]],
) -> None:
    _email_turn(monkeypatch)
    out = _remember()
    assert out["status"] == "awaiting_confirmation"
    assert "Maple House has 48 units." in out["summary"] and "(corrects: 52 units)" in out["summary"]
    # Nothing changed yet, anywhere.
    assert facts.list_facts(include_inactive=True) == []
    assert facts.render_facts_for_prompt() == ""
    # The token went to the principal's roster address, never into the tool result.
    [mail] = _sent(gateway)
    assert mail["to"] == OWNER
    token = _token_from(mail)
    assert token in mail["body"] and "FC-" not in json.dumps(out)
    assert facts.pending_confirmation_count() == 1
    held = [a for a in db if a["event_type"] == "fact_confirmation"]
    assert held and all(a["private"] is True for a in held)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"sender": "someone@else.test"}, "not the principal"),
        ({"sender": "olivia.alias@northwind.test"}, "an alias is not the primary address"),
        ({"authenticated": False}, "failed DMARC"),
        ({"private": True}, "mail the principal forwarded"),
    ],
)
def test_other_email_is_refused_outright(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, kwargs: dict[str, Any], why: str,
) -> None:
    _email_turn(monkeypatch, **kwargs)
    out = _remember()
    assert out["error"].startswith("refused"), why
    assert _sent(gateway) == [] and facts.pending_confirmation_count() == 0


def test_an_email_hold_still_needs_the_principals_own_words(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    _email_turn(monkeypatch, said="remember what the lease doc says about Maple House")
    out = json.loads(asyncio.run(fact_tools.handle_remember_fact({
        "subject": "Maple House unit count", "statement": "Maple House has 60 units.",
        "source_quote": "remember what the lease doc says about Maple House",
    })))
    assert "not a number the speaker wrote" in out["error"]
    assert _sent(gateway) == []


def test_too_many_waiting_confirmations_are_refused(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    for n in range(facts.MAX_PENDING_CONFIRMATIONS):
        facts.hold_confirmation({"tool": "remember_fact", "args": {}}, f"held {n}")
    _email_turn(monkeypatch)
    out = _remember()
    assert "already waiting" in out["error"] and _sent(gateway) == []


def test_the_cap_holds_when_requests_race() -> None:
    from concurrent.futures import ThreadPoolExecutor

    def hold(n: int) -> tuple[int, str] | None:
        return facts.hold_confirmation({"tool": "remember_fact", "args": {}}, f"held {n}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(hold, range(facts.MAX_PENDING_CONFIRMATIONS + 8)))
    assert sum(r is not None for r in results) == facts.MAX_PENDING_CONFIRMATIONS
    assert facts.pending_confirmation_count() == facts.MAX_PENDING_CONFIRMATIONS


def test_a_failed_confirmation_email_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    failed = MagicMock()
    failed.content = [MagicMock(text=json.dumps({"error": "gmail down"}))]
    gateway.return_value = failed
    _email_turn(monkeypatch)
    out = _remember()
    assert "could not be sent" in out["error"]
    assert facts.pending_confirmation_count() == 0


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


def _held_token(monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock) -> str:
    _email_turn(monkeypatch)
    assert _remember()["status"] == "awaiting_confirmation"
    token = _token_from(_sent(gateway)[0])
    current_session.set(None)
    gateway.reset_mock()
    return token


def test_confirm_applies_it_once_and_tells_the_principal(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    assert _answer(_reply(token, "CONFIRM")) is True
    [fact] = facts.list_facts()
    assert fact.statement == "Maple House has 48 units." and fact.source_channel == "email"
    assert fact.source_quote == "Maple House is 48 units, not 52"
    [done] = _sent(gateway)
    assert done["to"] == OWNER and done["body"].startswith("Done")
    # The same token again changes nothing, and gets no answer: a spent token
    # is not a reply, so it cannot make the Executive email anyone.
    gateway.reset_mock()
    mailbox = _mailbox(GMAIL_PASS)
    assert _answer(_reply(token, "confirm"), mailbox=mailbox) is False
    assert len(facts.list_facts(include_inactive=True)) == 1
    assert _sent(gateway) == [] and mailbox.call_tool.await_count == 0


def test_cancel_drops_it(monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock) -> None:
    token = _held_token(monkeypatch, gateway)
    assert _answer(_reply(token, "Cancel that, it was wrong")) is True
    assert facts.list_facts(include_inactive=True) == []
    assert _sent(gateway)[0]["body"].startswith("Cancelled")


def test_an_unclear_reply_keeps_it_waiting(monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock) -> None:
    token = _held_token(monkeypatch, gateway)
    assert _answer(_reply(token, "hmm, let me think")) is True
    assert facts.list_facts(include_inactive=True) == [] and facts.pending_confirmation_count() == 1
    assert "couldn't tell" in _sent(gateway)[0]["body"]


@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ("Confirm, no rush", "confirm"),
        ("Yes — no changes to the wording", "confirm"),
        ("> CONFIRM", "confirm"),
        ("No, don't. It's 52.", "cancel"),
        ("Cancel — yes, I changed my mind", "cancel"),
        ("Thanks, please confirm it", "confirm"),
        ("Thanks — yes, but no, wait", ""),
        ("Yes, but don't apply that", ""),
        ("Approved? No, cancel it", ""),
        ("Confirm — actually wait", ""),
        ("No. Confirm nothing.", "cancel"),
        ("Not approved", ""),
        ("Not confirmed yet", ""),
        ("I haven't confirmed this", ""),
        ("Confirm? No.", ""),
        ("Never approve that", ""),
        ("No rush, confirm", "confirm"),
        ("Please don't", "cancel"),
        ("Please don’t", "cancel"),
        ("CONFIRM\n\nSent from my iPhone. Please don't forward", "confirm"),
        ("Confirm\n-- \nOlivia Owner | Do not forward this email", "confirm"),
        ("Confirm\n\nActually wait, don't", ""),
        ("I haven’t approved it", ""),
        ("hmm, let me think", ""),
    ],
)
def test_the_opening_word_decides_a_mixed_reply(text: str, decision: str) -> None:
    assert fc._decision(text) == decision


def test_a_confirmation_that_fails_to_apply_is_reported(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)

    def boom(_action: dict[str, Any]) -> str:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fact_tools, "apply_confirmed", boom)
    assert _answer(_reply(token, "CONFIRM")) is True
    assert facts.list_facts(include_inactive=True) == []
    [told] = _sent(gateway)
    assert told["body"].startswith("I couldn't apply it") and "Maple House has 48 units." in told["body"]


def test_a_reply_from_anyone_else_is_not_an_answer(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    assert _answer(_reply(token, "CONFIRM", sender="mallory@evil.test"), "mallory@evil.test") is False
    assert facts.list_facts(include_inactive=True) == [] and _sent(gateway) == []


@pytest.mark.parametrize(
    "auth",
    [
        "Authentication-Results: mx.google.com; dmarc=fail (p=NONE) header.from=northwind.test",
        "Authentication-Results: mx.google.com; dmarc=none header.from=northwind.test",
        None,
    ],
    ids=["dmarc-fail", "dmarc-none", "no-header"],
)
def test_a_reply_gmail_did_not_authenticate_is_refused(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, auth: str | None,
) -> None:
    token = _held_token(monkeypatch, gateway)
    mailbox = _mailbox(*([auth] if auth else []))
    assert _answer(_reply(token, "CONFIRM"), mailbox=mailbox) is True
    assert facts.list_facts(include_inactive=True) == [] and facts.pending_confirmation_count() == 1
    [warning] = _sent(gateway)
    assert "DMARC check" in warning["body"] and warning["to"] == OWNER


@pytest.mark.parametrize(
    "auto",
    ["Auto-Submitted: auto-replied", "X-Auto-Response-Suppress: All",
     "Precedence: auto_reply", "Subject: Automatic reply: Confirm a change",
     "Subject: Auto: Re: Confirm a change"],
    ids=["auto-submitted", "exchange-suppress", "precedence", "ooo-subject", "auto-colon"],
)
def test_an_out_of_office_reply_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, db: list[dict[str, Any]], auto: str,
) -> None:
    """An out-of-office echoes the token from the principal's own, DMARC-passing
    address, and its text may say "confirm"; the printed headers the poller
    reads don't show Auto-Submitted, so the raw headers decide."""
    token = _held_token(monkeypatch, gateway)
    ooo = "I'm out of the office and will confirm receipt on my return."
    assert _answer(_reply(token, ooo), mailbox=_mailbox(GMAIL_PASS, auto)) is True
    assert facts.list_facts(include_inactive=True) == [] and facts.pending_confirmation_count() == 1
    assert _sent(gateway) == []
    assert any(a["details"].get("status") == "auto_reply_ignored" for a in db if "details" in a)


def test_a_vacation_reply_seen_in_the_printed_headers_is_ignored_without_a_read(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    mailbox = _mailbox(GMAIL_PASS)
    raw = _reply(token, "Away until Monday, will confirm then.", headers="Precedence: bulk")
    assert _answer(raw, mailbox=mailbox) is True
    assert facts.pending_confirmation_count() == 1 and _sent(gateway) == []
    assert mailbox.call_tool.await_count == 0


def test_a_failed_raw_read_refuses_and_tells_the_principal(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    mailbox = SimpleNamespace(call_tool=AsyncMock(side_effect=RuntimeError("gmail down")))
    assert _answer(_reply(token, "CONFIRM"), mailbox=mailbox) is True
    assert facts.pending_confirmation_count() == 1
    assert "DMARC check" in _sent(gateway)[0]["body"]


def test_a_made_up_reference_does_nothing_and_sends_nothing(gateway: AsyncMock) -> None:
    fake = "FC-" + "A" * 20
    mailbox = _mailbox()
    assert _answer(_reply(fake, "CONFIRM"), mailbox=mailbox) is False
    assert _sent(gateway) == [] and mailbox.call_tool.await_count == 0


def test_an_expired_confirmation_cannot_be_used(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, db: list[dict[str, Any]],
) -> None:
    token = _held_token(monkeypatch, gateway)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute("UPDATE fact_confirmations SET expires_at=?", (past,))
    assert _answer(_reply(token, "CONFIRM")) is False
    assert facts.list_facts(include_inactive=True) == [] and _sent(gateway) == []


# --------------------------------------------------------------------------- #
# Retiring and profile edits by email
# --------------------------------------------------------------------------- #


def test_forget_by_email_is_held_then_applied(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    row, _ = facts.record_fact(subject="Maple House unit count", statement="Maple House has 52 units.",
                               source_quote="q")
    _email_turn(monkeypatch, said="Forget the Maple House figure, the annex was sold.")
    out = json.loads(asyncio.run(fact_tools.handle_forget_fact({
        "fact_id": row.id, "rationale": "The principal said to forget it.",
        "source_quote": "Forget the Maple House figure",
    })))
    assert out["status"] == "awaiting_confirmation"
    assert facts.render_facts_for_prompt() != ""
    token = _token_from(_sent(gateway)[0])
    current_session.set(None)
    assert _answer(_reply(token, "yes, confirm")) is True
    assert facts.render_facts_for_prompt() == ""


def test_a_profile_edit_by_email_is_previewed_held_then_applied(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, tmp_path: Path,
) -> None:
    path = tmp_path / "profile.yaml"
    CompanyProfile(name="Northwind", headcount=40).save_to_yaml(path)
    monkeypatch.setattr("openexecutive.config.get_settings",
                        lambda: SimpleNamespace(company_profile_path=path, exec_email_address=EXEC))
    _email_turn(monkeypatch)
    out = json.loads(asyncio.run(fact_tools.handle_update_company_profile({
        "field": "headcount", "operation": "set", "value": "42",
        "source_quote": "we're 42 people now",
    })))
    assert out["status"] == "awaiting_confirmation"
    assert out["summary"] == "Update the company profile: Headcount: 40 → 42"
    assert CompanyProfile.load_from_yaml(path).headcount == 40
    token = _token_from(_sent(gateway)[0])
    current_session.set(None)
    assert _answer(_reply(token, "CONFIRM")) is True
    assert CompanyProfile.load_from_yaml(path).headcount == 42


def test_a_profile_edit_that_cannot_apply_is_reported_before_any_email(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, tmp_path: Path,
) -> None:
    path = tmp_path / "profile.yaml"
    CompanyProfile(name="Northwind").save_to_yaml(path)
    monkeypatch.setattr("openexecutive.config.get_settings",
                        lambda: SimpleNamespace(company_profile_path=path, exec_email_address=EXEC))
    _email_turn(monkeypatch, said="drop AWS from our vendors")
    out = json.loads(asyncio.run(fact_tools.handle_update_company_profile({
        "field": "vendors", "operation": "remove", "value": "AWS",
        "source_quote": "drop AWS from our vendors",
    })))
    assert "is not in vendors" in out["error"] and _sent(gateway) == []


# --------------------------------------------------------------------------- #
# Keeping the token from the model, and the poller's wiring
# --------------------------------------------------------------------------- #


def test_the_gateway_hides_confirmation_tokens_from_every_read() -> None:
    token = "FC-" + "B" * 20
    text = f"Subject: Confirm [{token}] and roster RR-{'C' * 20}"
    assert hide_roster_tokens(text) == "Subject: Confirm [FC-[hidden]] and roster RR-[hidden]"


def test_the_owners_own_mailbox_hides_tokens_from_the_drafting_model() -> None:
    from openexecutive.delegation.gmail import parse_message

    token = "FC-" + "D" * 20
    body = base64.urlsafe_b64encode(f"Reply CONFIRM.\n(Reference {token})".encode()).decode()
    message = parse_message({"id": "m1", "threadId": "t1", "payload": {
        "mimeType": "text/plain", "body": {"data": body},
        "headers": [{"name": "Subject", "value": f"Confirm a change [{token}]"},
                    {"name": "From", "value": EXEC}],
    }})
    assert token not in message.subject + message.text
    assert "FC-[hidden]" in message.subject and "FC-[hidden]" in message.text


@pytest.mark.parametrize(
    ("headers", "sender", "ok"),
    [
        ((GMAIL_PASS,), OWNER, True),
        (("Authentication-Results: mx.google.com; dmarc=pass header.from=northwind.test",),
         OWNER, True),
        ((), OWNER, False),
        (("Authentication-Results: mx.google.com; dmarc=none header.from=northwind.test",), OWNER, False),
        (("Authentication-Results: mx.google.com; dmarc=temperror header.from=northwind.test",),
         OWNER, False),
        (("Authentication-Results: mx.google.com; dmarc=fail header.from=northwind.test",), OWNER, False),
        # Gmail's stamp is the topmost; a pass the sender wrote lower down is theirs, not Gmail's.
        (("Authentication-Results: mx.google.com; dmarc=fail header.from=northwind.test",
          "Authentication-Results: mx.google.com; dmarc=pass header.from=northwind.test"), OWNER, False),
        (("Authentication-Results: mx.evil.test; dmarc=pass header.from=northwind.test",), OWNER, False),
        (("Authentication-Results: mx.google.com; dmarc=pass header.from=evil.test",), OWNER, False),
        ((GMAIL_PASS,), "mallory@northwind.test", False),
    ],
    ids=["gmail-pass", "one-line-pass", "no-header", "dmarc-none", "temperror", "dmarc-fail",
         "forged-pass-below", "not-gmail", "other-domain", "raw-from-differs"],
)
def test_authenticated_by_gmail(headers: tuple[str, ...], sender: str, ok: bool) -> None:
    raw = _raw_mime(*headers, sender=sender)
    assert fc.authenticated_by_gmail(raw, OWNER) is ok


@pytest.mark.parametrize(
    "stamp",
    [
        # A quoted envelope local part Gmail writes into smtp.mailfrom.
        'Authentication-Results: mx.google.com; spf=pass smtp.mailfrom="x;dmarc=pass '
        'header.from=northwind.test "@attacker.test; dmarc=fail header.from=northwind.test',
        # The same in the SPF comment, ahead of Gmail's real verdict.
        "Authentication-Results: mx.google.com; spf=pass (google.com: domain of "
        '"a;dmarc=pass header.from=northwind.test "@attacker.test designates 192.0.2.1) '
        "smtp.mailfrom=attacker.test; dmarc=fail header.from=northwind.test",
        # A comment alone, then no verdict of Gmail's at all.
        "Authentication-Results: mx.google.com; spf=pass (x;dmarc=pass "
        "header.from=northwind.test) smtp.mailfrom=attacker.test",
        # Two verdicts: never guess which one is Gmail's.
        "Authentication-Results: mx.google.com; dmarc=pass header.from=northwind.test; "
        "dmarc=fail header.from=northwind.test",
        # Unbalanced quoting or comments: unreadable, so not authenticated.
        'Authentication-Results: mx.google.com; smtp.mailfrom="x; dmarc=pass header.from=northwind.test',
        "Authentication-Results: mx.google.com; (x; dmarc=pass header.from=northwind.test",
        # Unquoted text Gmail might echo, on a From domain with no DMARC of its own.
        "Authentication-Results: mx.google.com; spf=pass smtp.mailfrom=x;dmarc=pass "
        "header.from=northwind.test @attacker.test",
    ],
    ids=["quoted-mailfrom", "quoted-in-spf-comment", "comment-only", "two-verdicts",
         "unbalanced-quote", "unbalanced-comment", "unquoted-echo"],
)
def test_sender_written_text_in_gmails_stamp_is_no_verdict(stamp: str) -> None:
    """Gmail copies the envelope sender (and DKIM tags) into its own
    Authentication-Results; a quoted string or comment there must not read
    as a DMARC pass."""
    assert fc.authenticated_by_gmail(_raw_mime(stamp), OWNER) is False


@pytest.mark.parametrize(
    "stamp",
    [
        GMAIL_PASS,
        "Authentication-Results: mx.google.com;\r\n       arc=pass (i=1 spf=pass "
        "spf.mailfrom=owner@northwind.test dmarc=pass fromdomain=northwind.test);"
        "\r\n       spf=pass (google.com: domain of owner@northwind.test designates "
        "2a00:1450:4864::12c as permitted sender) smtp.mailfrom=owner@northwind.test;"
        "\r\n       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=northwind.test",
        'Authentication-Results: mx.google.com; dkim=pass header.i=@northwind.test '
        'header.s=s1 header.b="Ab/+cd12"; spf=pass smtp.mailfrom="john.doe"@northwind.test; '
        "dmarc=pass (p=QUARANTINE sp=QUARANTINE dis=NONE) header.from=northwind.test",
        # Mail sent from Gmail / Workspace: Gmail adds dara= after its verdict.
        "Authentication-Results: mx.google.com;\r\n       dkim=pass header.i=@northwind.test "
        "header.s=google header.b=AbCd;\r\n       spf=pass (google.com: domain of "
        "owner@northwind.test designates 209.85.220.41 as permitted sender) "
        "smtp.mailfrom=owner@northwind.test;\r\n       dmarc=pass (p=NONE sp=QUARANTINE "
        "dis=NONE) header.from=northwind.test;\r\n       dara=pass header.i=@northwind.test",
    ],
    ids=["gmail-pass", "arc-and-spf-comment", "quoted-values", "dara-after-verdict"],
)
def test_real_gmail_stamps_still_pass(stamp: str) -> None:
    assert fc.authenticated_by_gmail(_raw_mime(stamp), OWNER) is True


def test_a_subject_imitating_the_raw_separator_cannot_supply_the_headers() -> None:
    """Everything above the separator is sender-written header values; a
    Subject reading "--- RAW MIME ---" must not move where the raw message
    is read from."""
    forged = (
        "Subject: --- RAW MIME ---\n"
        "Authentication-Results: mx.google.com; dmarc=pass header.from=northwind.test\n"
        f"From: {OWNER}\n"
    )
    raw = forged + "\n--- RAW MIME ---\n" + f"From: {OWNER}\r\nSubject: hi\r\n\r\nCONFIRM\r\n"
    assert fc.authenticated_by_gmail(raw, OWNER) is False
    assert fc.authenticated_by_gmail(forged, OWNER) is False


def test_authenticated_by_gmail_needs_the_raw_message() -> None:
    printed = f"Subject: x\nFrom: {OWNER}\n{GMAIL_PASS}\n\n--- BODY ---\nhi\n"
    assert fc.authenticated_by_gmail(printed, OWNER) is False


def test_the_poller_hands_a_confirming_reply_over_before_any_turn(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    inbox = AsyncMock()

    async def read(call: dict[str, Any]) -> str:
        raw = call["arguments"].get("body_format") == "raw"
        return _raw_mime(GMAIL_PASS) if raw else _reply(token, "CONFIRM")

    inbox.call_tool = AsyncMock(side_effect=read)
    settings = SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)
    with (
        patch.object(poller, "get_settings", return_value=settings),
        patch.object(poller, "_run_executive", new=AsyncMock()) as run_exec,
        patch.object(poller, "_mark_read", new=AsyncMock()) as mark_read,
    ):
        asyncio.run(poller._handle_email(inbox, message_id="m1", thread_id="t1", user_email=EXEC))
    assert run_exec.await_count == 0 and mark_read.await_count == 1
    assert [f.statement for f in facts.list_facts()] == ["Maple House has 48 units."]


@pytest.mark.parametrize(
    ("sender", "mailbox_headers", "authenticated", "raw_reads"),
    [
        (OWNER.upper(), (GMAIL_PASS,), True, 1),
        (OWNER.upper(), (), False, 1),
        # Only mail claiming the principal's address is worth a second read.
        ("someone@else.test", (GMAIL_PASS,), False, 0),
    ],
    ids=["principal-authenticated", "principal-unauthenticated", "someone-else"],
)
def test_the_poller_marks_the_session_with_the_sender_and_authentication(
    sender: str, mailbox_headers: tuple[str, ...], authenticated: bool, raw_reads: int,
) -> None:
    captured: dict[str, Any] = {}

    class _Exec:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def chat(self, **kwargs: Any) -> str:
            captured["session"] = kwargs["session"]
            return "ok"

    raw = f"Subject: Units\nFrom: Olivia <{sender}>\n\n--- BODY ---\nMaple House is 48 units.\n"
    mailbox = _mailbox(*mailbox_headers, sender=sender.lower())
    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_Exec),
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile",
              return_value=SimpleNamespace(is_empty=lambda: True)),
        patch("openexecutive.knowledge.retriever.retrieve", new=lambda **_k: ""),
        patch("openexecutive.memory.episodic.format_for_prompt", new=lambda **_: ""),
        patch.object(poller, "get_settings",
                     return_value=SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)),
        patch("openexecutive.config.get_settings",
              return_value=SimpleNamespace(exec_email_address=EXEC)),
    ):
        asyncio.run(poller._run_executive(
            gateway=mailbox, raw_email=raw, message_id="m1", thread_id="t1",  # type: ignore[arg-type]
            from_addr=sender,
        ))
    session = captured["session"]
    assert session.email_from == sender.lower()
    assert session.email_authenticated is authenticated
    assert mailbox.call_tool.await_count == raw_reads


def test_a_failed_principal_lookup_leaves_the_email_turn_unauthenticated_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Exec:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def chat(self, **kwargs: Any) -> str:
            captured["session"] = kwargs["session"]
            return "ok"

    def locked() -> str:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fc, "principal_address", locked)
    mailbox = _mailbox(GMAIL_PASS)
    raw = f"Subject: Units\nFrom: {OWNER}\n\n--- BODY ---\nMaple House is 48 units.\n"
    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_Exec),
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile",
              return_value=SimpleNamespace(is_empty=lambda: True)),
        patch("openexecutive.knowledge.retriever.retrieve", new=lambda **_k: ""),
        patch("openexecutive.memory.episodic.format_for_prompt", new=lambda **_: ""),
        patch.object(poller, "get_settings",
                     return_value=SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)),
    ):
        asyncio.run(poller._run_executive(
            gateway=mailbox, raw_email=raw, message_id="m1", thread_id="t1",  # type: ignore[arg-type]
            from_addr=OWNER,
        ))
    session = captured["session"]
    assert session.email_from == OWNER and session.email_authenticated is False
    assert mailbox.call_tool.await_count == 0
