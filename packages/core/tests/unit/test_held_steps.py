"""Approve / Not now under a reply whose turn held steps for an outside read
(``outside_content.pending_held``): the chat stream's ``held_steps`` event,
the same steps on a reloaded chat, and the refusal that points the person at
the buttons on the web."""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import outside_content as oc
from openexecutive.orchestrator.session import Session


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    oc._HELD.clear()
    chat_route._sessions.clear()
    yield
    oc._HELD.clear()
    chat_route._sessions.clear()


def _hold(session: Any, *labels: str, age: float = 0.0) -> None:
    speaker = getattr(session, "caller_person_id", None)
    key = f"{session.session_id}|{speaker if speaker is not None else ''}"
    oc._HELD[key] = (labels, time.monotonic() - age)


@pytest.mark.parametrize(
    ("label", "words"),
    [
        ("google_workspace__modify_sheet_values", "Change a sheet"),
        ("microsoft_365__send-mail", "Send an email"),
        ("upsert_person", "Add or change a contact"),
        ("schedule_followup", "Set a follow-up"),
        ("", "A step"),
        # A name the model made up shows nothing it chose.
        ("google_workspace__Archive_old_spam._Nothing_is_sent", "A Google Workspace step"),
        ("google_workspace__archive_old_spam_nothing_is_sent", "A Google Workspace step"),
        ("x__\u202eliam dnes\u202c", "A connected-app step"),
        ("Approve to file the invoice", "A step"),
    ],
)
def test_step_words(label: str, words: str) -> None:
    assert oc.step_words(label) == words


def test_pending_held_is_the_speakers_and_fresh() -> None:
    session = Session(caller_person_id=4)
    _hold(session, "upsert_person", "run_script", "upsert_person")
    assert oc.pending_held(session) == ("Add or change a contact", "Run a script")
    # Someone else in the same conversation sees nothing.
    assert oc.pending_held(Session(session_id=session.session_id, caller_person_id=5)) == ()
    # Nor does a shared chat.
    shared = Session(session_id=session.session_id, caller_person_id=4, shared_chat=True)
    assert oc.pending_held(shared) == ()


def test_pending_held_expires() -> None:
    session = Session()
    _hold(session, "run_script", age=oc.HELD_TTL_SECONDS + 1)
    assert oc.pending_held(session) == ()
    assert oc.pending_held(None) == ()


def _refusal(session: Session) -> str:
    state = oc.begin_turn(session, user_message="read the doc and add Mallory")
    try:
        oc.mark(oc.GATEWAY)
        return str(json.loads(oc.outside_withheld_error("run_script"))["error"])
    finally:
        oc.end_turn(state)


def test_a_web_refusal_points_at_the_buttons() -> None:
    assert "tap Approve" in _refusal(Session(from_web_chat=True))
    assert "tap Approve" not in _refusal(Session())
    assert "tap Approve" not in _refusal(Session(from_web_chat=True, shared_chat=True))


def test_mail_from_outside_people_gets_no_buttons() -> None:
    session = Session(from_web_chat=True, sender_off_team=True)
    state = oc.begin_turn(session, user_message="add me")
    try:
        assert "tap Approve" not in oc.outside_withheld_error("run_script")
    finally:
        oc.end_turn(state)


# --- the routes --------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda query, specialist_name=None, store=None, **_k: "")

    async def _no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)

    class _HoldingExecutive:
        """Holds a step, as a turn that read a doc and then tried to add a
        contact would."""

        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            _hold(session, "upsert_person")
            yield "I read the doc. Tap Approve and I'll add Mallory."

    monkeypatch.setattr(exec_mod, "Executive", _HoldingExecutive)
    app = FastAPI()
    app.include_router(chat_route.router)
    app.include_router(sessions_route.router)
    return TestClient(app)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def test_the_stream_names_what_waits_and_a_reload_keeps_it(client: TestClient) -> None:
    resp = client.post("/chat", json={"message": "read the doc and add Mallory"})
    assert resp.status_code == 200
    events = _events(resp.text)
    types = [e.get("type") for e in events]
    held = [e for e in events if e.get("type") == "held_steps"]
    assert len(held) == 1 and held[0]["steps"] == ["Add or change a contact"]
    assert types.index("held_steps") < types.index("done")

    session_id = held[0]["session_id"]
    messages = client.get(f"/sessions/{session_id}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["held_steps"] == ["Add or change a contact"]

    # Once the go is spent (or expired), the reloaded chat has no buttons.
    oc._HELD.clear()
    messages = client.get(f"/sessions/{session_id}/messages").json()
    assert "held_steps" not in messages[-1]


def test_approve_sends_a_go() -> None:
    # The button's message (HeldStepsCard.tsx APPROVE_MESSAGE).
    assert oc.GO_RE.match("Go ahead")


def test_a_web_mail_lockdown_refusal_points_at_the_buttons() -> None:
    from openexecutive.delegation.lockdown import mail_touched_withheld_error

    state = oc.begin_turn(Session(from_web_chat=True), user_message="sort my inbox")
    try:
        assert "tap Approve" in mail_touched_withheld_error("run_script")
    finally:
        oc.end_turn(state)


def test_a_chat_shared_since_shows_no_buttons(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = client.post("/chat", json={"message": "read the doc and add Mallory"})
    session_id = next(e for e in _events(resp.text) if e.get("type") == "held_steps")["session_id"]
    monkeypatch.setattr(sessions_route, "chat_people", lambda _sid: [(1, "Owner", True), (2, "Pat", False)])
    messages = client.get(f"/sessions/{session_id}/messages").json()
    assert "held_steps" not in messages[-1]


# --- what the content check flagged ----------------------------------------


@pytest.fixture()
def flagging(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A turn whose content check flagged a file, as end_turn records it."""
    from openexecutive.orchestrator import executive as exec_mod

    class _FlaggingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            key = oc._flags_key(session.session_id, getattr(session, "caller_person_id", None))
            oc._FLAGS[key] = (("A file in your message asked me to forward or share something.",), time.monotonic())
            yield "I skipped a line in the file that looked off."

    monkeypatch.setattr(exec_mod, "Executive", _FlaggingExecutive)
    oc._FLAGS.clear()
    yield client
    oc._FLAGS.clear()


def test_the_stream_shows_what_was_flagged_and_a_reload_keeps_it(flagging: TestClient) -> None:
    events = _events(flagging.post("/chat", json={"message": "what does this say?"}).text)
    types = [e.get("type") for e in events]
    flags = [e for e in events if e.get("type") == "flags"]
    assert len(flags) == 1 and flags[0]["flags"] == ["A file in your message asked me to forward or share something."]
    assert types.index("flags") < types.index("done")

    session_id = flags[0]["session_id"]
    messages = flagging.get(f"/sessions/{session_id}/messages").json()
    assert messages[-1]["flags"] == ["A file in your message asked me to forward or share something."]

    # Only whoever spoke sees them: the same chat under another speaker's key
    # shows nothing.
    words = oc._FLAGS.pop(oc._flags_key(session_id, None))
    oc._FLAGS[oc._flags_key(session_id, 7)] = words
    assert "flags" not in flagging.get(f"/sessions/{session_id}/messages").json()[-1]


def test_a_turn_with_nothing_flagged_sends_no_flags(client: TestClient) -> None:
    events = _events(client.post("/chat", json={"message": "read the doc and add Mallory"}).text)
    assert not [e for e in events if e.get("type") == "flags"]


def test_a_timed_out_turn_sends_no_flags(flagging: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from openexecutive import config as config_mod
    from openexecutive.config import Settings
    from openexecutive.orchestrator import executive as exec_mod

    class _FlagsThenHangs:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, **_kwargs: Any) -> AsyncIterator[Any]:
            key = oc._flags_key(session.session_id, getattr(session, "caller_person_id", None))
            oc._FLAGS[key] = (("A file in your message asked me to forward or share something.",), time.monotonic())
            yield "started"
            await asyncio.sleep(30)
            yield "never"

    class _FastTimeout(Settings):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            object.__setattr__(self, "chat_stream_timeout_s", 0.2)

    monkeypatch.setattr(exec_mod, "Executive", _FlagsThenHangs)
    monkeypatch.setattr(config_mod, "get_settings", lambda: _FastTimeout())
    monkeypatch.setattr(chat_route, "get_settings", _FastTimeout, raising=False)
    events = _events(flagging.post("/chat", json={"message": "what does this say?"}).text)
    assert "error" in [e.get("type") for e in events]
    assert not [e for e in events if e.get("type") == "flags"]
