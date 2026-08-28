"""Unit tests for openexecutive.orchestrator.people_tools.

These tools let the Executive add/update/archive people and assign
department heads from inside a chat turn (no UI round-trip).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from pathlib import Path

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
from openexecutive.orchestrator.schedule_tools import (
    current_caller_person_id,
    current_roster_write_authorized,
)
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def shared_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """All three stores point at the same tmp SQLite file, matching prod."""
    db_path = tmp_path / "episodic.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    monkeypatch.setattr(episodic_module, "DB_PATH", db_path)
    # `agents.overrides` does `from ...episodic import DB_PATH`, so it has its
    # own module-level binding that must also be patched.
    monkeypatch.setattr(agent_overrides, "DB_PATH", db_path)
    people_store.initialize_db()
    principal_id = people_store.upsert_person(
        full_name="Principal", is_principal=True, email="principal@example.com"
    )
    caller_token = current_caller_person_id.set(principal_id)
    roster_token = current_roster_write_authorized.set(True)
    dept_store.initialize_db()
    agent_overrides.initialize_overrides_db()
    people_registry.invalidate()
    dept_registry.invalidate()
    yield db_path
    current_roster_write_authorized.reset(roster_token)
    current_caller_person_id.reset(caller_token)


def _call(coro_fn, payload: dict) -> dict:
    return json.loads(asyncio.run(coro_fn(payload)))


# --------------------------------------------------------------------------- #
# upsert_person
# --------------------------------------------------------------------------- #


def test_untrusted_channel_cannot_change_roster() -> None:
    token = current_roster_write_authorized.set(False)
    try:
        result = _call(handle_upsert_person, {"full_name": "Outsider"})
    finally:
        current_roster_write_authorized.reset(token)
    assert "principal" in result["error"].lower()


def test_non_principal_chat_cannot_change_roster() -> None:
    member_id = people_store.upsert_person(full_name="Member", email="member@example.com")
    token = current_caller_person_id.set(member_id)
    try:
        result = _call(
            handle_upsert_person,
            {"full_name": "Outsider", "email": "outsider@example.com", "authority_scopes": ["wildcard"]},
        )
    finally:
        current_caller_person_id.reset(token)

    assert "principal" in result["error"].lower()
    assert people_store.find_person_by_email("outsider@example.com") is None


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


def test_chat_tools_cannot_mutate_or_archive_principal() -> None:
    # A chat turn may originate from a non-principal. The record's email is
    # part of the HTTP authorization binding, so no principal mutation is safe
    # through the LLM tool path.
    principal = people_store.find_principal_person()
    assert principal is not None and principal.id is not None
    pid = principal.id
    result = _call(
        handle_upsert_person,
        {
            "person_id": pid,
            "full_name": "Principal Pat",
            "email": "attacker@example.com",
        },
    )
    assert "error" in result
    assert "principal" in result["error"].lower()
    assert _call(handle_archive_person, {"person_id": pid})["error"]

    refreshed = people_store.get_person(pid)
    assert refreshed is not None
    assert refreshed.email == "principal@example.com"
    assert refreshed.archived is False


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
