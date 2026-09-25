"""Unit tests for openexecutive.orchestrator.people_tools.

These tools let the Executive add/update/archive people and assign
department heads from inside a chat turn (no UI round-trip) — for the
principal only, on a surface that verified it is them.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from openexecutive.agents import overrides as agent_overrides
from openexecutive.departments import registry as dept_registry
from openexecutive.departments import store as dept_store
from openexecutive.memory import episodic as episodic_module
from openexecutive.orchestrator.people_tools import (
    handle_archive_person,
    handle_list_people,
    handle_set_department_head,
    handle_upsert_person,
)
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def shared_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """All three stores point at the same tmp SQLite file, matching prod."""
    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic_module, "DB_PATH", db_path)
    # `agents.overrides` does `from ...episodic import DB_PATH`, so it has its
    # own module-level binding that must also be patched.
    monkeypatch.setattr(agent_overrides, "DB_PATH", db_path)
    people_store.initialize_db()
    dept_store.initialize_db()
    agent_overrides.initialize_overrides_db()
    people_registry.invalidate()
    dept_registry.invalidate()
    return db_path


@pytest.fixture(autouse=True)
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the tools' audit rows instead of writing ./episodic_memory.db."""
    calls: list[dict[str, Any]] = []

    def _fake_log_event(event_type: str, summary: str, **kwargs: Any) -> None:
        calls.append({"event_type": event_type, "summary": summary, **kwargs})

    monkeypatch.setattr("openexecutive.audit.log_event", _fake_log_event)
    return calls


@pytest.fixture(autouse=True)
def owner_id(shared_db: Path) -> Iterator[int]:
    """Run every call as the principal in the signed-in web chat — a turn
    that may change the roster. Tests of refusals rebind with `_turn`."""
    pid = people_store.upsert_person(full_name="Owner Olivia", is_principal=True)
    people_registry.invalidate()
    token = current_session.set(Session(from_web_chat=True, caller_person_id=pid))
    yield pid
    current_session.reset(token)


@contextmanager
def _turn(session: Session | None) -> Iterator[None]:
    token = current_session.set(session)
    try:
        yield
    finally:
        current_session.reset(token)


def _call(coro_fn, payload: dict) -> dict:
    return json.loads(asyncio.run(coro_fn(payload)))


# --------------------------------------------------------------------------- #
# upsert_person
# --------------------------------------------------------------------------- #


def test_upsert_creates_new_person() -> None:
    result = _call(
        handle_upsert_person,
        {
            "full_name": "Cindy Lee",
            "role": "Head of Marketing",
            "email": "cindy@example.com",
            "department_slugs": ["marketing"],
            "authority_scopes": ["spend_lt_10k"],
        },
    )
    assert result["status"] == "ok"
    assert result["action"] == "created"
    person_id = result["person_id"]

    person = people_store.get_person(person_id)
    assert person is not None
    assert person.full_name == "Cindy Lee"
    assert person.email == "cindy@example.com"
    assert person.department_slugs == ["marketing"]
    assert [s.value for s in person.authority_scope] == ["spend_lt_10k"]


def test_upsert_updates_existing_person() -> None:
    created = _call(handle_upsert_person, {"full_name": "Cindy Lee"})
    pid = created["person_id"]

    updated = _call(
        handle_upsert_person,
        {"person_id": pid, "full_name": "Cindy Lee", "role": "VP Marketing"},
    )
    assert updated["action"] == "updated"
    assert updated["person_id"] == pid

    person = people_store.get_person(pid)
    assert person is not None
    assert person.role == "VP Marketing"


def test_upsert_rejects_invalid_authority_scope() -> None:
    result = _call(
        handle_upsert_person,
        {"full_name": "Cindy Lee", "authority_scopes": ["spend_lt_1m"]},
    )
    assert "error" in result
    assert "invalid authority scope" in result["error"]


def test_upsert_rejects_invalid_on_leave_date() -> None:
    result = _call(
        handle_upsert_person,
        {"full_name": "Cindy Lee", "on_leave_until": "next tuesday"},
    )
    assert "error" in result
    assert "on_leave_until" in result["error"]


def test_upsert_rejects_blank_full_name() -> None:
    result = _call(handle_upsert_person, {"full_name": "   "})
    assert "error" in result


# --------------------------------------------------------------------------- #
# list_people
# --------------------------------------------------------------------------- #


def test_list_people_returns_active_by_default() -> None:
    pid_a = _call(handle_upsert_person, {"full_name": "Alice"})["person_id"]
    pid_b = _call(handle_upsert_person, {"full_name": "Bob"})["person_id"]
    people_store.archive_person(pid_b)

    result = _call(handle_list_people, {})
    ids = {p["person_id"] for p in result["people"]}
    assert pid_a in ids
    assert pid_b not in ids
    assert result["count"] == len(result["people"])


def test_list_people_include_archived() -> None:
    pid = _call(handle_upsert_person, {"full_name": "Alice"})["person_id"]
    people_store.archive_person(pid)

    result = _call(handle_list_people, {"include_archived": True})
    ids = {p["person_id"] for p in result["people"]}
    assert pid in ids


# --------------------------------------------------------------------------- #
# archive_person
# --------------------------------------------------------------------------- #


def test_archive_existing_person() -> None:
    pid = _call(handle_upsert_person, {"full_name": "Alice"})["person_id"]
    result = _call(handle_archive_person, {"person_id": pid})
    assert result["status"] == "archived"
    assert people_store.get_person(pid) is not None  # row still there
    # but it should not appear in active listing
    active_ids = {p.id for p in people_store.list_people()}
    assert pid not in active_ids


def test_archive_missing_person_returns_not_found() -> None:
    result = _call(handle_archive_person, {"person_id": 9999})
    assert result["status"] == "not_found"


# --------------------------------------------------------------------------- #
# set_department_head
# --------------------------------------------------------------------------- #


def test_set_department_head_assigns_existing_person() -> None:
    dept = dept_store.create_department("Marketing")
    pid = _call(handle_upsert_person, {"full_name": "Cindy Lee"})["person_id"]

    result = _call(
        handle_set_department_head,
        {"department_slug": dept.config.slug, "person_id": pid},
    )
    assert result["status"] == "ok"
    assert result["head_person_id"] == pid

    refreshed = dept_store.get_department(dept.config.slug)
    assert refreshed is not None
    assert refreshed.config.head_person_id == pid


def test_set_department_head_clear_with_null_person() -> None:
    dept = dept_store.create_department("Marketing")
    pid = _call(handle_upsert_person, {"full_name": "Cindy Lee"})["person_id"]
    _call(
        handle_set_department_head,
        {"department_slug": dept.config.slug, "person_id": pid},
    )

    result = _call(
        handle_set_department_head,
        {"department_slug": dept.config.slug, "person_id": None},
    )
    assert result["status"] == "ok"
    refreshed = dept_store.get_department(dept.config.slug)
    assert refreshed is not None
    assert refreshed.config.head_person_id is None


def test_set_department_head_unknown_person_rejected() -> None:
    dept = dept_store.create_department("Marketing")
    result = _call(
        handle_set_department_head,
        {"department_slug": dept.config.slug, "person_id": 9999},
    )
    assert "error" in result
    assert "9999" in result["error"]


def test_set_department_head_unknown_department_rejected() -> None:
    from openexecutive.agents.overrides import get_override

    pid = _call(handle_upsert_person, {"full_name": "Cindy Lee"})["person_id"]
    result = _call(
        handle_set_department_head,
        {"department_slug": "nonexistent", "person_id": pid},
    )
    assert "error" in result
    # No orphan override row should be left behind for a non-existent dept.
    assert get_override("department:nonexistent:head") is None


def test_set_department_head_creates_persona_override() -> None:
    from openexecutive.agents.overrides import get_override

    dept = dept_store.create_department("Marketing")
    pid = _call(handle_upsert_person, {"full_name": "Cindy Lee"})["person_id"]
    _call(
        handle_set_department_head,
        {"department_slug": dept.config.slug, "person_id": pid},
    )

    override = get_override(f"department:{dept.config.slug}:head")
    assert override is not None
    assert str(pid) in (override.role or "")


# --------------------------------------------------------------------------- #
# Hardening guarantees
# --------------------------------------------------------------------------- #


def test_upsert_cannot_set_is_principal() -> None:
    result = _call(
        handle_upsert_person,
        {"full_name": "Cindy Lee", "is_principal": True},
    )
    assert "error" in result
    assert "principal" in result["error"].lower()


def test_upsert_with_bogus_person_id_returns_error() -> None:
    result = _call(
        handle_upsert_person,
        {"person_id": 9999, "full_name": "Cindy Lee"},
    )
    assert "error" in result
    assert "9999" in result["error"]


def test_upsert_preserves_is_principal_on_update() -> None:
    # Create a principal directly in the store (bypassing the chat tool gate
    # — this is what the HTTP API does behind BACKEND_SHARED_SECRET).
    pid = people_store.upsert_person(full_name="Principal Pat", is_principal=True)
    # Updating other fields via the chat tool must not flip the flag off.
    result = _call(
        handle_upsert_person,
        {"person_id": pid, "full_name": "Principal Pat", "role": "Founder"},
    )
    assert result.get("action") == "updated"
    refreshed = people_store.get_person(pid)
    assert refreshed is not None
    assert refreshed.is_principal is True
    assert refreshed.role == "Founder"


# --------------------------------------------------------------------------- #
# Only the principal, on a surface that verified it is them, may change the
# roster. A roster row decides web sign-in, who the Executive may email and who
# approves what — and these tools are offered on every turn.
# --------------------------------------------------------------------------- #


def _names() -> set[str]:
    return {p.full_name for p in people_store.list_people()}


def test_owner_on_their_own_slack_can_change_the_roster(owner_id: int) -> None:
    with _turn(Session(origin_channel="slack", caller_person_id=owner_id)):
        result = _call(handle_upsert_person, {"full_name": "Cindy Lee"})
    assert result["status"] == "ok"
    assert "Cindy Lee" in _names()


@pytest.mark.parametrize("surface", [
    {"origin_channel": "slack"},
    {"origin_channel": "discord"},
    {"origin_channel": "telegram"},
    {"from_web_chat": True},
])
def test_teammate_cannot_change_the_roster(surface: dict[str, Any]) -> None:
    teammate = people_store.upsert_person(full_name="Ben Teammate")
    with _turn(Session(caller_person_id=teammate, **surface)):
        added = _call(handle_upsert_person, {
            "full_name": "Mallory", "email": "mallory@evil.example",
            "authority_scopes": ["wildcard"],
        })
        promoted = _call(handle_upsert_person, {
            "person_id": teammate, "full_name": "Ben Teammate",
            "authority_scopes": ["wildcard"],
        })
    assert added["status"] == "refused"
    assert promoted["status"] == "refused"
    assert "owner" in added["detail"]
    assert "Mallory" not in _names()
    ben = people_store.get_person(teammate)
    assert ben is not None and ben.authority_scope == []


@pytest.mark.parametrize("surface", [
    # An inbound email: the poller resolves the From header to a person, but a
    # From header proves nothing — even when it names the principal.
    {"session_id": "email:thread-1"},
    {"origin_channel": "google_chat"},
    # The CLI, the MCP server and unattended runs set no surface either.
    {},
], ids=["email", "google_chat", "no_surface"])
def test_unverified_surfaces_cannot_change_the_roster(
    owner_id: int, surface: dict[str, Any]
) -> None:
    with _turn(Session(caller_person_id=owner_id, **surface)):
        result = _call(handle_upsert_person, {"full_name": "Mallory"})
    assert result["status"] == "refused"
    assert "web app" in result["detail"]
    assert "Mallory" not in _names()


def test_owner_in_a_private_telegram_chat_can_change_the_roster(
    owner_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
    session = Session(origin_channel="telegram", origin_channel_ref="424242",
                      caller_person_id=owner_id)
    with _turn(session):
        result = _call(handle_upsert_person, {"full_name": "Cindy Lee"})
    assert result["status"] == "ok"


@pytest.mark.parametrize(("secret", "chat_ref"), [
    # No webhook secret: /webhook/telegram accepts anyone's POST naming any
    # chat id, so a Telegram "principal" proves nothing.
    (None, "424242"),
    # A group chat (negative id) is every member of the group.
    ("hook-secret", "-100424242"),
], ids=["no_webhook_secret", "group_chat"])
def test_unverifiable_telegram_cannot_change_the_roster(
    owner_id: int, monkeypatch: pytest.MonkeyPatch, secret: str | None, chat_ref: str
) -> None:
    # An empty value, not delenv: Settings also reads the repo .env, which a
    # developer may have filled in; the process env wins over it.
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", secret or "")
    session = Session(origin_channel="telegram", origin_channel_ref=chat_ref,
                      caller_person_id=owner_id)
    with _turn(session):
        result = _call(handle_upsert_person, {"full_name": "Mallory"})
    assert result["status"] == "refused"
    assert "Mallory" not in _names()


def test_background_run_with_no_conversation_cannot_change_the_roster() -> None:
    with _turn(None):
        result = _call(handle_upsert_person, {"full_name": "Mallory"})
    assert result["status"] == "refused"
    assert "Mallory" not in _names()


def test_unlinked_web_user_is_told_how_to_prove_they_are_the_owner() -> None:
    # Signed in, but their email is on no People entry — e.g. a new owner who
    # has not added it to their own row yet.
    with _turn(Session(from_web_chat=True, caller_person_id=None)):
        result = _call(handle_upsert_person, {"full_name": "Cindy Lee"})
    assert result["status"] == "refused"
    assert "People page" in result["detail"]
    assert "Cindy Lee" not in _names()


def test_archived_owner_cannot_change_the_roster(owner_id: int) -> None:
    people_store.archive_person(owner_id)
    result = _call(handle_upsert_person, {"full_name": "Cindy Lee"})
    assert result["status"] == "refused"


def test_archive_and_department_head_are_owner_only_too() -> None:
    dept = dept_store.create_department("Legal")
    teammate = people_store.upsert_person(full_name="Ben Teammate")
    with _turn(Session(origin_channel="slack", caller_person_id=teammate)):
        archived = _call(handle_archive_person, {"person_id": teammate})
        head = _call(handle_set_department_head, {
            "department_slug": dept.config.slug, "person_id": teammate,
        })
    assert archived["status"] == "refused"
    assert head["status"] == "refused"
    assert teammate in {p.id for p in people_store.list_people()}
    refreshed = dept_store.get_department(dept.config.slug)
    assert refreshed is not None and refreshed.config.head_person_id is None


def test_refusal_is_audited(audit_calls: list[dict[str, Any]]) -> None:
    teammate = people_store.upsert_person(full_name="Ben Teammate")
    with _turn(Session(origin_channel="discord", caller_person_id=teammate)):
        _call(handle_upsert_person, {"full_name": "Mallory"})
    refused = [c for c in audit_calls if c["details"].get("refused")]
    assert len(refused) == 1
    assert refused[0]["details"]["tool"] == "upsert_person"
    assert refused[0]["details"]["ok"] is False
    assert refused[0]["details"]["caller_person_id"] == teammate
    assert refused[0]["details"]["origin_channel"] == "discord"


def test_reading_the_roster_stays_open_to_everyone() -> None:
    teammate = people_store.upsert_person(full_name="Ben Teammate")
    with _turn(Session(origin_channel="slack", caller_person_id=teammate)):
        result = _call(handle_list_people, {})
    assert "Ben Teammate" in {p["full_name"] for p in result["people"]}


# --------------------------------------------------------------------------- #
# ask_about_person — Honcho-backed directional peer-memory query
# --------------------------------------------------------------------------- #
from unittest.mock import patch  # noqa: E402  (kept local; not used by CRUD tests)

from openexecutive.orchestrator import people_tools as _people_tools_module  # noqa: E402


def _ask_call(input_dict: dict, answer: str = "synthesized answer") -> dict:
    """Invoke handle_ask_about_person with directional_chat mocked.

    Returns the handler's JSON response merged with `_captured` so tests
    can assert both the wire shape and the values that flowed through
    to honcho_client.directional_chat.
    """
    captured: dict = {}

    async def _fake_chat(
        person_id: int,
        question: str,
        *,
        target_person_id: int | None = None,
        reasoning_level: str = "medium",
    ) -> str:
        captured["person_id"] = person_id
        captured["question"] = question
        captured["target_person_id"] = target_person_id
        captured["reasoning_level"] = reasoning_level
        return answer

    with patch.object(_people_tools_module, "directional_chat", _fake_chat):
        result_str = asyncio.run(
            _people_tools_module.handle_ask_about_person(input_dict)
        )
    parsed = json.loads(result_str)
    parsed["_captured"] = captured
    return parsed


def test_ask_tool_schema_registered() -> None:
    names = {t["name"] for t in _people_tools_module.PEOPLE_TOOLS}
    assert "ask_about_person" in names
    assert "ask_about_person" in _people_tools_module.PEOPLE_TOOL_HANDLERS
    schema = next(
        t["input_schema"] for t in _people_tools_module.PEOPLE_TOOLS
        if t["name"] == "ask_about_person"
    )
    assert set(schema["required"]) == {"person_id", "question"}
    assert set(schema["properties"]["reasoning_level"]["enum"]) == {
        "minimal", "low", "medium", "high", "max"
    }


def test_ask_handler_passes_basic_question_through() -> None:
    result = _ask_call(
        {"person_id": 7, "question": "what does Alice prefer?"},
        answer="Alice prefers terse bullet points.",
    )
    assert result["person_id"] == 7
    assert result["target_person_id"] is None
    assert result["answer"] == "Alice prefers terse bullet points."
    assert result["found"] is True
    assert result["_captured"]["person_id"] == 7
    assert result["_captured"]["question"] == "what does Alice prefer?"
    assert result["_captured"]["target_person_id"] is None
    assert result["_captured"]["reasoning_level"] == "medium"


def test_ask_handler_threads_target_for_directional_query() -> None:
    result = _ask_call(
        {
            "person_id": 7,
            "question": "what has Alice said about Bob?",
            "target_person_id": 9,
            "reasoning_level": "high",
        }
    )
    assert result["target_person_id"] == 9
    assert result["_captured"]["target_person_id"] == 9
    assert result["_captured"]["reasoning_level"] == "high"


def test_ask_handler_clamps_invalid_reasoning_level() -> None:
    """Out-of-band reasoning_level falls back to medium rather than
    propagating a value Honcho would reject at runtime."""
    result = _ask_call(
        {"person_id": 7, "question": "q", "reasoning_level": "ludicrous"}
    )
    assert result["_captured"]["reasoning_level"] == "medium"


def test_ask_handler_reports_empty_answer_as_not_found() -> None:
    """`directional_chat` returning empty (Honcho disabled, no data,
    swallowed error) is a normal outcome — the response should clearly
    signal `found: false` so the model doesn't treat it as a tool failure."""
    result = _ask_call({"person_id": 7, "question": "q"}, answer="")
    assert result["answer"] == ""
    assert result["found"] is False


def test_ask_handler_rejects_missing_person_id() -> None:
    result_str = asyncio.run(
        _people_tools_module.handle_ask_about_person({"question": "q"})
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "person_id" in parsed["error"]


def test_ask_handler_rejects_missing_question() -> None:
    result_str = asyncio.run(
        _people_tools_module.handle_ask_about_person({"person_id": 7})
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "question" in parsed["error"]


def test_ask_handler_rejects_non_integer_target() -> None:
    """A string target_person_id is a model mistake — fail loud so the
    model knows to retry with a proper integer instead of silently
    falling back to a global representation query."""
    result_str = asyncio.run(
        _people_tools_module.handle_ask_about_person(
            {"person_id": 7, "question": "q", "target_person_id": "bob"}
        )
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "target_person_id" in parsed["error"]


def test_ask_handler_accepts_int_strings_for_person_id() -> None:
    """Some MCP / tool-call layers stringify integers — accept them
    rather than 400-ing on a coercion mismatch."""
    result = _ask_call({"person_id": "42", "question": "q"})
    assert result["person_id"] == 42
    assert result["_captured"]["person_id"] == 42
