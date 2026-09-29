"""End to end: an unknown sender's email becomes a roster request, the
principal answers it from the web card, and the held email is replayed.

Route-level against a temp SQLite DB; the Gmail gateway and the Executive
are stubbed, so no API key and no mail leave the test.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openexecutive.integrations.email_poller as poller
from openexecutive.alerts import store as alerts_store
from openexecutive.api.routes import people as people_route
from openexecutive.integrations import roster_intake
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import roster_requests as rr
from openexecutive.people import store as people_store

EXEC = "exec@acme.com"
OWNER = {"x-caller-email": "olivia@acme.com"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    for module in (people_store, episodic, alerts_store):
        monkeypatch.setattr(module, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db()
    alerts_store.initialize_db()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", EXEC)
    monkeypatch.setattr(roster_intake, "_REPLAYERS", {})
    people_registry.invalidate()
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@acme.com")
    people_store.upsert_person(full_name="Anna Smith", email="anna@acme.com")
    yield path
    people_registry.invalidate()


RAW = (
    "Subject: Q3 numbers\nFrom: Annamarie Chen <annamarie@acme.com>\n\n"
    "--- BODY ---\nHi — I joined finance this week. Can you send the Q3 numbers?\n"
)

# The same mail read as raw MIME, with Gmail's stamp: only an authenticated
# sender is acknowledged.
RAW_MIME = (
    "\n\n--- RAW MIME ---\n"
    "Authentication-Results: mx.google.com;\r\n       spf=pass smtp.mailfrom=annamarie@acme.com;"
    "\r\n       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=acme.com\r\n"
    "From: Annamarie Chen <annamarie@acme.com>\r\nSubject: Q3 numbers\r\n\r\nHi\r\n"
)


def _receive() -> tuple[AsyncMock, list[dict[str, Any]]]:
    async def _read(request: dict[str, Any]) -> str:
        if request["arguments"].get("body_format") == "raw":
            return RAW + RAW_MIME
        return RAW

    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(side_effect=_read)
    turns: list[dict[str, Any]] = []

    async def _executive(*_a: Any, **kw: Any) -> None:
        turns.append(kw)

    with (
        patch.object(poller, "_run_executive", new=_executive),
        patch.object(poller, "_mark_read", new=AsyncMock()),
        patch.object(roster_intake, "notify_principal", new=AsyncMock(return_value=None)),
    ):
        asyncio.run(poller._handle_email(gateway, "m1", "t1", EXEC))
    return gateway, turns


def test_unknown_email_to_card_to_approval_to_replay() -> None:
    gateway, turns = _receive()

    # Held, acknowledged once, and still triaged (told it is already held).
    [request] = rr.list_requests()
    assert request.channel_ref == "annamarie@acme.com"
    assert request.on_company_domain and request.suggested_kind == "team"
    assert turns and turns[0]["held_for_roster"] is True
    sends = [
        c.args[0]["arguments"] for c in gateway.call_tool.await_args_list
        if c.args[0]["name"] == "google_workspace__send_gmail_message"
    ]
    assert [s["to"] for s in sends] == ["annamarie@acme.com"]
    assert sends[0]["body"] == roster_intake.ACK_TEXT

    # The card is on the principal's /today, private to them.
    card = alerts_store.get_alert_by_external(rr.ALERT_SOURCE, rr.alert_external_id(request.id))
    assert card is not None and "private:principal" in card.topic_tags

    replayed: list[dict[str, Any]] = []

    async def _replay(message: rr.HeldMessage, _req: rr.RosterRequest) -> bool:
        replayed.append(message.payload)
        return True

    app = FastAPI()
    app.include_router(people_route.router)
    with TestClient(app) as client:
        roster_intake.register_replayer("email", _replay)
        # Someone else cannot see or answer it.
        other = {"x-caller-email": "anna@acme.com"}
        assert client.get("/people/requests", headers=other).status_code == 404

        resp = client.post(
            f"/people/requests/{request.id}/approve",
            json={"full_name": "Annamarie Chen", "kind": "team"}, headers=OWNER,
        )
        assert resp.status_code == 200 and resp.json()["status"] == "approved"
        deadline = time.monotonic() + 2
        while not replayed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert replayed == [{"message_id": "m1", "thread_id": "t1"}]

        # One answer only.
        again = client.post(f"/people/requests/{request.id}/decline", headers=OWNER)
        assert again.status_code == 409

    person = people_store.find_person_by_address("annamarie@acme.com")
    assert person is not None and person.kind == "team"
    assert alerts_store.get_alert(card.id).status == "ack"
    # Next time they write, they are on the roster: no new request, no ack.
    gateway, turns = _receive()
    assert rr.list_requests() == []
    assert turns[0]["held_for_roster"] is False
