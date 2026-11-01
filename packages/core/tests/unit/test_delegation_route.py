"""GET / PUT /delegation and /delegation/voice (api/routes/delegation.py)."""
from __future__ import annotations

import asyncio
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
    assert "connect-own-outlook.py --email olivia@co.example" in body["gmail"]["outlook_connect_command"]
    assert body["gmail"]["provider"] == "google"


def test_a_request_without_a_sign_in_is_refused(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.put("/delegation", json={"enabled": False})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "sign_in_required"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    assert client.get("/delegation").status_code == 200


def _roster_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from contextlib import contextmanager

    @contextmanager
    def broken(db_path: Any = None) -> Iterator[Any]:
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(people_store, "_get_conn", broken)


def test_a_roster_it_cannot_read_says_so(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _roster_down(monkeypatch)
    resp = client.get("/delegation", headers=OWNER)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "roster_unavailable"


def test_it_knows_the_caller_the_way_the_rest_of_the_app_does(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment can map a signed-in account to its person in the shared
    # caller resolver; Act as me must follow that, not look the email up again.
    monkeypatch.setattr(
        "openexecutive.api.routes.chat._resolve_caller_person_id", lambda request: ids["principal"],
    )
    resp = client.get("/delegation", headers={"x-caller-email": "signed-in@elsewhere.example"})
    assert resp.status_code == 200
    assert "--email olivia@co.example" in resp.json()["gmail"]["connect_command"]


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


def test_turning_it_off_forgets_the_emails_it_opened(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str]
) -> None:
    from openexecutive.delegation import mail_reads

    mail_reads.record(ids["principal"], "olivia@co.example", "t1", subject="Pilot", sender="Dana")
    mail_reads.record(ids["teammate"], "ben@co.example", "t2", subject="Ops", sender="Sam")
    gmail["status"] = "connected"
    client.put("/delegation", json={"enabled": True}, headers=OWNER)
    assert len(mail_reads.recent(ids["principal"], "olivia@co.example")) == 1
    client.put("/delegation", json={"enabled": False}, headers=OWNER)
    assert mail_reads.recent(ids["principal"], "olivia@co.example") == []
    assert len(mail_reads.recent(ids["teammate"], "ben@co.example")) == 1


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


def test_an_outlook_signature_is_never_overwritten(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], mailbox: _Mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outlook has no signature an app can read: the route refuses rather
    than clearing the one they have, and Settings says which mailbox it is."""
    save_voice(ids["principal"], VoiceProfile(signature="Olivia Owner"), locked=False, updated_by="person")
    gmail["status"] = "connected"
    monkeypatch.setattr(route, "credential_provider", lambda email: "microsoft")
    resp = client.post("/delegation/voice/signature", headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "signature_unavailable"
    assert mailbox.asked == 0
    assert get_voice(ids["principal"]).profile.signature == "Olivia Owner"
    assert client.get("/delegation", headers=OWNER).json()["gmail"]["provider"] == "microsoft"


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


def test_a_style_is_written_from_their_words_without_saving(
    client: TestClient, ids: dict[str, int], audit: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int, str]] = []

    async def fake_describe(person_id: int, description: str) -> dvoice.DescribedVoice:
        seen.append((person_id, description))
        return dvoice.DescribedVoice(
            profile=VoiceProfile(sign_off="Cheers,\nSam", habits=["Keeps emails short"], signature="Sam"),
            sample_reply="Hi Alex,\nThursday works.",
            dropped=[],
        )

    monkeypatch.setattr(route, "describe_voice", fake_describe)
    resp = client.post("/delegation/voice/describe", json={"description": "Short. Cheers, Sam."}, headers=OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sign_off"] == "Cheers,\nSam" and body["habits"] == ["Keeps emails short"]
    assert body["sample_reply"] == "Hi Alex,\nThursday works."
    assert "signature" not in body
    assert seen == [(ids["principal"], "Short. Cheers, Sam.")]
    assert get_voice(ids["principal"]).profile.is_empty()
    event, kw = audit[-1]
    assert event == "delegation_voice_described" and kw["private_to_person"] == ids["principal"]

    async def refused(person_id: int, description: str) -> dvoice.DescribedVoice:
        raise dvoice.VoiceError("too_many", "That's a lot of tries in an hour.")

    monkeypatch.setattr(route, "describe_voice", refused)
    resp = client.post("/delegation/voice/describe", json={"description": "Short."}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "too_many"
    assert client.post("/delegation/voice/describe", json={"description": "x"}, headers=TEAMMATE).status_code == 403
    assert client.post("/delegation/voice/describe", json={"nope": 1}, headers=OWNER).status_code == 422


# --------------------------------------------------------------------------- #
# Read my calendar
# --------------------------------------------------------------------------- #


class _Calendar:
    def __init__(self, outcome: Exception | None = None, granted: bool | None = False) -> None:
        self.outcome = outcome
        self.granted = granted
        self.asked = 0

    async def calendar_granted(self) -> bool | None:
        return self.granted

    async def list_events(self, start: Any, end: Any, *, max_results: int = 100) -> list[Any]:
        self.asked += 1
        if self.outcome is not None:
            raise self.outcome
        return []


def test_read_my_calendar_turns_on_only_once_the_calendar_opens(
    client: TestClient,
    ids: dict[str, int],
    gmail: dict[str, str],
    audit: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.delegation import calendar
    from openexecutive.delegation.gmail import CalendarNotGranted

    box = _Calendar(CalendarNotGranted("no"))
    monkeypatch.setattr(route, "gmail_for", lambda email: box)
    body = client.get("/delegation", headers=OWNER).json()
    # Not connected: whether the sign-in includes the calendar isn't known.
    assert body["calendar"] == {
        "enabled": False, "granted": None, "connect_command": body["gmail"]["connect_command"] + " --calendar",
    }
    resp = client.put("/delegation/calendar", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "act_as_me_off"
    gmail["status"] = "connected"
    assert client.get("/delegation", headers=OWNER).json()["calendar"]["granted"] is False
    client.put("/delegation", json={"enabled": True}, headers=OWNER)
    resp = client.put("/delegation/calendar", json={"enabled": True}, headers=OWNER)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "calendar_not_granted"
    box.outcome = GmailError("down")
    assert client.put("/delegation/calendar", json={"enabled": True}, headers=OWNER).status_code == 503
    assert calendar.enabled(ids["principal"]) is False
    box.outcome, box.granted = None, True
    body = client.put("/delegation/calendar", json={"enabled": True}, headers=OWNER).json()
    assert body["calendar"]["enabled"] is True and calendar.enabled(ids["principal"]) is True
    assert body["calendar"]["granted"] is True
    assert calendar.enabled(ids["teammate"]) is False
    # The mailbox gone (disconnected, or its sign-in revoked): the switch reads
    # Off, since nothing can be read, and On again once it's back.
    gmail["status"] = "not_configured"
    assert client.get("/delegation", headers=OWNER).json()["calendar"]["enabled"] is False
    assert calendar.enabled(ids["principal"]) is True
    gmail["status"] = "connected"
    assert client.get("/delegation", headers=OWNER).json()["calendar"]["enabled"] is True
    # Act as me off takes it with it, and it is audited privately each way.
    client.put("/delegation", json={"enabled": False}, headers=OWNER)
    assert calendar.enabled(ids["principal"]) is False
    changed = [kw for et, kw in audit if et == "delegation_calendar_changed"]
    assert [kw["details"]["enabled"] for kw in changed] == [True, False]
    assert all(kw["private"] is True for kw in changed)
    # Off never needs the calendar.
    box.outcome = CalendarNotGranted("no")
    assert client.put("/delegation/calendar", json={"enabled": False}, headers=OWNER).status_code == 200


# ── How far it goes as you (PUT /delegation/reach) ────────────────────────────


def _reach(client: TestClient, step: str) -> Any:
    return client.put("/delegation/reach", json={"step": step}, headers=OWNER)


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending on its own needs a caller the API can tie to the person."""
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")


def test_the_dial_climbs_one_switch_at_a_time(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], signed: None
) -> None:
    from openexecutive.delegation import handle_it, inbox

    pid = ids["principal"]
    gmail["status"] = "connected"
    assert route.reach_of(pid) == "off"
    assert _reach(client, "inbox").status_code == 200
    assert is_enabled(pid) and inbox.get_watch(pid).enabled and not handle_it.get(pid).enabled
    assert route.reach_of(pid) == "inbox"
    _reach(client, "bold")
    assert handle_it.get(pid).enabled and handle_it.get(pid).mode == "bold"
    _reach(client, "everything")
    assert route.reach_of(pid) == "everything"
    # Across the sending steps: Everything goes, the level stays the one picked.
    _reach(client, "careful")
    assert route.reach_of(pid) == "careful" and not handle_it.leading(pid)


def test_moving_down_turns_everything_above_off(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], signed: None
) -> None:
    from openexecutive.delegation import handle_it, inbox

    pid = ids["principal"]
    gmail["status"] = "connected"
    _reach(client, "everything")
    assert _reach(client, "when_asked").status_code == 200
    assert is_enabled(pid) and not inbox.get_watch(pid).enabled
    assert not handle_it.get(pid).enabled and not handle_it.leading(pid)
    _reach(client, "off")
    assert route.reach_of(pid) == "off" and not is_enabled(pid)


def test_without_a_mailbox_the_dial_stays_off(client: TestClient, ids: dict[str, int], gmail: dict[str, str]) -> None:
    resp = _reach(client, "inbox")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "gmail_not_connected"
    assert route.reach_of(ids["principal"]) == "off"


def test_sending_needs_a_signed_sign_in_but_holding_back_doesnt(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.delegation import handle_it

    pid = ids["principal"]
    gmail["status"] = "connected"
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    _reach(client, "bold")
    monkeypatch.delenv("OE_LOCAL_LOGIN")
    # Without signed sign-ins it can't go higher...
    assert _reach(client, "everything").status_code == 409
    assert route.reach_of(pid) == "bold"
    # ...but a lower level, or no sending at all, is always possible.
    assert _reach(client, "careful").status_code == 200 and handle_it.get(pid).mode == "careful"
    assert _reach(client, "inbox").status_code == 200 and route.reach_of(pid) == "inbox"


def test_everything_is_the_owners_and_not_in_training(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], signed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.delegation import training

    gmail["status"] = "connected"
    _reach(client, "balanced")
    training.set_(ids["principal"], {training.REPLIES: True}, updated_by="t")
    resp = _reach(client, "everything")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "in_training"
    assert route.reach_of(ids["principal"]) == "balanced"


def test_training_set_before_sending_holds_once_it_sends(
    client: TestClient, ids: dict[str, int], gmail: dict[str, str], signed: None
) -> None:
    from openexecutive.delegation import handle_it, training

    pid = ids["principal"]
    gmail["status"] = "connected"
    _reach(client, "inbox")
    resp = client.put("/delegation/training", json={"replies": True, "follow_ups": True}, headers=OWNER)
    assert resp.status_code == 200
    _reach(client, "careful")
    assert handle_it.get(pid).enabled
    t = training.get(pid)
    assert t.replies and t.follow_ups


def test_one_settings_change_asks_for_the_mailbox_status_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import gmail as gmail_mod

    asked: list[str] = []

    async def live(address: str, *, gmail: Any = None) -> str:
        asked.append(address)
        return "connected"

    monkeypatch.setattr(gmail_mod, "_gmail_status", live)

    async def go() -> list[str]:
        with gmail_mod.status_memo():
            with gmail_mod.status_memo():
                first = await gmail_mod.gmail_status("Owner@Example.com")
            second = await gmail_mod.gmail_status("owner@example.com")
        third = await gmail_mod.gmail_status("owner@example.com")
        return [first, second, third]

    assert asyncio.run(go()) == ["connected"] * 3
    # Once inside the block, and again outside it.
    assert asked == ["owner@example.com", "owner@example.com"]
