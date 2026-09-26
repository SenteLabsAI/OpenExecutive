"""A whole chat turn that drafts as someone (Executive.stream_chat): it stays
private to them and teaches no shared memory; a turn that doesn't is as
before."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.context import rows_private
from openexecutive.audit.logger import AuditLogger, log_event
from openexecutive.delegation import ghostwriter as gw
from openexecutive.delegation.settings import DelegationOverride
from openexecutive.memory import episodic
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

from ._agent_loop_fakes import FakeStream, FinalMsg, ScriptedProvider, TextBlock, ToolUseBlock
from .test_delegation_tools import FakeMailbox


class _TextStream(FakeStream):
    """Streams the final message's text as deltas, as the real provider does,
    so the turn has a reply to audit."""

    def __init__(self, final_msg: FinalMsg) -> None:
        super().__init__(final_msg)
        self._deltas = [
            SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            for b in final_msg.content if getattr(b, "type", "") == "text"
        ]

    async def __anext__(self) -> Any:
        if not self._deltas:
            raise StopAsyncIteration
        return self._deltas.pop(0)


class _TextProvider(ScriptedProvider):
    def messages_stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        return _TextStream(self._final_msgs.pop(0))


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    prior = current_session.get()
    current_session.set(None)
    yield path
    current_session.set(prior)
    people_registry.invalidate()


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"sync": [], "extraction_private": []}

    async def no_prefetch(*_a: Any, **_kw: Any) -> str:
        return ""

    monkeypatch.setattr("openexecutive.memory.honcho_client.prefetch", no_prefetch)
    monkeypatch.setattr(
        "openexecutive.memory.honcho_client.sync_turn",
        lambda *a, **kw: seen["sync"].append(a),
    )
    monkeypatch.setattr("openexecutive.memory.episodic.should_extract", lambda *a, **kw: True)
    monkeypatch.setattr(
        "openexecutive.memory.episodic.schedule_extraction",
        lambda *a, **kw: seen["extraction_private"].append(rows_private()),
    )

    async def composer(model: str, system: str, turn: str) -> dict[str, Any]:
        return {"subject": "x", "body": "Hi Dana,\n\nYes.\n\nOlivia"}

    monkeypatch.setattr(gw, "_call_model", composer)
    return seen


def _turn(tool_uses: list[Any]) -> Session:
    from openexecutive.orchestrator.executive import Executive

    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    people_registry.invalidate()
    person = people_store.get_person(principal)
    session = Session(
        delegation_override=DelegationOverride(enabled=True, gmail=FakeMailbox(), person=person),
    )
    finals = [FinalMsg(tool_uses, "tool_use")] if tool_uses else []
    finals.append(FinalMsg([TextBlock("Your draft is waiting in your Gmail Drafts.")], "end_turn"))
    provider = _TextProvider(finals)

    async def go() -> None:
        with (
            patch("openexecutive.orchestrator.executive.get_provider", return_value=provider),
            patch("openexecutive.orchestrator.executive.audit_log", log_event),
        ):
            async for _ in Executive().stream_chat("reply to Dana as me: yes", session, person_id=principal):
                pass

    asyncio.run(go())
    return session


def _chat_turn_rows() -> list[Any]:
    return [
        r for r in audit_logger.get_audit_logger().query(event_type="chat_turn")
        if (r.details or {}).get("direction") == "out"
    ]


def test_a_turn_that_drafted_stays_private_and_teaches_no_shared_memory(
    hooks: dict[str, list[Any]],
) -> None:
    session = _turn([ToolUseBlock("tu1", "ghostwrite_email", {"intent": "Yes.", "thread_id": "t1"})])
    assert session.turn_delegation.touched_mail is True
    assert [r.private for r in _chat_turn_rows()] == [True]
    assert hooks["sync"] == []
    assert hooks["extraction_private"] == [True]


def test_a_turn_that_did_not_is_as_before(hooks: dict[str, list[Any]]) -> None:
    session = _turn([])
    assert session.turn_delegation.touched_mail is False
    assert [r.private for r in _chat_turn_rows()] == [False]
    assert len(hooks["sync"]) == 1
    assert hooks["extraction_private"] == [False]


def test_the_system_prompt_carries_the_addendum_only_when_on(hooks: dict[str, list[Any]]) -> None:
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.prompts.executive_persona import DELEGATION_ADDENDUM

    def system_text(override: DelegationOverride | None) -> str:
        provider = ScriptedProvider([FinalMsg([TextBlock("ok")], "end_turn")])
        session = Session(delegation_override=override)

        async def go() -> None:
            with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
                async for _ in Executive().stream_chat("hi", session):
                    pass

        asyncio.run(go())
        return str(provider.calls[0]["system"][0]["text"])

    assert DELEGATION_ADDENDUM in system_text(DelegationOverride(enabled=True, gmail=FakeMailbox()))
    assert DELEGATION_ADDENDUM not in system_text(DelegationOverride(enabled=False))
