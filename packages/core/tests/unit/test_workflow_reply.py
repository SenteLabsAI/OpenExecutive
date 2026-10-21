"""A workflow step answers an email in its creator's inbox the way Handle it
for me does (delegation/workflow_reply.py, inbox.answer_thread): the same
writing, rules, last look and ledger, with only a thread to name."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from openexecutive.delegation import handle_it, inbox, workflow_email, workflow_reply
from openexecutive.memory import decision_ledger as ledger
from openexecutive.orchestrator.artifact_records import pinned_viewer, viewer_for_person
from openexecutive.workflows import chat_tools, dynamic_store, tool_catalog

from . import test_delegation_inbox as _inbox_tests
from .test_delegation_inbox import NOW, OWNER, FakeInbox, _ledger, _msg

db = _inbox_tests.db
owner = _inbox_tests.owner
models = _inbox_tests.models


@pytest.fixture(autouse=True)
def local_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_LOCAL_LOGIN", "1")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)


def _answer(owner: Any, mailbox: FakeInbox, thread_id: str = "t1") -> str:
    return asyncio.run(inbox.answer_thread(owner, mailbox, thread_id, now=NOW))


def _mailbox(*messages: Any) -> FakeInbox:
    mailbox = FakeInbox()
    mailbox.add(*(messages or (_msg("m1", "t1"),)))
    return mailbox


def test_it_sends_on_its_own_when_handle_it_allows(db, owner: Any, models: dict[str, Any]) -> None:
    handle_it.set_(owner.id, enabled=True, mode=None, updated_by="test")
    mailbox = _mailbox()

    assert _answer(owner, mailbox) == "sent"
    assert mailbox.sent == ["d1"]
    assert _ledger(db)["m1"] == ("sent", "handled")
    assert _answer(owner, mailbox) == "theirs_last" and mailbox.sent == ["d1"]


def test_otherwise_it_waits_on_a_card(owner: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox()

    assert _answer(owner, mailbox) == "card"
    assert mailbox.sent == []
    assert [c.gate_mode for c in ledger.list_instances("delegation_reply", limit=5)] == ["propose"]
    # Looked at once, by the step or the watcher: never a second card.
    assert _answer(owner, mailbox) == "not_now"
    assert len(ledger.list_instances("delegation_reply", limit=5)) == 1


def test_an_email_that_steers_an_assistant_waits(owner: Any, models: dict[str, Any]) -> None:
    handle_it.set_(owner.id, enabled=True, mode=None, updated_by="test")
    models["steers"] = True
    mailbox = _mailbox()

    assert _answer(owner, mailbox) == "card" and mailbox.sent == []


def test_nothing_to_answer_when_they_wrote_last(owner: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox(_msg("m1", "t1"), _msg("m2", "t1", sender=OWNER, name="Olivia", minutes_ago=5))

    assert _answer(owner, mailbox) == "theirs_last"
    assert models["classified"] == []


def test_mail_that_gets_no_reply_is_skipped(owner: Any, models: dict[str, Any]) -> None:
    mailbox = _mailbox(_msg("m1", "t1", bulk=True))

    assert _answer(owner, mailbox) == "skipped" and models["classified"] == []


# ── the step's tool ──────────────────────────────────────────────────────────


def _run(owner_id: int, tool_input: dict[str, Any], mailbox: FakeInbox, *, runner: int | None) -> dict[str, Any]:
    async def go() -> str:
        with chat_tools.in_workflow("inbox_answers", owner_id), pinned_viewer(viewer_for_person(runner)):
            return await workflow_reply.handle_answer_email_as_me(tool_input, mailbox=mailbox)

    return json.loads(asyncio.run(go()))


@pytest.fixture
def at_now(monkeypatch: pytest.MonkeyPatch, owner: Any) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return NOW

    monkeypatch.setattr(workflow_reply, "datetime", _Clock)
    monkeypatch.setattr(dynamic_store, "get_owner", lambda name: owner.id)


def test_the_step_answers_as_its_creator(owner: Any, models: dict[str, Any], at_now: None) -> None:
    handle_it.set_(owner.id, enabled=True, mode=None, updated_by="test")
    mailbox = _mailbox()

    out = _run(owner.id, {"thread_id": "t1"}, mailbox, runner=owner.id)

    assert out["status"] == "sent" and mailbox.sent == ["d1"]
    assert "body" not in out


def test_the_step_never_answers_on_someone_elses_run(owner: Any, models: dict[str, Any], at_now: None) -> None:
    mailbox = _mailbox()

    out = _run(owner.id, {"thread_id": "t1"}, mailbox, runner=owner.id + 1)

    assert "someone else" in out["error"] and mailbox.specs == [] and models["classified"] == []


def test_the_step_needs_a_thread(owner: Any, models: dict[str, Any], at_now: None) -> None:
    assert "thread_id" in _run(owner.id, {}, _mailbox(), runner=owner.id)["error"]


def test_it_is_a_send_and_offered_to_steps() -> None:
    step = type("S", (), {"tools": [workflow_email.REPLY_TOOL]})()
    defn = type("D", (), {"steps": [step]})()
    assert workflow_email.sends(defn)
    info = {t.name: t for t in tool_catalog.builtin_tools()}[workflow_email.REPLY_TOOL]
    assert info.read_only is not True and (info.timeout_s or 0) >= 300


def test_naming_a_thread_is_no_write_target() -> None:
    from openexecutive.workflows import action_step

    # The recipients come from the thread itself, so nothing is held to ask.
    assert action_step.resource_targets({"thread_id": "18c2f0a9b7"}) == ([], True)


def test_one_answer_at_a_time_per_person(owner: Any, models: dict[str, Any]) -> None:
    handle_it.set_(owner.id, enabled=True, mode=None, updated_by="test")
    mailbox = _mailbox(_msg("m1", "t1"), _msg("m2", "t2", minutes_ago=40))

    async def both() -> list[str]:
        return list(await asyncio.gather(
            inbox.answer_thread(owner, mailbox, "t1", now=NOW),
            inbox.answer_thread(owner, mailbox, "t2", now=NOW),
        ))

    assert sorted(asyncio.run(both())) == ["busy", "sent"]
    assert owner.id not in inbox._SCANNING
