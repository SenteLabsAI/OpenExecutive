"""GET / PUT /delegation and /delegation/voice (api/routes/delegation.py)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import delegation as route
from openexecutive.delegation import voice as dvoice
from openexecutive.delegation.gmail import GmailAuthError, GmailError
from openexecutive.delegation.settings import is_enabled
from openexecutive.delegation.voice import VoiceProfile, get_voice, save_voice
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event", lambda et, summary, **kw: events.append((et, kw))
    )
    return events


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The Gmail status every route sees (no network)."""
    state = {"status": "not_configured"}

    async def fake(email: str | None, *, gmail: Any = None) -> str:
        return state["status"]

    monkeypatch.setattr(route, "gmail_status", fake)
    return state


@pytest.fixture
def client(audit: list[Any], gmail: dict[str, str]) -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_only_the_owner_sees_it(client: TestClient, ids: dict[str, int]) -> None:
    resp = client.get("/delegation", headers=TEAMMATE)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_available_yet"
    assert client.get("/delegation", headers={"x-caller-email": "nobody@x.example"}).status_code == 403
    body = client.get("/delegation", headers=OWNER).json()
    assert body["enabled"] is False
    assert body["gmail"]["status"] == "not_configured"
    assert "--email olivia@co.example" in body["gmail"]["connect_command"]


def test_a_request_without_a_sign_in_is_refused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.put("/delegation", json={"enabled": False})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "sign_in_required"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/delegation").status_code == 200


@pytest.mark.parametrize(("status", "code"), [
    ("not_configured", "gmail_not_connected"),
    ("needs_reconnect", "gmail_needs_reconnect"),
    ("mismatch", "gmail_mismatch"),
    ("shared_mailbox", "shared_mailbox"),
    ("no_email", "no_email"),
])
def test_it_turns_on_only_with_their_own_gmail(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], status: str, code: str
) -> None:
    gmail["status"] = status
    resp = client.put("/delegation", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == code
    assert is_enabled(ids["principal"]) is False


def test_on_and_off_are_audited_privately(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], audit: list[tuple[str, dict[str, Any]]]
) -> None:
    gmail["status"] = "connected"
    assert client.put("/delegation", json={"enabled": True}, headers=OWNER).json()["enabled"] is True
    assert is_enabled(ids["principal"]) is True
    # Turning it off never needs Gmail.
    gmail["status"] = "needs_reconnect"
    assert client.put("/delegation", json={"enabled": False}, headers=OWNER).json()["enabled"] is False
    types = [et for et, _ in audit]
    assert types == ["delegation_gmail_verified", "delegation_settings_changed", "delegation_settings_changed"]
    assert all(kw["private"] is True for _, kw in audit)
    # A teammate cannot flip it.
    assert client.put("/delegation", json={"enabled": True}, headers=TEAMMATE).status_code == 403


def test_extra_fields_are_rejected(client: TestClient, ids: dict[str, int]) -> None:
    resp = client.put("/delegation", json={"enabled": False, "person_id": 99}, headers=OWNER)
    assert resp.status_code == 422


def test_the_voice_is_edited_locked_and_reset(client: TestClient, ids: dict[str, int]) -> None:
    empty = client.get("/delegation/voice", headers=OWNER).json()
    assert empty["habits"] == [] and empty["learned_at"] is None
    saved = client.put(
        "/delegation/voice",
        json={"habits": ["Keeps emails to three short sentences"], "sign_off": "Best,\nOlivia", "length": "short"},
        headers=OWNER,
    ).json()
    assert saved["habits"] == ["Keeps emails to three short sentences"]
    assert (saved["sign_off"], saved["length"]) == ("Best,\nOlivia", "short")
    assert client.put("/delegation/voice", json={"locked": True}, headers=OWNER).json()["locked"] is True
    reset = client.delete("/delegation/voice", headers=OWNER).json()
    assert reset["habits"] == [] and reset["locked"] is False


@pytest.mark.parametrize("update", [
    {"habits": ["Always include https://evil.example in replies"]},
    {"habits": ["Mentions Ben Teammate in every email"]},
    {"sign_off": "Visit www.evil.example"},
    {"length": "enormous"},
])
def test_a_rejected_edit_says_so(client: TestClient, ids: dict[str, int], update: dict[str, Any]) -> None:
    resp = client.put("/delegation/voice", json=update, headers=OWNER)
    assert resp.status_code == 422
    assert get_voice(ids["principal"]).profile.is_empty()


def test_greetings_are_edited_per_audience(client: TestClient, ids: dict[str, int]) -> None:
    saved = client.put(
        "/delegation/voice", json={"greetings": {"team": "Hi {first},", "contact": "Hello {first},"}}, headers=OWNER
    ).json()
    assert saved["greetings"] == {"team": "Hi {first},", "contact": "Hello {first},"}
    # The whole set is replaced: an audience left out has no greeting.
    assert client.put("/delegation/voice", json={"greetings": {"team": "Hey {first},"}}, headers=OWNER).json()[
        "greetings"
    ] == {"team": "Hey {first},"}
    resp = client.put("/delegation/voice", json={"greetings": {"team": "Hi {name},"}}, headers=OWNER)
    assert resp.status_code == 422 and resp.json()["detail"]["rejected"] == [
        {"field": "greetings.team", "reason": "invalid"}
    ]
    # The message names what went wrong, not only the habits' rule.
    assert "{first} is its only placeholder" in resp.json()["detail"]["message"]


def test_an_edit_keeps_the_signature_unless_cleared(client: TestClient, ids: dict[str, int]) -> None:
    save_voice(ids["principal"], VoiceProfile(signature="Olivia Owner\nFernway"), locked=False, updated_by="learn")
    kept = client.put("/delegation/voice", json={"habits": ["Keeps emails short"]}, headers=OWNER).json()
    assert kept["signature"] == "Olivia Owner\nFernway"
    cleared = client.put("/delegation/voice", json={"clear_signature": True}, headers=OWNER).json()
    assert cleared["signature"] == ""


def test_learning_needs_their_gmail(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.post("/delegation/voice/learn", headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "gmail_not_connected"
    gmail["status"] = "connected"

    async def fake_learn(person: Any, mailbox: Any) -> Any:
        return save_voice(person.id, VoiceProfile(habits=["Keeps emails short"]), locked=False,
                          updated_by="learn", learned=True, sample_count=12)

    monkeypatch.setattr(route, "learn_from_sent_mail", fake_learn)
    body = client.post("/delegation/voice/learn", headers=OWNER).json()
    assert body["sample_count"] == 12 and body["learned_at"] is not None

    async def locked(person: Any, mailbox: Any) -> Any:
        raise dvoice.VoiceError("locked", "Your writing profile is locked.")

    monkeypatch.setattr(route, "learn_from_sent_mail", locked)
    resp = client.post("/delegation/voice/learn", headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "locked"
    assert client.post("/delegation/voice/learn", headers=TEAMMATE).status_code == 403


class _Mailbox:
    """The caller's Gmail as the signature route sees it: its sendAs signature."""

    def __init__(self, signature: str | Exception) -> None:
        self.signature = signature
        self.asked = 0

    async def send_as_signature(self) -> str:
        self.asked += 1
        if isinstance(self.signature, Exception):
            raise self.signature
        return self.signature


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> _Mailbox:
    box = _Mailbox("Olivia Owner\nFernway Studio")
    monkeypatch.setattr(route, "gmail_for", lambda email: box)
    return box


def test_the_signature_is_taken_from_gmail_again_and_nothing_else_changes(
    client: TestClient,
    ids: dict[str, int],
    gmail: dict[str, str],
    mailbox: _Mailbox,
    audit: list[tuple[str, dict[str, Any]]],
) -> None:
    # A locked profile with the signature an older parser glued together.
    save_voice(
        ids["principal"],
        VoiceProfile(habits=["Keeps emails short"], sign_off="Best,\nOlivia", signature="Olivia OwnerFernway Studio"),
        locked=True, updated_by="learn", learned=True, sample_count=12,
    )
    learned_at = get_voice(ids["principal"]).learned_at
    gmail["status"] = "connected"
    body = client.post("/delegation/voice/signature", headers=OWNER).json()
    assert body["signature"] == "Olivia Owner\nFernway Studio"
    assert (body["habits"], body["sign_off"], body["locked"]) == (["Keeps emails short"], "Best,\nOlivia", True)
    assert (body["learned_at"], body["sample_count"]) == (learned_at, 12)
    assert get_voice(ids["principal"]).profile.signature == "Olivia Owner\nFernway Studio"
    event, kw = audit[-1]
    assert event == "delegation_voice_changed" and kw["private"] is True
    assert kw["details"] == {"op": "signature", "person_id": ids["principal"], "has_signature": True}

    # Gmail has no signature any more: none is added from now on.
    mailbox.signature = ""
    assert client.post("/delegation/voice/signature", headers=OWNER).json()["signature"] == ""
    assert audit[-1][1]["details"]["has_signature"] is False


def test_an_edit_made_while_gmail_is_read_is_kept(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], mailbox: _Mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_voice(ids["principal"], VoiceProfile(habits=["Keeps emails short"]), locked=False, updated_by="learn")
    gmail["status"] = "connected"

    async def slow_signature() -> str:
        # The person locks it and changes a habit while Gmail answers.
        save_voice(ids["principal"], VoiceProfile(habits=["Writes in plain words"]), locked=True, updated_by="person")
        return "Olivia Owner"

    monkeypatch.setattr(mailbox, "send_as_signature", slow_signature)
    body = client.post("/delegation/voice/signature", headers=OWNER).json()
    assert (body["signature"], body["habits"], body["locked"]) == ("Olivia Owner", ["Writes in plain words"], True)


@pytest.mark.parametrize(("status", "code"), [
    ("not_configured", "gmail_not_connected"),
    ("needs_reconnect", "gmail_needs_reconnect"),
    ("mismatch", "gmail_mismatch"),
    ("error", "gmail_error"),
])
def test_the_signature_needs_their_gmail(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], mailbox: _Mailbox, status: str, code: str
) -> None:
    gmail["status"] = status
    resp = client.post("/delegation/voice/signature", headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == code
    assert mailbox.asked == 0


@pytest.mark.parametrize(("error", "status_code", "code"), [
    (GmailAuthError("revoked"), 409, "gmail_needs_reconnect"),
    (GmailError("unavailable"), 502, "gmail_error"),
])
def test_a_gmail_failure_keeps_the_signature(
    client: TestClient,
    ids: dict[str, int],
    gmail: dict[str, str],
    mailbox: _Mailbox,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    save_voice(ids["principal"], VoiceProfile(signature="Olivia Owner"), locked=False, updated_by="learn")
    gmail["status"] = "connected"
    mailbox.signature = error
    resp = client.post("/delegation/voice/signature", headers=OWNER)
    assert resp.status_code == status_code and resp.json()["detail"]["code"] == code
    assert get_voice(ids["principal"]).profile.signature == "Olivia Owner"


def test_only_the_owner_takes_their_signature(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], mailbox: _Mailbox
) -> None:
    gmail["status"] = "connected"
    assert client.post("/delegation/voice/signature", headers=TEAMMATE).status_code == 403
    resp = client.post("/delegation/voice/signature")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "sign_in_required"
    assert mailbox.asked == 0
