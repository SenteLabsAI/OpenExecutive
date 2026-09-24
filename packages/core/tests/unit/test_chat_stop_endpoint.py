"""Contract tests for `POST /chat/stop`.

These drive the endpoint directly against a hand-seeded registry, so they need
no streaming turn. The SSE side — what a flipped switch actually does to a live
turn — is covered in `test_chat_route_stop.py`.

The security property under test: an unknown id and someone else's id must be
indistinguishable (both 404), or the endpoint becomes an oracle for which turn
ids are currently live.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    chat_route._active_stops.clear()
    yield
    chat_route._active_stops.clear()


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every request to person 7 unless a test overrides it."""
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _r: 7)


def _seed(client_turn_id: str, owner: str) -> asyncio.Event:
    event = asyncio.Event()
    chat_route._active_stops[client_turn_id] = chat_route._StopEntry(
        event, owner, "t-abc123456789", time.monotonic()
    )
    return event


def test_stop_sets_the_event_for_the_owner(client: TestClient) -> None:
    event = _seed("live-turn-0001", "person:7")

    resp = client.post("/chat/stop", json={"client_turn_id": "live-turn-0001"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "stopping", "turn_id": "t-abc123456789"}
    assert event.is_set()


def test_unknown_id_is_404(client: TestClient) -> None:
    resp = client.post("/chat/stop", json={"client_turn_id": "no-such-turn-1"})
    assert resp.status_code == 404


def test_another_callers_turn_is_404_and_keeps_running(client: TestClient) -> None:
    """Someone else's live turn must look exactly like a turn that never
    existed — and must not actually be stopped."""
    event = _seed("someone-elses-1", "person:99")

    resp = client.post("/chat/stop", json={"client_turn_id": "someone-elses-1"})

    assert resp.status_code == 404
    # Indistinguishable from the unknown-id case above.
    assert resp.json()["detail"] == "No in-flight turn with that id"
    assert not event.is_set(), "the victim's turn was stopped"


def test_local_caller_owns_local_turns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No roster id and no caller header — CLI / direct curl against a local
    API, where there is no identity to separate."""
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _r: None)
    event = _seed("anon-turn-0001", "local")

    resp = client.post("/chat/stop", json={"client_turn_id": "anon-turn-0001"})

    assert resp.status_code == 200
    assert event.is_set()


def test_unrostered_users_do_not_share_ownership(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two signed-in users who are not on the People roster both resolve to
    person_id None. They must NOT therefore own each other's turns — the owner
    key falls back to the verified email, which the UI proxy stamps from the
    session and strips from client input."""
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _r: None)
    victim = _seed("victims-turn-01", "email:alice@example.com")

    resp = client.post(
        "/chat/stop",
        json={"client_turn_id": "victims-turn-01"},
        headers={"x-caller-email": "mallory@example.com"},
    )

    assert resp.status_code == 404
    assert not victim.is_set(), "an unrostered user stopped another user's turn"

    # ...and the real owner still can.
    ok = client.post(
        "/chat/stop",
        json={"client_turn_id": "victims-turn-01"},
        headers={"x-caller-email": "Alice@Example.com"},  # case-insensitive
    )
    assert ok.status_code == 200
    assert victim.is_set()


def test_unrostered_caller_cannot_stop_a_rostered_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _r: None)
    event = _seed("owned-turn-0001", "person:7")

    resp = client.post("/chat/stop", json={"client_turn_id": "owned-turn-0001"})

    assert resp.status_code == 404
    assert not event.is_set()


@pytest.mark.parametrize(
    "bad",
    [
        "short",  # under min_length
        "has space in it",
        "has_underscore_x",
        "newline\ninjection",
        "trailing-newline\n",
        "x" * 65,  # over max_length
    ],
)
def test_malformed_ids_are_rejected_at_the_edge(client: TestClient, bad: str) -> None:
    """On /chat/stop the id IS the payload, so a bad one is a bad request.

    (On /chat it is only dropped — failing a whole chat turn over an
    unstoppable id would be the wrong trade.)
    """
    resp = client.post("/chat/stop", json={"client_turn_id": bad})
    assert resp.status_code == 422


def test_clean_client_turn_id_drops_rather_than_raises() -> None:
    """The /chat side drops a bad id: it only means the turn isn't stoppable,
    which is never a reason to fail the turn itself."""
    assert chat_route._clean_client_turn_id(None) is None
    assert chat_route._clean_client_turn_id("bad id") is None
    assert chat_route._clean_client_turn_id("tiny") is None
    # `$` would match before a trailing newline; the regex uses `\Z`.
    assert chat_route._clean_client_turn_id("aaaaaaaa\n") is None
    ok = "3f2b9c1a-0000-4000-8000-abcdefabcdef"
    assert chat_route._clean_client_turn_id(ok) == ok


def test_registry_is_bounded_by_refusing_new_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the cap, new turns are refused rather than old ones evicted.

    Entries are popped when their turn ends, so the oldest entry is typically a
    long-running LIVE turn. Evicting it would silently take the Stop button
    away from the person most likely to be reaching for it.
    """
    monkeypatch.setattr(chat_route, "_STOP_REGISTRY_MAX", 4)

    events = [
        chat_route._register_stop(f"turn-{i:08d}", "person:7", f"t-{i:012d}")
        for i in range(6)
    ]

    assert len(chat_route._active_stops) == 4
    # The first four registered; the last two were refused, not swapped in.
    assert all(e is not None for e in events[:4])
    assert events[4] is None and events[5] is None
    assert "turn-00000000" in chat_route._active_stops
    assert "turn-00000005" not in chat_route._active_stops


def test_duplicate_client_turn_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second turn reusing a live id must not hijack the first one's switch."""
    first = chat_route._register_stop("shared-id-0001", "person:7", "t-first00000")
    second = chat_route._register_stop("shared-id-0001", "person:7", "t-second0000")

    assert first is not None
    assert second is None, "the second turn overwrote the first turn's entry"
    # Stop still reaches the turn that actually owns the id.
    assert chat_route._request_stop("shared-id-0001", "person:7") == "t-first00000"
    assert first.is_set()


def test_release_ignores_an_entry_owned_by_another_turn() -> None:
    """The refused second turn must not pop the first turn's entry when it
    finishes."""
    chat_route._register_stop("shared-id-0002", "person:7", "t-first00000")

    chat_route._release_stop("shared-id-0002", "t-second0000")
    assert "shared-id-0002" in chat_route._active_stops

    chat_route._release_stop("shared-id-0002", "t-first00000")
    assert "shared-id-0002" not in chat_route._active_stops


def test_release_is_idempotent_and_ignores_none() -> None:
    _seed("live-turn-0002", "person:7")
    chat_route._release_stop("live-turn-0002")
    chat_route._release_stop("live-turn-0002")
    chat_route._release_stop(None)
    assert chat_route._active_stops == {}


def test_request_stop_on_empty_registry_returns_none() -> None:
    assert chat_route._request_stop("live-turn-0003", "person:7") is None


def test_stop_endpoint_does_not_echo_the_client_id(client: TestClient) -> None:
    """The 404 body must not reflect the submitted id back to the caller."""
    resp = client.post("/chat/stop", json={"client_turn_id": "probe-id-0001"})
    assert resp.status_code == 404
    assert "probe-id-0001" not in resp.text


def test_stale_entries_are_reclaimed_on_registration() -> None:
    """A stranded entry must not hold its slot forever.

    Entries are normally popped when the turn ends. Since a full registry now
    refuses NEW turns rather than evicting live ones, an entry stranded by a
    path that never reaches that cleanup would otherwise deny somebody a Stop
    button permanently.
    """
    stale_event = asyncio.Event()
    chat_route._active_stops["stranded-000001"] = chat_route._StopEntry(
        stale_event, "person:7", "t-stranded000", time.monotonic() - 100_000
    )
    fresh = _seed("fresh-turn-0001", "person:7")

    assert chat_route._register_stop("new-turn-00001", "person:7", "t-new00000000")

    assert "stranded-000001" not in chat_route._active_stops
    # A live entry registered moments ago is untouched.
    assert "fresh-turn-0001" in chat_route._active_stops
    assert not fresh.is_set()


def test_a_stale_entry_does_not_block_reusing_its_id() -> None:
    chat_route._active_stops["recycled-00001"] = chat_route._StopEntry(
        asyncio.Event(), "person:7", "t-old00000000", time.monotonic() - 100_000
    )
    event = chat_route._register_stop("recycled-00001", "person:7", "t-new00000000")
    assert event is not None
    assert chat_route._active_stops["recycled-00001"].turn_id == "t-new00000000"
