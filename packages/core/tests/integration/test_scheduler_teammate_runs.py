"""A scheduled row a teammate queued runs as that teammate.

`schedule_followup` and `suggest_workflow` keep the speaker on the row
(`created_by_person_id`). When such a row comes due, the runner must not hand
the teammate's stored text to a turn with more than their own turns get: the
Executive only writes the message (with `Session.run_for_person_id` naming
them and nothing it may send or schedule to), and the runner sends it to the
teammate's own address. A row whose creator left the team, or whose address
is not theirs, is cancelled.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.memory.episodic import (
    claim_due_actions,
    get_scheduled_action,
    initialize_db,
    insert_scheduled_action,
)
from openexecutive.people import store as people_store
from openexecutive.scheduler import runner
from openexecutive.scheduler.runner import PrincipalDelivery, _execute_action


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    monkeypatch.setattr(audit_logger, "_default_logger", audit_logger.AuditLogger(db_path=db_path))
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    return db_path


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub the Executive, profile, retriever and delivery; record calls."""
    seen: dict[str, list[Any]] = {"chat": [], "sent": [], "episodic": []}

    class _StubExec:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def chat(self, **kwargs: Any) -> str:
            seen["chat"].append(kwargs)
            return "Board prep is tomorrow at 10."

    class _EmptyProfile:
        def is_empty(self) -> bool:
            return True

    async def _send(person: Any, plan: list[str], text: str, *, label: str) -> PrincipalDelivery:
        seen["sent"].append((person.id, plan, text))
        return PrincipalDelivery(True, "ok", "delivered", plan[0])

    def _episodic(**kwargs: Any) -> str:
        seen["episodic"].append(kwargs)
        return "decisions"

    monkeypatch.setattr("openexecutive.orchestrator.executive.Executive", _StubExec)
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: _EmptyProfile(),
    )
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda query: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", _episodic)
    monkeypatch.setattr(runner, "_send_on_plan", _send)
    return seen


def _due(channel_ref: str, creator: int | None, *, session_id: str | None = "s-1") -> int:
    return insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        channel="telegram",
        channel_ref=channel_ref,
        intent_text="remind me about board prep and include the CEO's decisions",
        originating_session_id=session_id,
        created_by_person_id=creator,
    )


def _run() -> None:
    claimed = claim_due_actions(datetime.now(UTC))
    assert len(claimed) == 1
    asyncio.run(_execute_action(claimed[0], gateway=None))


def test_a_teammates_row_runs_as_them_and_reaches_only_them(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    people_store.upsert_person(full_name="Owner", is_principal=True, telegram_chat_id="1")
    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")
    action_id = _due("77", sam)

    _run()

    assert get_scheduled_action(action_id).status == "done"  # type: ignore[union-attr]
    (chat,) = calls["chat"]
    session = chat["session"]
    assert session.unattended is True
    assert session.run_for_person_id == sam
    # Nothing it may send or schedule to: the runner delivers.
    assert session.seen_channel_refs == set()
    assert "Reply with the message text alone" in chat["user_message"]
    # Only the conversation that queued it, never the company-wide list.
    assert calls["episodic"] == [{"session_id": "s-1"}]
    assert calls["sent"] == [(sam, ["telegram"], "Board prep is tomorrow at 10.")]


def test_without_its_conversation_the_run_gets_no_decisions(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")
    _due("77", sam, session_id=None)

    _run()

    assert calls["episodic"] == []
    assert calls["chat"][0]["episodic_context"] == ""


def test_a_teammates_row_aimed_at_someone_else_is_cancelled(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    people_store.upsert_person(full_name="Owner", is_principal=True, telegram_chat_id="1")
    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")
    action_id = _due("1", sam)

    _run()

    stored = get_scheduled_action(action_id)
    assert stored is not None and stored.status == "cancelled"
    assert "only reach that teammate" in stored.last_error
    assert calls["chat"] == [] and calls["sent"] == []


def test_a_row_whose_creator_left_is_cancelled(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")
    people_store.archive_person(sam)
    action_id = _due("77", sam)

    _run()

    stored = get_scheduled_action(action_id)
    assert stored is not None and stored.status == "cancelled"
    assert calls["chat"] == []


def test_a_failed_delivery_retries(
    db: Path, calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")

    async def _fail(*_a: Any, **_k: Any) -> PrincipalDelivery:
        return PrincipalDelivery(False, "telegram: down", "send_failed")

    monkeypatch.setattr(runner, "_send_on_plan", _fail)
    action_id = _due("77", sam)

    _run()

    stored = get_scheduled_action(action_id)
    assert stored is not None and stored.status == "pending"
    assert "telegram: down" in stored.last_error


def test_the_principals_row_keeps_the_ordinary_dispatch(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    owner = people_store.upsert_person(full_name="Owner", is_principal=True, telegram_chat_id="1")
    action_id = _due("1", owner)

    _run()

    assert get_scheduled_action(action_id).status == "done"  # type: ignore[union-attr]
    (chat,) = calls["chat"]
    assert chat["session"].run_for_person_id is None
    assert chat["session"].seen_channel_refs == {("telegram", "1")}
    assert calls["sent"] == []


def test_an_alias_of_the_teammate_counts_as_their_own(
    db: Path, calls: dict[str, list[Any]]
) -> None:
    from openexecutive.people.models import Person
    from openexecutive.scheduler.runner import _own_ref

    sam = Person(full_name="Sam", email="sam@co.example", email_aliases=["S.Lee@co.example"])
    assert _own_ref(sam, "email", "s.lee@co.example|thread-9")
    assert _own_ref(sam, "email", "SAM@co.example")
    assert not _own_ref(sam, "email", "owner@co.example")
    assert not _own_ref(sam, "telegram", "77")


def test_an_approved_copy_of_a_teammates_row_stays_theirs(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department-gated row is re-queued on approval; the copy must still
    run as the teammate, not take the ordinary dispatch."""
    from types import SimpleNamespace

    from openexecutive.api.routes import alerts as alerts_route
    from openexecutive.departments.authority import PROPOSAL_ALERT_SOURCE
    from openexecutive.memory.episodic import list_scheduled_actions, mark_action_done

    sam = people_store.upsert_person(full_name="Sam", telegram_chat_id="77")
    held = _due("77", sam)
    mark_action_done(held)
    monkeypatch.setattr(alerts_route, "_finished", lambda *a, **k: "finished")

    out = alerts_route._requeue_scheduled_action(
        SimpleNamespace(source=PROPOSAL_ALERT_SOURCE), SimpleNamespace(), held  # type: ignore[arg-type]
    )

    assert out == "finished"
    copy = [a for a in list_scheduled_actions() if a.id != held]
    assert [a.created_by_person_id for a in copy] == [sam]
