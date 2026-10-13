"""AI disclosure (orchestrator/disclosure.py, delegation/disclosure.py): the
line on what the Executive sends, and on replies sent as a person."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import delegation as route
from openexecutive.delegation import disclosure as owner_disclosure
from openexecutive.memory import episodic
from openexecutive.orchestrator import disclosure
from openexecutive.orchestrator.mcp_gateway import _with_disclosure
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = "olivia@co.example"
HEADERS = {"x-caller-email": OWNER}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(disclosure, "_company_name", lambda: "Acme Corp")
    for var in (
        "AI_DISCLOSURE", "AI_DISCLOSURE_MAIL_TEXT", "AI_DISCLOSURE_CHAT_TEXT",
        "AI_DISCLOSURE_HANDLE_IT", "AI_DISCLOSURE_APPROVED_SENDS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXEC_DISPLAY_NAME", "Ava")
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_registry.invalidate()
    disclosure.reset()
    yield path
    people_registry.invalidate()
    disclosure.reset()


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


# ── the Executive's mail ──────────────────────────────────────────────────────


def test_the_mail_line_names_the_executive_the_company_and_the_owner() -> None:
    assert disclosure.mail_line() == (
        "Ava is an AI assistant working for Acme Corp. To reach a person, reply and ask, "
        "or write to olivia@co.example."
    )
    body = disclosure.with_mail_line("Hi Joe,\n\nThanks.\n\nAva\n", recipients=["joe@x.example"])
    assert body == f"Hi Joe,\n\nThanks.\n\nAva\n\n---\n{disclosure.mail_line()}"
    # Once: a body that already has it is left alone.
    assert disclosure.with_mail_line(body, recipients=["joe@x.example"]) == body


def test_mail_only_to_the_owner_goes_without_it() -> None:
    assert disclosure.with_mail_line("Your brief.", recipients=["Olivia@co.example"]) == "Your brief."
    assert disclosure.mail_line() in disclosure.with_mail_line("Hi", recipients=[OWNER, "joe@x.example"])


def test_html_mail_gets_an_escaped_paragraph() -> None:
    out = disclosure.with_mail_line("<p>Hi</p>", recipients=["joe@x.example"], as_html=True)
    assert out.startswith("<p>Hi</p><hr><p")
    assert "Acme Corp" in out and "<script" not in out


def test_a_rewording_must_still_say_it_is_an_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, AI_DISCLOSURE_MAIL_TEXT="Sent by Ava for Acme.")
    assert "AI assistant" in disclosure.mail_line()
    _env(monkeypatch, AI_DISCLOSURE_MAIL_TEXT="Ava is Acme's AI helper.", AI_DISCLOSURE_CHAT_TEXT="Ava (AI)")
    assert disclosure.mail_line() == "Ava is Acme's AI helper."
    assert disclosure.chat_line() == "Ava (AI)"


def test_off_nothing_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, AI_DISCLOSURE="false")
    assert disclosure.with_mail_line("Hi", recipients=["joe@x.example"]) == "Hi"
    assert disclosure.with_chat_line("Hi", "slack:s1") == "Hi"
    assert _with_disclosure("google_workspace__send_gmail_message", "google_workspace__send_gmail_message",
                            {"to": "joe@x.example", "body": "Hi"})["body"] == "Hi"


@pytest.mark.parametrize("tool", ["google_workspace__send_gmail_message", "google_workspace__draft_gmail_message"])
def test_gmail_sends_and_drafts_get_it(tool: str) -> None:
    sent = {"to": "joe@x.example", "subject": "Re: terms", "body": "Thanks."}
    out = _with_disclosure(tool, tool, sent)
    assert out["body"].endswith(disclosure.mail_line())
    assert sent["body"] == "Thanks."  # a copy: what the gates and records saw stays
    html = _with_disclosure(tool, tool, {**sent, "body": "<p>Thanks.</p>", "body_format": "html"})
    assert html["body"].startswith("<p>Thanks.</p><hr>")
    to_owner = _with_disclosure(tool, tool, {**sent, "to": OWNER})
    assert to_owner["body"] == "Thanks."


def test_outlook_sends_replies_and_drafts_get_it() -> None:
    message = {
        "subject": "Plan",
        "body": {"contentType": "Text", "content": "Attached."},
        "toRecipients": [{"emailAddress": {"address": "joe@x.example"}}],
    }
    sent = _with_disclosure("microsoft_365__send-mail", "microsoft_365__send_mail",
                            {"body": {"Message": message, "SaveToSentItems": True}})
    assert sent["body"]["SaveToSentItems"] is True
    assert sent["body"]["Message"]["body"]["content"].endswith(disclosure.mail_line())
    reply = _with_disclosure("microsoft_365__reply-mail-message", "microsoft_365__reply_mail_message",
                             {"messageId": "m1", "body": {"Comment": "Sounds good."}})
    assert reply["body"]["Comment"].endswith(disclosure.mail_line())
    draft = _with_disclosure("microsoft_365__create-draft-email", "microsoft_365__create_draft_email",
                             {"body": message})
    assert draft["body"]["body"]["content"].endswith(disclosure.mail_line())
    # Sending a draft already made sends it as it is.
    args = {"messageId": "d1"}
    assert _with_disclosure("microsoft_365__send-draft-message", "microsoft_365__send_draft_message", args) is args
    # A shape it doesn't know is left as it is.
    odd: dict[str, Any] = {"body": "plain"}
    assert _with_disclosure("microsoft_365__send-mail", "microsoft_365__send_mail", odd) is odd


# ── chat ──────────────────────────────────────────────────────────────────────


def test_chat_gets_it_once_a_day_per_conversation() -> None:
    first = disclosure.with_chat_line("Two exceptions were approved.", "slack:s1", now=1000.0)
    assert first == "Two exceptions were approved.\n\n_Ava · AI assistant_"
    assert disclosure.with_chat_line("Dana and the CFO.", "slack:s1", now=2000.0) == "Dana and the CFO."
    assert disclosure.with_chat_line("Hello.", "slack:s2", now=2000.0).endswith("_Ava · AI assistant_")
    later = 1000.0 + disclosure.CHAT_REPEAT + 1
    assert disclosure.with_chat_line("Morning.", "slack:s1", now=later).endswith("_Ava · AI assistant_")
    assert disclosure.with_chat_line("", "slack:s3") == ""


def test_agent_messages_to_a_channel_carry_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from openexecutive.orchestrator import broadcast_tools

    posted: list[str] = []

    async def post(channel_id: str, text: str) -> dict[str, Any]:
        posted.append(text)
        return {"status": "sent"}

    monkeypatch.setattr(broadcast_tools, "_post_slack_channel", post)
    asyncio.run(broadcast_tools._dispatch("slack", "C1", "Q3 burn trending high"))
    asyncio.run(broadcast_tools._dispatch("slack", "C1", "And again"))
    assert posted == ["Q3 burn trending high\n\n_Ava · AI assistant_", "And again"]


# ── replies sent as a person ──────────────────────────────────────────────────


def test_each_person_starts_from_the_install_and_can_change_theirs(monkeypatch: pytest.MonkeyPatch) -> None:
    start = owner_disclosure.get(7)
    assert start.handle_it is True and start.approved_sends is False
    owner_disclosure.set_(7, approved_sends=True, updated_by="t")
    assert owner_disclosure.get(7).approved_sends is True
    owner_disclosure.set_(8, handle_it=False, updated_by="t")
    _env(monkeypatch, AI_DISCLOSURE_HANDLE_IT="false", AI_DISCLOSURE_APPROVED_SENDS="true")
    assert owner_disclosure.get(9) == owner_disclosure.Disclosure(9, handle_it=False, approved_sends=True)
    assert owner_disclosure.get(8).handle_it is False  # their own choice stays theirs


def test_sign_adds_the_line_by_the_kind_of_send(monkeypatch: pytest.MonkeyPatch) -> None:
    line = owner_disclosure.LINE
    assert owner_disclosure.sign(7, "Thanks,\nOlivia", on_its_own=True) == f"Thanks,\nOlivia\n\n{line}"
    assert owner_disclosure.sign(7, "Thanks,\nOlivia", on_its_own=False) == "Thanks,\nOlivia"
    owner_disclosure.set_(7, approved_sends=True, updated_by="t")
    assert owner_disclosure.sign(7, "Thanks", on_its_own=False).endswith(line)
    assert owner_disclosure.sign(7, f"Thanks\n\n{line}", on_its_own=False).count(line) == 1
    _env(monkeypatch, AI_DISCLOSURE="false")
    assert owner_disclosure.sign(7, "Thanks", on_its_own=True) == "Thanks"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def status(email: str | None, *, gmail: Any = None) -> str:
        return "connected"

    monkeypatch.setattr(route, "gmail_status", status)
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_the_route_shows_and_changes_the_callers_own_switches(client: TestClient) -> None:
    body = client.get("/delegation", headers=HEADERS).json()
    assert body["disclosure"] == {
        "enabled": True, "handle_it": True, "approved_sends": False, "line": owner_disclosure.LINE,
    }
    body = client.put("/delegation/disclosure", json={"handle_it": False}, headers=HEADERS).json()
    assert body["disclosure"]["handle_it"] is False
    principal = people_store.find_principal_person()
    assert principal is not None and principal.id is not None
    assert owner_disclosure.get(principal.id).handle_it is False
    assert client.put("/delegation/disclosure", json={"other": True}, headers=HEADERS).status_code == 422


def test_turning_one_off_needs_a_caller_the_api_can_vouch_for(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OE_LOCAL_LOGIN", raising=False)
    resp = client.put("/delegation/disclosure", json={"handle_it": False}, headers=HEADERS)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "caller_signing_required"
    resp = client.put("/delegation/disclosure", json={"approved_sends": True}, headers=HEADERS)
    assert resp.status_code == 200 and resp.json()["disclosure"]["approved_sends"] is True
