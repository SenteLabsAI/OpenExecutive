"""The web chat route must seed the caller's own channel refs on the session.

`schedule_followup` refuses to queue a send to a `(channel, channel_ref)` the
session has not seen. That gate is what stops an injected "email this to
attacker@evil" in an alert body from becoming a scheduled send.

The chat adapters populate `seen_channel_refs` from the inbound message.
Nothing populated it for a browser turn, and it did not show: tool handlers
read the session off `current_session`, which was `None` on every SSE step
past the first, so the gate read `seen is None` and skipped itself — web could
schedule to any address at all. Binding the session for the whole stream made
the gate reachable and it read an empty set, refusing even the principal's own
address. Seeding the caller's own refs is what makes the gate mean the same
thing on web as on every other surface.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store

_CALLER_EMAIL = "alex@example.com"


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    monkeypatch.setattr(alert_store, "DB_PATH", tmp_path / "alerts.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    alert_store.initialize_db(tmp_path / "alerts.db")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())

    from openexecutive.knowledge import retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "")

    people_store.upsert_person(
        full_name="Alex", is_principal=True, email=_CALLER_EMAIL
    )
    return tmp_path


def _schedule_from_stream_step(
    channel_ref: str, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Drive the real route; call `schedule_followup` from a later stream step."""
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.orchestrator.schedule_tools import (
        current_session,
        handle_schedule_followup,
    )

    run_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    results: list[str] = []

    class _Stub:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            current_session.set(kwargs.get("session"))
            for i in range(4):
                if i == 2:
                    results.append(await handle_schedule_followup({
                        "channel": "email",
                        "channel_ref": channel_ref,
                        "run_at": run_at,
                        "intent": "remind me",
                    }))
                yield f"chunk-{i}"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            async for chunk in self.stream_chat(**kwargs):
                yield chunk

    monkeypatch.setattr(exec_mod, "Executive", _Stub)
    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "remind me tomorrow at 9"})
    assert resp.status_code == 200
    _ = resp.text
    assert len(results) == 1
    return results[0]


def test_caller_can_schedule_to_their_own_address(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Remind me tomorrow at 9am" — the whole point of the feature."""
    result = _schedule_from_stream_step(_CALLER_EMAIL, monkeypatch)

    assert "error" not in result, result
    assert '"status": "scheduled"' in result


def test_an_address_the_caller_never_used_is_refused(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alerts are minted from inbound mail, so an alert body can ask the
    Executive to schedule a send to an address the principal never chose.
    Before the session was bound for the whole stream this was ALLOWED on
    web — the gate saw no session and skipped itself."""
    result = _schedule_from_stream_step("attacker@evil.example", monkeypatch)

    assert "error" in result
    assert "not seen in this session" in result


def test_the_session_carries_the_callers_refs(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _schedule_from_stream_step(_CALLER_EMAIL, monkeypatch)

    assert len(chat_route._sessions) == 1
    session = next(iter(chat_route._sessions.values()))
    assert ("email", _CALLER_EMAIL) in session.seen_channel_refs


def test_seeding_survives_an_unresolvable_caller(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup failure must not take down the turn — it just seeds nothing,
    which leaves the gate at its safe default of refusing."""
    monkeypatch.setattr(
        chat_route, "_resolve_caller_person_id", lambda _request: None
    )

    result = _schedule_from_stream_step(_CALLER_EMAIL, monkeypatch)

    assert "not seen in this session" in result


# --------------------------------------------------------------------- #
# Outbound reply-linkage stays off for web
#
# `_record_outbound_context` writes a row so a recipient's REPLY can be
# hydrated with the originating conversation. `inbound_hydration` quotes that
# conversation into the turn handling the reply — a turn whose user content
# the recipient authored. Before the session was bound for the whole stream
# this never fired on web (the function read `current_session` and got None).
# Binding it correctly would have switched that on for the principal's
# broadest surface as a silent side effect. It stays off pending its own
# decision.
# --------------------------------------------------------------------- #


def _send_from_stream_step(
    monkeypatch: pytest.MonkeyPatch, *, origin_channel: str = ""
) -> list[Any]:
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.orchestrator import schedule_tools

    recorded: list[Any] = []
    # `_record_outbound_context` imports this inside its own body, so the
    # module-path patch is the one that takes. Patching a `schedule_tools`
    # attribute would be dead — the name does not exist there.
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_outbound_context",
        lambda **kw: recorded.append(kw),
    )

    class _Stub:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            session = kwargs.get("session")
            session.origin_channel = origin_channel
            schedule_tools.current_session.set(session)
            for i in range(4):
                if i == 2:
                    schedule_tools._record_outbound_context(
                        channel="email",
                        channel_ref="vendor@external.example",
                        text="here is the summary",
                    )
                yield f"chunk-{i}"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            async for chunk in self.stream_chat(**kwargs):
                yield chunk

    monkeypatch.setattr(exec_mod, "Executive", _Stub)
    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "email the vendor"})
    assert resp.status_code == 200
    _ = resp.text
    return recorded


def test_a_web_send_records_no_reply_linkage(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _send_from_stream_step(monkeypatch) == []


@pytest.mark.parametrize(
    "origin_channel",
    ["", "slack", "telegram"],
    ids=["no-adapter-channel", "slack", "telegram"],
)
def test_a_non_web_send_still_records_reply_linkage(
    origin_channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must key on `from_web_chat`, not on an empty `origin_channel`.

    `origin_channel` names an INBOUND CHAT ADAPTER. The email poller, alert
    review's outbound session, the CLI, the MCP server, the scheduler and the
    unattended workflows all leave it empty while legitimately recording
    linkage — the email path both writes it here and reads it back through
    `hydrate_user_message`. An earlier draft of this guard keyed on the empty
    string and silently broke every one of them; the first parameter here is
    that regression.
    """
    from openexecutive.orchestrator import schedule_tools
    from openexecutive.orchestrator.session import Session

    recorded: list[Any] = []
    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_outbound_context",
        lambda **kw: recorded.append(kw),
    )
    # Built the way every non-web caller builds it: from_web_chat left False.
    session = Session(session_id="poller-1", origin_channel=origin_channel)
    token = schedule_tools.current_session.set(session)
    try:
        schedule_tools._record_outbound_context(
            channel="email", channel_ref="dana@example.com", text="fyi",
        )
    finally:
        schedule_tools.current_session.reset(token)

    assert len(recorded) == 1, (
        f"origin_channel={origin_channel!r} must still record linkage"
    )
    assert recorded[0]["originating_session_id"] == "poller-1"


def test_a_web_session_is_marked_as_such(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard depends on this flag, so pin that the route sets it."""
    _schedule_from_stream_step(_CALLER_EMAIL, monkeypatch)

    session = next(iter(chat_route._sessions.values()))
    assert session.from_web_chat is True


def test_a_scheduled_action_from_web_carries_the_session_id(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other symptom of the unbound session, and the one that made the
    original incident hard to trace: rows landed with `session_id` NULL, so
    the audit view could not connect a scheduled send to the turn that asked
    for it."""
    from openexecutive.memory.episodic import list_scheduled_actions

    result = _schedule_from_stream_step(_CALLER_EMAIL, monkeypatch)
    assert "error" not in result, result

    session = next(iter(chat_route._sessions.values()))
    rows = list_scheduled_actions(db_path=Path("./episodic_memory.db").resolve())
    assert len(rows) == 1
    assert rows[0].originating_session_id == session.session_id
