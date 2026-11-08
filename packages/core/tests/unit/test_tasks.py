"""Tasks: the open-loop rows as a list people manage (statuses, notes, edits
with a conflict check, done history, reopen), the /tasks routes and the chat
tools that change and reopen them."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from openexecutive.attunement import open_loops
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    yield
    people_registry.invalidate()


@pytest.fixture
def team() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Pat Principal", is_principal=True)
    sara = people_store.upsert_person(full_name="Sara Kim")
    ben = people_store.upsert_person(full_name="Ben Ortiz")
    return SimpleNamespace(principal=principal, sara=sara, ben=ben)


def _task(owner: int, text: str = "send the vendor quote", days: int = 2) -> int:
    loop_id = open_loops.open_loop(
        owner_person_id=owner, description=f"Pat Principal asked Sara Kim for: {text}",
        due_at=datetime.now(UTC) + timedelta(days=days),
    )
    assert loop_id is not None
    return loop_id


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_title_drops_who_owes_it() -> None:
    assert open_loops.task_title("Sara Kim committed to: send the quote") == "send the quote"
    assert open_loops.task_title("Pat asked Sara Kim for: the Q3 numbers") == "the Q3 numbers"
    assert open_loops.task_title("Just a note") == "Just a note"


def test_done_keeps_history_and_reopen_restores(team: SimpleNamespace) -> None:
    loop_id = _task(team.sara)
    assert open_loops.close_open_loop(loop_id, reason="done", closed_by_person_id=team.sara)
    done = open_loops.get_task(loop_id)
    assert done is not None and done.status == "done" and done.completed_at and done.closed_reason == "done"
    assert done.due_at  # the due time it had is kept
    assert open_loops.list_tasks(state="open")[1] == 0
    assert [t.id for t in open_loops.list_tasks(state="done")[0]] == [loop_id]

    change = open_loops.reopen_task(loop_id, reopened_by_person_id=team.principal)
    assert change.reason is None and change.task is not None
    assert change.task.status == "open" and change.task.completed_at is None
    assert change.task.due_at == done.due_at  # still ahead, so the same due time
    assert open_loops.reopen_task(loop_id, reopened_by_person_id=team.principal).reason == "open"


def test_reopen_refuses_a_second_copy(team: SimpleNamespace) -> None:
    first = _task(team.sara)
    open_loops.close_open_loop(first, reason="done")
    _task(team.sara)  # the same task opened again meanwhile
    assert open_loops.reopen_task(first, reopened_by_person_id=team.principal).reason == "duplicate"


def test_reopen_refuses_an_archived_owner_or_one_at_cap(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    left = _task(team.sara)
    open_loops.close_open_loop(left, reason="done")
    people_store.archive_person(team.sara)
    people_registry.invalidate()
    assert open_loops.reopen_task(left, reopened_by_person_id=team.principal).reason == "owner_archived"

    full = _task(team.ben, "the Q3 numbers")
    open_loops.close_open_loop(full, reason="done")
    _task(team.ben, "the board deck")
    monkeypatch.setenv("ATTUNEMENT_MAX_OPEN_LOOPS_PER_PERSON", "1")
    assert open_loops.reopen_task(full, reopened_by_person_id=team.principal).reason == "owner_at_cap"


def test_update_words_notes_status_due_and_owner(team: SimpleNamespace) -> None:
    loop_id = _task(team.sara)
    due = date.today() + timedelta(days=5)
    change = open_loops.update_task(
        loop_id, edited_by_person_id=team.principal, title="three vendor quotes",
        notes="Under $400 a chair", status="waiting", due_date=due,
    )
    assert change.reason is None and change.task is not None
    t = change.task
    assert t.description == "Pat Principal asked Sara Kim for: three vendor quotes"
    assert (t.notes, t.status) == ("Under $400 a chair", "waiting")
    assert open_loops._due_date_of(t) == due.isoformat()

    moved = open_loops.update_task(loop_id, edited_by_person_id=team.principal, owner_person_id=team.ben).task
    assert moved is not None and moved.owner_person_id == team.ben
    assert moved.description == "Pat Principal asked Ben Ortiz for: three vendor quotes"
    mine = open_loops.update_task(loop_id, edited_by_person_id=team.ben, owner_person_id=team.ben)
    assert mine.task is not None and mine.task.owner_person_id == team.ben  # no change, no error


def test_update_refusals(team: SimpleNamespace) -> None:
    loop_id = _task(team.sara)
    assert open_loops.update_task(loop_id, edited_by_person_id=team.sara, title=" ").reason == "missing_text"
    assert open_loops.update_task(loop_id, edited_by_person_id=team.sara, status="later").reason == "bad_status"
    assert open_loops.update_task(999, edited_by_person_id=team.sara, notes="x").reason == "not_found"
    open_loops.close_open_loop(loop_id, reason="done")
    assert open_loops.update_task(loop_id, edited_by_person_id=team.sara, notes="x").reason == "closed"


def test_conflict_only_on_a_field_someone_else_changed(team: SimpleNamespace) -> None:
    loop_id = _task(team.sara)
    # Ben changes the notes; Sara, who saw the old (empty) notes, edits them too.
    open_loops.update_task(loop_id, edited_by_person_id=team.principal, notes="Ben's note")
    clash = open_loops.update_task(
        loop_id, edited_by_person_id=team.sara, notes="Sara's note", base={"notes": ""},
    )
    assert clash.reason == "conflict" and clash.fields == ("notes",)
    assert clash.task is not None and clash.task.notes == "Ben's note"
    # A different field with its own base still applies.
    ok = open_loops.update_task(
        loop_id, edited_by_person_id=team.sara, status="in_progress", base={"status": "open"},
    )
    assert ok.reason is None and ok.task is not None and ok.task.status == "in_progress"


def test_list_filters_and_pages(team: SimpleNamespace) -> None:
    ids = [_task(team.sara, f"task number {i}", days=i + 1) for i in range(3)]
    _task(team.ben, "Ben's own task")
    open_loops.update_task(ids[1], edited_by_person_id=team.sara, status="waiting")
    page, total = open_loops.list_tasks(owner_person_id=team.sara, limit=2)
    assert total == 3 and [t.id for t in page] == ids[:2]
    assert [t.id for t in open_loops.list_tasks(owner_person_id=team.sara, limit=2, offset=2)[0]] == ids[2:]
    assert [t.id for t in open_loops.list_tasks(status="waiting")[0]] == [ids[1]]


def test_assign_saves_notes(team: SimpleNamespace) -> None:
    result = open_loops.assign_open_loop(
        owner_person_id=team.sara, text="book the venue", assigned_by_person_id=team.principal,
        notes="Somewhere near the office",
    )
    assert result.loop_id is not None
    task = open_loops.get_task(result.loop_id)
    assert task is not None and task.notes == "Somewhere near the office"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def as_caller(monkeypatch: pytest.MonkeyPatch) -> Any:
    from openexecutive.api.routes import chat as chat_route

    def set_caller(person_id: int | None) -> None:
        monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda req: person_id)

    async def no_notice(**kwargs: Any) -> Any:
        return SimpleNamespace(as_dict=lambda for_principal: {"status": "skipped"})

    monkeypatch.setattr("openexecutive.attunement.assignment_notice.notify_assignee", no_notice)
    return set_caller


def _status(fn: Any) -> int:
    try:
        fn()
        return 200
    except HTTPException as exc:
        return exc.status_code


def test_principal_sees_everyone_a_teammate_only_their_own(team: SimpleNamespace, as_caller: Any) -> None:
    from openexecutive.api.routes import people as route

    sara_task = _task(team.sara)
    _task(team.ben, "Ben's own task")
    as_caller(team.principal)
    assert route.list_tasks_route(request=None).total == 2  # type: ignore[arg-type]
    assert route.list_tasks_route(request=None, owner_person_id=team.ben).total == 1  # type: ignore[arg-type]
    as_caller(team.sara)
    # Asking for Ben's still lists only her own.
    listed = route.list_tasks_route(request=None, owner_person_id=team.ben)  # type: ignore[arg-type]
    assert [t.task_id for t in listed.tasks] == [sara_task]
    as_caller(team.ben)
    assert _status(lambda: route.get_task_route(sara_task, request=None)) == 404  # type: ignore[arg-type]
    as_caller(None)
    assert _status(lambda: route.list_tasks_route(request=None)) == 403  # type: ignore[arg-type]


def test_patch_conflict_returns_the_latest(team: SimpleNamespace, as_caller: Any) -> None:
    from openexecutive.api.routes import people as route

    loop_id = _task(team.sara)
    as_caller(team.principal)
    asyncio.run(route.update_task_route(loop_id, route.TaskUpdate(notes="first"), request=None))  # type: ignore[arg-type]
    as_caller(team.sara)
    body = route.TaskUpdate(notes="second", base=route.TaskBase(notes=""))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route.update_task_route(loop_id, body, request=None))  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["fields"] == ["notes"] and detail["task"]["notes"] == "first"


def test_done_and_reopen_routes(team: SimpleNamespace, as_caller: Any) -> None:
    from openexecutive.api.routes import people as route

    loop_id = _task(team.sara)
    as_caller(team.ben)
    assert _status(lambda: route.complete_task_route(loop_id, route.TaskClose(), request=None)) == 404  # type: ignore[arg-type]
    as_caller(team.sara)
    done = route.complete_task_route(loop_id, route.TaskClose(), request=None)  # type: ignore[arg-type]
    assert done.status == "done" and done.completed_at
    assert route.list_tasks_route(request=None, state="done").total == 1  # type: ignore[arg-type]
    reopened = route.reopen_task_route(loop_id, request=None)  # type: ignore[arg-type]
    assert reopened.status == "open"


def test_create_route_saves_before_answering(team: SimpleNamespace, as_caller: Any) -> None:
    from openexecutive.api.routes import people as route

    as_caller(team.principal)
    out = asyncio.run(route.create_task_route(  # type: ignore[arg-type]
        route.TaskCreate(task="send the board deck", owner_person_id=team.sara, notes="v2"), request=None))
    assert out.title == "send the board deck" and out.notes == "v2" and out.owner_person_id == team.sara
    assert open_loops.get_task(out.task_id) is not None
    with pytest.raises(HTTPException) as exc:  # the same task again is refused, not doubled
        asyncio.run(route.create_task_route(  # type: ignore[arg-type]
            route.TaskCreate(task="send the board deck", owner_person_id=team.sara), request=None))
    assert exc.value.status_code == 409


# --------------------------------------------------------------------------- #
# Chat tools
# --------------------------------------------------------------------------- #


def _in_turn(monkeypatch: pytest.MonkeyPatch, caller: int | None) -> None:
    from openexecutive.orchestrator.schedule_tools import current_session

    session = SimpleNamespace(caller_person_id=caller, from_web_chat=True, shared_chat=False, session_id="s1")
    token = current_session.set(session)  # type: ignore[arg-type]
    monkeypatch.setattr("openexecutive.orchestrator.open_loop_tools._in_speakers_words",
                        lambda text, session: True)
    _tokens.append(token)


_tokens: list[Any] = []


@pytest.fixture(autouse=True)
def _reset_session() -> Any:
    yield
    from openexecutive.orchestrator.schedule_tools import current_session

    while _tokens:
        current_session.reset(_tokens.pop())


def test_chat_update_and_reopen(team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import open_loop_tools as tools

    loop_id = _task(team.sara)
    _in_turn(monkeypatch, team.ben)
    refused = json.loads(asyncio.run(tools.handle_update_open_loop({"loop_id": loop_id, "status": "waiting"})))
    assert refused["status"] == "refused"

    _in_turn(monkeypatch, team.sara)
    changed = json.loads(asyncio.run(tools.handle_update_open_loop(
        {"loop_id": loop_id, "status": "in_progress", "notes": "two quotes in"})))
    assert changed["status"] == "changed" and changed["task_status"] == "in_progress"
    open_loops.close_open_loop(loop_id, reason="done")
    listed = json.loads(asyncio.run(tools.handle_list_open_loops({"state": "done"})))
    assert [t["loop_id"] for t in listed["open_loops"]] == [loop_id]
    reopened = json.loads(asyncio.run(tools.handle_reopen_open_loop({"loop_id": loop_id})))
    assert reopened["status"] == "reopened"


def test_task_requests_go_to_tasks_not_alerts() -> None:
    from openexecutive.orchestrator.alert_tools import CREATE_ALERT_TOOL
    from openexecutive.orchestrator.open_loop_tools import ASSIGN_OPEN_LOOP_TOOL

    assert "Never use it for a task" in CREATE_ALERT_TOOL["description"]
    assert "never create_alert" in ASSIGN_OPEN_LOOP_TOOL["description"]


def test_chips_for_update_and_reopen() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    chip = summarize_action(tool_name="update_open_loop", tool_input={"loop_id": 1},
                            tool_result=json.dumps({"status": "changed"}))
    assert chip is not None and chip["summary"] == "Updated a task"
    assert summarize_action(tool_name="reopen_open_loop", tool_input={"loop_id": 1},
                            tool_result=json.dumps({"status": "not_reopened"})) is None
