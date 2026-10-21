"""``oe__draft_email_as_me``: a workflow step drafts an email as the
workflow's creator, under the same Act as me rules as chat
(delegation/workflow_drafts.py, workflows/action_step.py)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from openexecutive.audit import logger as audit_logger
from openexecutive.audit.logger import AuditLogger
from openexecutive.delegation import caps, workflow_drafts
from openexecutive.delegation import drafts as ddrafts
from openexecutive.delegation import ghostwriter as gw
from openexecutive.delegation import settings as dsettings
from openexecutive.delegation.gmail import CreatedDraft, DraftSpec
from openexecutive.memory import episodic
from openexecutive.orchestrator.artifact_records import pinned_viewer, viewer_for_person
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.workflows import action_step, tool_catalog
from openexecutive.workflows.designer import build_context_block

OWNER = "olivia@co.example"
TEAM = "ben@co.example"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    token = current_session.set(None)
    yield path
    current_session.reset(token)
    caps._SAVED_TODAY.clear()
    caps._IN_FLIGHT.clear()
    people_registry.invalidate()


@pytest.fixture
def people() -> tuple[int, int]:
    owner_id = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate_id = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return owner_id, teammate_id


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_compose(**kwargs: Any) -> gw.ComposedDraft:
        calls.append(kwargs)
        return gw.ComposedDraft(subject="Q3 numbers", body="Hi Ben, revenue is up 12%.")

    monkeypatch.setattr(gw, "compose", fake_compose)
    return calls


class FakeMailbox:
    def __init__(self, opened: str = OWNER) -> None:
        self.opened = opened
        self.drafts: list[DraftSpec] = []

    async def profile_email(self) -> str:
        return self.opened

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        self.drafts.append(spec)
        return CreatedDraft(draft_id="d1", message_id="m99", thread_id="tnew")


def _run(person_id: int | None, tool_input: dict[str, Any], mailbox: FakeMailbox) -> dict[str, Any]:
    async def go() -> str:
        with workflow_drafts.writing_for(person_id):
            return await workflow_drafts.handle_draft_email_as_me(tool_input, mailbox=mailbox)

    return json.loads(asyncio.run(go()))


INPUT = {"to": [TEAM], "intent": "Send Ben the Q3 revenue: up 12%."}


def test_drafts_in_the_creators_mailbox_and_returns_no_body(people, composed) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    mailbox = FakeMailbox()

    result = _run(owner_id, INPUT, mailbox)

    assert result["status"] == "drafted"
    assert result["to"] == [TEAM] and result["subject"] == "Q3 numbers"
    assert "body" not in result and "preview" not in result
    assert [d.to for d in mailbox.drafts] == [[TEAM]]
    assert mailbox.drafts[0].thread_id is None
    assert composed[0]["intent"] == INPUT["intent"]
    assert ddrafts.count_since(owner_id, ddrafts.datetime(2000, 1, 1, tzinfo=ddrafts.UTC)) == 1


def test_refused_with_the_fallback_when_act_as_me_is_off(people, composed) -> None:
    owner_id, _ = people
    mailbox = FakeMailbox()

    result = _run(owner_id, INPUT, mailbox)

    assert "Act as me" in result["error"] and "Executive's own email" in result["error"]
    assert mailbox.drafts == [] and composed == []


def test_refused_when_the_mailbox_is_not_theirs(people, composed) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)

    result = _run(owner_id, INPUT, FakeMailbox(opened="someone@else.example"))

    assert result["status"] == "mismatch" and composed == []


def test_refused_without_a_creator(people, composed) -> None:
    result = _run(None, INPUT, FakeMailbox())

    assert "no creator" in result["error"] and composed == []


def test_refused_when_someone_else_started_the_run(people, composed) -> None:
    owner_id, teammate_id = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    mailbox = FakeMailbox()

    with pinned_viewer(viewer_for_person(teammate_id)):
        result = _run(owner_id, INPUT, mailbox)

    assert "someone else started this run" in result["error"]
    assert mailbox.drafts == []


def test_the_creator_starting_it_by_hand_may(people, composed) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)

    with pinned_viewer(viewer_for_person(owner_id)):
        result = _run(owner_id, INPUT, FakeMailbox())

    assert result["status"] == "drafted"


def test_daily_limit_is_shared(people, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    monkeypatch.setenv("DELEGATION_MAX_DRAFTS_PER_DAY", "1")
    mailbox = FakeMailbox()

    assert _run(owner_id, INPUT, mailbox)["status"] == "drafted"
    assert "limit" in _run(owner_id, INPUT, mailbox)["error"]
    assert len(mailbox.drafts) == 1


@pytest.mark.parametrize("tool_input", [
    {"to": [TEAM]},
    {"to": [], "intent": "hi"},
    {"to": ["not-an-address"], "intent": "hi"},
    {"to": [f"p{i}@x.example" for i in range(11)], "intent": "hi"},
])
def test_bad_input_writes_nothing(people, composed, tool_input) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    mailbox = FakeMailbox()

    assert "error" in _run(owner_id, tool_input, mailbox)
    assert mailbox.drafts == []


def test_a_composer_failure_is_an_error_not_a_raise(people, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)

    async def boom(**kwargs: Any) -> gw.ComposedDraft:
        raise TimeoutError

    monkeypatch.setattr(gw, "compose", boom)
    mailbox = FakeMailbox()

    result = _run(owner_id, INPUT, mailbox)

    assert "Executive's own email" in result["error"]
    assert mailbox.drafts == [] and caps._IN_FLIGHT.get(owner_id, 0) == 0


def test_a_failed_audit_still_reports_the_saved_draft(people, composed, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(workflow_drafts, "_audit", boom)

    assert _run(owner_id, INPUT, FakeMailbox())["status"] == "drafted"


def test_unrostered_address_is_named(people, composed) -> None:
    owner_id, _ = people
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)

    result = _run(owner_id, {**INPUT, "cc": ["new@outside.example"]}, FakeMailbox())

    assert result["not_in_people"] == ["new@outside.example"]


def test_dispatch_writes_as_the_workflows_creator(people, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id, _ = people
    seen: list[int | None] = []

    async def handler(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
        seen.append(workflow_drafts._creator.get())
        return "{}"

    monkeypatch.setattr(workflow_drafts, "handle_draft_email_as_me", handler)
    monkeypatch.setattr("openexecutive.workflows.dynamic_store.get_owner", lambda name: owner_id)
    info = {t.name: t for t in tool_catalog.builtin_tools()}[workflow_drafts.TOOL_NAME]

    asyncio.run(action_step._dispatch(workflow_drafts.TOOL_NAME, {}, info, "wf"))

    assert seen == [owner_id] and workflow_drafts._creator.get() is None


def test_builtin_is_a_write_and_recipients_are_targets() -> None:
    info = {t.name: t for t in tool_catalog.builtin_tools()}[workflow_drafts.TOOL_NAME]
    assert info.read_only is None
    targets, complete = action_step.resource_targets({"to": [TEAM], "cc": ["c@x.example"], "intent": "x"})
    assert complete and {v for _, v in targets} == {TEAM, "c@x.example"}


def test_designer_is_told_whether_it_can_write_as_them(people) -> None:
    owner_id, _ = people
    assert workflow_drafts.available_for(owner_id) is False
    dsettings.set_enabled(owner_id, True, updated_by=OWNER)
    assert workflow_drafts.available_for(owner_id) is True

    assert "their own mailbox is connected" in build_context_block(as_me=True)
    assert "Do not use oe__draft_email_as_me" in build_context_block(as_me=False)
