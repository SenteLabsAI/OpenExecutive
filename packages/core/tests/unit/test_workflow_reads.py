"""Workflows that read their creator's own mailbox and calendar
(delegation/workflow_reads.py): only on the creator's own run, with the run
theirs alone and locked down the way a chat turn that read mail is."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation import workflow_email, workflow_reads
from openexecutive.delegation.gmail import ThreadSummary
from openexecutive.memory import episodic
from openexecutive.orchestrator.artifact_records import pinned_viewer, turn_owner, viewer_for_person
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import chat_tools, dynamic_store, tool_catalog

OWNER = "olivia@co.example"
TEAM = "ben@co.example"
SEARCH = "oe__search_my_email"


def _step(tools: list[str], **extra: Any) -> Any:
    return SimpleNamespace(kind="action", tools=tools, **extra)


def _defn(*steps: Any, cadence_person_id: int | None = None) -> Any:
    return SimpleNamespace(name="inbox_digest", steps=list(steps), cadence_person_id=cadence_person_id)


READING = _defn(_step([SEARCH, "oe__message_person"]))


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Import what the tools reach before DB_PATH is patched: modules that bind
    # it at import (approved_targets, ...) would keep this test's path.
    tool_catalog.builtin_tools()
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    people_registry.invalidate()


@pytest.fixture
def people(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate_id = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    monkeypatch.setattr(dynamic_store, "get_owner", lambda name: owner_id)
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: READING)
    return owner_id, teammate_id


@pytest.fixture
def on(people: tuple[int, int]) -> int:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    return owner_id


class FakeMailbox:
    def __init__(self) -> None:
        self.searched: list[str] = []

    async def profile_email(self) -> str:
        return OWNER

    async def search_threads(self, query: str, *, max_results: int = 5) -> list[ThreadSummary]:
        self.searched.append(query)
        return [ThreadSummary(id="t1", subject="Pilot", sender="Dana <dana@x.example>", date="today")]


def _search(
    mailbox: FakeMailbox, *, creator: int | None, run_owner: int | None,
    chain: tuple[str, ...] = ("inbox_digest",),
) -> dict[str, Any]:
    handler = tool_catalog.builtin_handler(SEARCH)

    async def go() -> str:
        blocks = [chat_tools.in_workflow(name, creator) for name in chain]
        for block in blocks:
            block.__enter__()
        try:
            with pinned_viewer(viewer_for_person(run_owner) if run_owner is not None else viewer_for_person(None)):
                return await handler({"query": "pilot"}, mailbox=mailbox)
        finally:
            for block in reversed(blocks):
                block.__exit__(None, None, None)

    return json.loads(asyncio.run(go()))


# ── Reading ──────────────────────────────────────────────────────────────────


def test_reads_the_creators_mailbox_on_their_own_run(on) -> None:
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=on, run_owner=on)

    assert out["status"] == "ok" and out["threads"][0]["thread_id"] == "t1"
    assert mailbox.searched == ["pilot"]


def test_registered_as_reads() -> None:
    tools = {t.name: t for t in tool_catalog.builtin_tools()}
    for name in workflow_reads.TOOLS:
        assert tools[name].read_only is True and tools[name].source == "builtin"


def test_not_on_a_team_run(on) -> None:
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=on, run_owner=None)

    assert "run that is theirs" in out["error"] and mailbox.searched == []


def test_not_on_a_run_someone_else_started(people, on) -> None:
    _, teammate = people
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=on, run_owner=teammate)

    assert "error" in out and mailbox.searched == []


def test_not_when_another_workflow_started_it(on) -> None:
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=on, run_owner=on, chain=("weekly", "inbox_digest"))

    assert "another workflow started" in out["error"] and mailbox.searched == []


def test_not_without_act_as_me(people) -> None:
    owner_id, _ = people
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=owner_id, run_owner=owner_id)

    assert "Act as me" in out["error"] and mailbox.searched == []


def test_not_when_the_workflow_reaches_outside(on, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: _defn(_step([SEARCH, "oe__web_search"])))
    mailbox = FakeMailbox()

    out = _search(mailbox, creator=on, run_owner=on)

    assert "oe__web_search" in out["error"] and mailbox.searched == []


# ── What a workflow that reads may be ────────────────────────────────────────


@pytest.mark.parametrize("tool", [
    "oe__web_search", "oe__run_workflow", "oe__read_document", "oe__send_company_broadcast",
    "oe__create_alert", "google_workspace__send_gmail_message", "notion__create_page",
    "oe__send_email_as_me",
])
def test_nothing_that_reaches_outside_alongside_a_read(tool: str) -> None:
    refused = workflow_reads.definition_refusal(_defn(_step([SEARCH]), _step([tool])), 1)
    assert refused is not None and tool in refused


def test_what_may_run_alongside_a_read() -> None:
    defn = _defn(_step([
        *sorted(workflow_reads.TOOLS), workflow_email.DRAFT_TOOL,
        "oe__message_person", "oe__draft_artifact", "oe__list_people", "oe__create_calendar_event",
    ]))
    assert workflow_reads.definition_refusal(defn, 1) is None


def test_approvals_and_reports_go_to_the_creator_only() -> None:
    gate = SimpleNamespace(kind="approval_gate", person_id=2, tools=None)
    assert "only ask you" in str(workflow_reads.definition_refusal(_defn(_step([SEARCH]), gate), 1))
    mine = SimpleNamespace(kind="approval_gate", person_id=1, tools=None)
    assert workflow_reads.definition_refusal(_defn(_step([SEARCH]), mine), 1) is None
    to_ben = _defn(_step([SEARCH]), cadence_person_id=2)
    assert "report to you" in str(workflow_reads.definition_refusal(to_ben, 1))
    assert workflow_reads.definition_refusal(_defn(_step([SEARCH]), cadence_person_id=1), 1) is None


def test_a_workflow_that_doesnt_read_is_not_checked() -> None:
    assert workflow_reads.definition_refusal(_defn(_step(["oe__web_search"])), None) is None


def test_steps_of_a_reading_workflow_get_no_scripts(people) -> None:
    assert workflow_reads.run_rules("inbox_digest") == (True, None)


def test_only_the_verified_creator_saves_one(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.delegation import verified

    owner_id, _ = people
    monkeypatch.setattr(verified, "caller_refusal", lambda caller, email: verified.NOT_YOURS)
    assert "reads their email" in str(workflow_email.save_refusal(READING, object(), owner_id))
    monkeypatch.setattr(verified, "caller_refusal", lambda caller, email: None)
    assert workflow_email.save_refusal(READING, object(), owner_id) is None
    outside = _defn(_step([SEARCH, "oe__web_search"]))
    assert "oe__web_search" in str(workflow_email.save_refusal(outside, object(), owner_id))
    # The stored copy an edit replaces is checked for who, not what it lists,
    # so an edit can take out a tool it may no longer use.
    assert workflow_email.save_refusal(outside, object(), owner_id, check_tools=False) is None


def test_chat_cannot_draft_one_that_reaches_outside(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import workflow_authoring_tools as wat

    async def build(definition: Any) -> tuple[Any, None]:
        return _defn(_step([SEARCH, "oe__web_search"])), None

    monkeypatch.setattr(wat, "_build_def", build)
    monkeypatch.setattr(wat, "_session_caller_id", lambda: people[0])
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: None)

    out = json.loads(asyncio.run(wat.handle_draft_workflow({"definition": {"name": "inbox_digest"}})))

    assert "oe__web_search" in out["error"]


# ── Whose run it is ──────────────────────────────────────────────────────────


def test_a_chat_run_needs_the_creator_and_marks_the_turn(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import delegation_tools

    owner_id, teammate = people
    monkeypatch.setattr(delegation_tools, "_keep_conversation_private", lambda writer: True)
    theirs = dsettings.TurnDelegation(enabled=True, offered=True, person_id=owner_id)
    monkeypatch.setattr(dsettings, "turn_delegation", lambda session: theirs)

    assert workflow_reads.chat_run_refusal("inbox_digest") is None
    assert theirs.read_mail and theirs.touched_mail

    someone = dsettings.TurnDelegation(enabled=True, offered=True, person_id=teammate)
    monkeypatch.setattr(dsettings, "turn_delegation", lambda session: someone)
    assert "only they can run it" in str(workflow_reads.chat_run_refusal("inbox_digest"))
    assert not someone.read_mail

    monkeypatch.setattr(dsettings, "turn_delegation", lambda session: None)
    assert workflow_reads.chat_run_refusal("inbox_digest") is not None


def test_a_scheduled_run_is_the_creators_and_reports_to_them(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.scheduler import runner
    from openexecutive.workflows import persistence
    from openexecutive.workflows.base import WorkflowEvent

    owner_id, teammate = people
    seen: dict[str, Any] = {}

    class Reader:
        title = "Inbox digest"

        def input_model(self) -> Any:
            return lambda: SimpleNamespace(model_dump=lambda: {})

        async def run(self, inputs: Any, store: Any) -> Any:
            seen["run_owner"] = turn_owner()
            yield WorkflowEvent(type="artifact", content="Three threads need you.")

    defn = SimpleNamespace(**vars(READING), is_active=True)
    monkeypatch.setattr(dynamic_store, "get_definition", lambda name: defn)
    monkeypatch.setattr("openexecutive.workflows.get_workflow", lambda name: Reader())
    monkeypatch.setattr(
        persistence, "create_run",
        lambda *a, owner_person_id=None, **k: seen.__setitem__("owner_person_id", owner_person_id),
    )
    monkeypatch.setattr(persistence, "complete_run", lambda rid, art: None)
    monkeypatch.setattr("openexecutive.knowledge.store.ChromaDBStore", lambda **k: None)
    monkeypatch.setattr(runner, "mark_action_done", lambda aid: None)
    monkeypatch.setattr(
        "openexecutive.workflows.dynamic_cadence.schedule_dynamic_workflow_cadence", lambda d, after=None: None
    )
    dms: list[dict[str, Any]] = []

    async def dm(payload: dict[str, Any]) -> str:
        from openexecutive.audit.context import rows_owner, rows_private

        dms.append({**payload, "private": rows_private(), "rows_owner": rows_owner()})
        return "{}"

    monkeypatch.setattr("openexecutive.orchestrator.schedule_tools.handle_message_person", dm)
    action = SimpleNamespace(id=3, channel_ref="inbox_digest", assigned_to_person_id=teammate, scope_key=None)

    asyncio.run(runner._run_dynamic_workflow(action, now=__import__("datetime").datetime.now()))  # type: ignore[arg-type]

    assert seen == {"owner_person_id": owner_id, "run_owner": owner_id}
    # Delivered to them alone, privately: no row on the shared activity rail.
    assert [(d["person_id"], d["private"], d["rows_owner"]) for d in dms] == [(owner_id, True, owner_id)]


def test_a_resumed_run_stays_private(people, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.audit.context import rows_owner, rows_private
    from openexecutive.workflows import resumer

    owner_id, _ = people
    seen: list[tuple[bool, int | None, int | None]] = []

    async def drive(row: Any, claim: str, db_path: Any = None) -> bool:
        seen.append((rows_private(), rows_owner(), turn_owner()))
        return True

    monkeypatch.setattr(resumer, "_drive_resume", drive)
    monkeypatch.setattr(
        resumer._wf_persistence, "get_run",
        lambda run_id, db_path=None: {"owner_person_id": owner_id, "workflow_name": "inbox_digest"},
    )

    assert asyncio.run(resumer._execute_resume({"run_id": "r1"}, "c1")) is True
    assert seen == [(True, owner_id, owner_id)]
