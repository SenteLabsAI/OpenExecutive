"""Standing facts asked for by email (``integrations.fact_confirmation``).

The principal's own authenticated email may ask for a fact, a retirement or a
company-profile edit. The request is checked exactly as in chat, then held:
nothing changes until a one-time token, emailed to the principal's own
address, comes back in their reply. A From line alone proves nothing.
"""
from __future__ import annotations

import asyncio
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


def _answer(raw: str, sender: str = OWNER) -> bool:
    return asyncio.run(fc.try_email_fact_confirmation(None, raw, sender, "m-reply"))


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
    assert "not a number the principal wrote" in out["error"]
    assert _sent(gateway) == []


def test_too_many_waiting_confirmations_are_refused(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    for n in range(facts.MAX_PENDING_CONFIRMATIONS):
        facts.hold_confirmation({"tool": "remember_fact", "args": {}}, f"held {n}")
    _email_turn(monkeypatch)
    out = _remember()
    assert "already waiting" in out["error"] and _sent(gateway) == []


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
    # The same token again changes nothing.
    gateway.reset_mock()
    assert _answer(_reply(token, "confirm")) is True
    assert len(facts.list_facts(include_inactive=True)) == 1
    assert "isn't waiting any more" in _sent(gateway)[0]["body"]


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


def test_a_reply_from_anyone_else_is_not_an_answer(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    assert _answer(_reply(token, "CONFIRM", sender="mallory@evil.test"), "mallory@evil.test") is False
    assert facts.list_facts(include_inactive=True) == [] and _sent(gateway) == []


def test_a_reply_failing_dmarc_is_refused(monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock) -> None:
    token = _held_token(monkeypatch, gateway)
    raw = _reply(token, "CONFIRM", headers="Authentication-Results: mx; dmarc=fail")
    assert _answer(raw) is True
    assert facts.list_facts(include_inactive=True) == [] and facts.pending_confirmation_count() == 1
    assert "failed an authenticity check" in _sent(gateway)[0]["body"]


def test_a_made_up_reference_does_nothing_and_sends_nothing(gateway: AsyncMock) -> None:
    fake = "FC-" + "A" * 20
    raw = _reply(fake, "CONFIRM", headers="Authentication-Results: mx; dmarc=fail")
    assert _answer(raw) is False
    assert _sent(gateway) == []


def test_an_expired_confirmation_cannot_be_used(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock, db: list[dict[str, Any]],
) -> None:
    token = _held_token(monkeypatch, gateway)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute("UPDATE fact_confirmations SET expires_at=?", (past,))
    assert _answer(_reply(token, "CONFIRM")) is True
    assert facts.list_facts(include_inactive=True) == []
    assert "isn't waiting any more" in _sent(gateway)[0]["body"]


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


def test_dmarc_failed() -> None:
    assert fc.dmarc_failed("Subject: x\nAuthentication-Results: mx; dmarc=fail\n\n--- BODY ---\nhi\n")
    assert not fc.dmarc_failed("Subject: x\nAuthentication-Results: mx; dmarc=pass\n\n--- BODY ---\nhi\n")


def test_the_poller_hands_a_confirming_reply_over_before_any_turn(
    monkeypatch: pytest.MonkeyPatch, gateway: AsyncMock,
) -> None:
    token = _held_token(monkeypatch, gateway)
    inbox = AsyncMock()
    inbox.call_tool = AsyncMock(return_value=_reply(token, "CONFIRM"))
    settings = SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)
    with (
        patch.object(poller, "get_settings", return_value=settings),
        patch.object(poller, "_run_executive", new=AsyncMock()) as run_exec,
        patch.object(poller, "_mark_read", new=AsyncMock()) as mark_read,
    ):
        asyncio.run(poller._handle_email(inbox, message_id="m1", thread_id="t1", user_email=EXEC))
    assert run_exec.await_count == 0 and mark_read.await_count == 1
    assert [f.statement for f in facts.list_facts()] == ["Maple House has 48 units."]


def test_the_poller_marks_the_session_with_the_sender_and_dmarc() -> None:
    captured: dict[str, Any] = {}

    class _Exec:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def chat(self, **kwargs: Any) -> str:
            captured["session"] = kwargs["session"]
            return "ok"

    raw = (
        f"Subject: Units\nFrom: Olivia <{OWNER.upper()}>\n"
        "Authentication-Results: mx; dmarc=pass\n\n--- BODY ---\nMaple House is 48 units.\n"
    )
    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_Exec),
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile",
              return_value=SimpleNamespace(is_empty=lambda: True)),
        patch("openexecutive.knowledge.retriever.retrieve", new=lambda **_k: ""),
        patch("openexecutive.memory.episodic.format_for_prompt", new=lambda: ""),
        patch.object(poller, "get_settings",
                     return_value=SimpleNamespace(exec_email_address=EXEC, email_poll_interval_seconds=60)),
    ):
        asyncio.run(poller._run_executive(
            gateway=AsyncMock(), raw_email=raw, message_id="m1", thread_id="t1",
            from_addr=OWNER.upper(),
        ))
    session = captured["session"]
    assert session.email_from == OWNER and session.email_authenticated is True
