"""Attunement open loops: attribution, extraction gates, closure, chasing."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.attunement import open_loops
from openexecutive.memory import episodic, session_store
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.scheduler import nudge_engine


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    db = tmp_path / "test.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    audited: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: audited.append((summary, kw.get("details") or {})),
    )
    monkeypatch.setattr("openexecutive.audit.usage.log_model_usage", lambda *a, **k: None)
    yield audited
    people_registry.invalidate()


@pytest.fixture
def team() -> SimpleNamespace:
    principal = people_store.upsert_person(
        full_name="Pat Principal", is_principal=True, slack_user_id="U_PAT", preferred_channel="slack"
    )
    sara = people_store.upsert_person(
        full_name="Sara Kim", slack_user_id="U_SARA", preferred_channel="slack"
    )
    ben = people_store.upsert_person(
        full_name="Ben Ortiz", slack_user_id="U_BEN", preferred_channel="slack"
    )
    return SimpleNamespace(principal=principal, sara=sara, ben=ben)


class _FakeProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def messages_create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", name="record_open_loops", input=self.payload)
        return SimpleNamespace(content=[block])


def _install_provider(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> _FakeProvider:
    fake = _FakeProvider(payload)
    monkeypatch.setattr("openexecutive.providers.get_provider", lambda model: fake)
    return fake


async def _run(msg: str, person_id: int, reply: str = "Noted.") -> dict[str, int]:
    return await open_loops.run_open_loop_pass(msg, reply, person_id=person_id, session_id="s1")


# --------------------------------------------------------------------------- #
# Attribution + feedback storage
# --------------------------------------------------------------------------- #


def test_save_message_records_sender_and_returns_id() -> None:
    db = episodic.DB_PATH
    session_store.create_session("s1", "t", "2026-01-01T00:00:00", caller_person_id=1, db_path=db)
    uid = session_store.save_message("s1", "user", "hi", db_path=db, sender_person_id=7)
    aid = session_store.save_message("s1", "assistant", "hello", db_path=db)
    assert uid and aid and aid > uid
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT sender_person_id FROM chat_messages WHERE id=?", (uid,)).fetchone()
    assert row[0] == 7
    msgs = session_store.load_messages("s1", db_path=db)
    assert msgs[0] == {"role": "user", "content": "hi"}
    assert msgs[1] == {"role": "assistant", "content": "hello", "id": aid}


def test_feedback_scoped_to_session_and_assistant_rows() -> None:
    db = episodic.DB_PATH
    session_store.create_session("s1", "t", "2026-01-01T00:00:00", db_path=db)
    session_store.create_session("s2", "t", "2026-01-01T00:00:00", db_path=db)
    uid = session_store.save_message("s1", "user", "hi", db_path=db)
    aid = session_store.save_message("s1", "assistant", "hello", db_path=db)
    assert session_store.set_message_feedback("s1", aid, "down", "too long", db_path=db)
    assert not session_store.set_message_feedback("s2", aid, "up", db_path=db)
    assert not session_store.set_message_feedback("s1", uid, "up", db_path=db)
    assert session_store.load_messages("s1", db_path=db)[1]["feedback"] == "down"
    with pytest.raises(ValueError):
        session_store.set_message_feedback("s1", aid, "meh", db_path=db)
    assert session_store.set_message_feedback("s1", aid, None, db_path=db)
    assert "feedback" not in session_store.load_messages("s1", db_path=db)[1]


# --------------------------------------------------------------------------- #
# Extraction pass
# --------------------------------------------------------------------------- #


def test_prefilter_skips_small_talk() -> None:
    assert not open_loops.should_run("thanks, that's helpful", has_open_loops=False)
    assert open_loops.should_run("I'll send the vendor quote by Thursday", has_open_loops=False)
    assert not open_loops.should_run("sent it", has_open_loops=False)
    assert open_loops.should_run("sent it", has_open_loops=True)


async def test_teammate_commitment_opens_loop_owned_by_speaker(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = (date.today() + timedelta(days=3)).isoformat()
    _install_provider(monkeypatch, {"loops": [{
        "owner": "me", "kind": "commitment", "text": "send the vendor quote",
        "due_date": due, "quote": "I'll send the vendor quote by Thursday",
    }], "closed": []})
    counts = await _run("Sure. I'll send the vendor quote by Thursday.", team.sara)
    assert counts["opened"] == 1
    [loop] = open_loops.list_open_loops()
    assert loop.owner_person_id == team.sara
    assert loop.description == "Sara Kim committed to: send the vendor quote"
    assert loop.due_at.startswith(due) or loop.due_at[:10] in {
        due, (date.fromisoformat(due) + timedelta(days=1)).isoformat()
    }


async def test_quote_not_in_speakers_message_is_dropped(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Executive offered to send it; the speaker never committed.
    _install_provider(monkeypatch, {"loops": [{
        "owner": "me", "kind": "commitment", "text": "send the deck",
        "due_date": None, "quote": "I will send the deck",
    }], "closed": []})
    counts = await _run("Can you draft the deck?", team.sara, reply="I will send the deck tomorrow.")
    assert counts == {"opened": 0, "closed": 0, "dropped": 1}
    assert open_loops.list_open_loops() == []


async def test_teammate_cannot_attribute_commitment_to_someone_else(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch, {"loops": [{
        "owner": "Ben Ortiz", "kind": "commitment", "text": "send the invoice",
        "due_date": None, "quote": "Ben will send the invoice",
    }], "closed": []})
    counts = await _run("Ben will send the invoice.", team.sara)
    assert counts["opened"] == 0 and counts["dropped"] == 1


async def test_principal_can_attribute_and_asks_route_to_named_person(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch, {"loops": [
        {"owner": "Sara", "kind": "commitment", "text": "send the Q3 numbers",
         "due_date": None, "quote": "Sara will send the Q3 numbers Monday"},
        {"owner": "Ben Ortiz", "kind": "ask", "text": "the hiring plan",
         "due_date": None, "quote": "Ben, can you get me the hiring plan?"},
        {"owner": "me", "kind": "commitment", "text": "review the budget",
         "due_date": None, "quote": "I will review the budget"},
    ], "closed": []})
    msg = "Sara will send the Q3 numbers Monday. Ben, can you get me the hiring plan? I will review the budget."
    counts = await _run(msg, team.principal)
    assert counts["opened"] == 2
    # The principal's own commitment is the episodic extractor's job.
    assert counts["dropped"] == 1
    owners = {lp.owner_person_id: lp.description for lp in open_loops.list_open_loops()}
    assert owners[team.sara] == "Sara Kim committed to: send the Q3 numbers"
    assert owners[team.ben] == "Pat Principal asked Ben Ortiz for: the hiring plan"


async def test_duplicate_loop_is_not_reopened(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"loops": [{"owner": "me", "kind": "commitment", "text": "send the quote",
                          "due_date": None, "quote": "I'll send the quote"}], "closed": []}
    _install_provider(monkeypatch, payload)
    assert (await _run("I'll send the quote.", team.sara))["opened"] == 1
    second = await _run("I'll send the quote.", team.sara)
    assert second["opened"] == 0 and second["dropped"] == 1
    assert len(open_loops.list_open_loops()) == 1


def test_unique_index_dedupes_concurrent_inserts(team: SimpleNamespace) -> None:
    due = datetime.now(UTC) + timedelta(days=1)
    results: list[int | None] = []

    def insert() -> None:
        results.append(open_loops.open_loop(owner_person_id=team.sara,
                                            description="send the quote", due_at=due))

    threads = [threading.Thread(target=insert) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1


async def test_owner_reports_done_closes_loop(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="Sara Kim committed to: send the quote",
                                   due_at=datetime.now(UTC) + timedelta(days=1))
    fake = _install_provider(monkeypatch, {"loops": [], "closed": [{"loop_id": loop_id, "quote": "sent it"}]})
    counts = await _run("Sent it this morning.", team.sara)
    assert counts["closed"] == 1
    assert open_loops.list_open_loops() == []
    # The open loop was shown to the model so it could close it.
    assert f"id={loop_id}" in fake.calls[0]["messages"][0]["content"]


async def test_teammate_cannot_close_someone_elses_loop(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.ben, description="Ben owes the plan",
                                   due_at=datetime.now(UTC) + timedelta(days=1))
    _install_provider(monkeypatch, {"loops": [], "closed": [{"loop_id": loop_id, "quote": "done"}]})
    counts = await _run("I'll check, but done on my side.", team.sara)
    assert counts["closed"] == 0 and counts["dropped"] == 1
    assert len(open_loops.list_open_loops()) == 1


async def test_budget_spent_skips_model_call(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_provider(monkeypatch, {"loops": [], "closed": []})
    monkeypatch.setattr(open_loops, "consume_call_budget", lambda limit, db_path=None: False)
    await _run("I'll send the quote.", team.sara)
    assert fake.calls == []


def test_call_budget_is_enforced() -> None:
    assert open_loops.consume_call_budget(2)
    assert open_loops.consume_call_budget(2)
    assert not open_loops.consume_call_budget(2)


async def test_unrostered_or_archived_speaker_does_nothing(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_provider(monkeypatch, {"loops": [], "closed": []})
    assert await _run("I'll send the quote.", 9999) == {"opened": 0, "closed": 0, "dropped": 0}
    people_store.archive_person(team.sara)
    await _run("I'll send the quote.", team.sara)
    assert fake.calls == []


def test_schedule_ignores_unresolved_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # drop conftest's no-op patch for this module-level fn
    ran: list[Any] = []
    monkeypatch.setattr(open_loops, "run_open_loop_pass", lambda *a, **k: ran.append(a))
    open_loops.schedule_open_loop_pass("I'll send it", "ok", person_id=None)
    assert ran == []


def test_due_date_clamped_and_defaulted() -> None:
    today = date(2026, 1, 10)
    far = open_loops._resolve_due("2027-01-01", today=today, default_days=2)
    assert far.date() <= today + timedelta(days=61)
    past = open_loops._resolve_due("2020-01-01", today=today, default_days=2)
    assert past.date() >= today - timedelta(days=1)
    default = open_loops._resolve_due(None, today=today, default_days=2)
    assert default > datetime.now(UTC) + timedelta(days=1, hours=23)


# --------------------------------------------------------------------------- #
# Chasing, expiry, visibility
# --------------------------------------------------------------------------- #


def test_overdue_loop_is_chased_and_future_loop_is_not(team: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    overdue = open_loops.open_loop(owner_person_id=team.sara, description="Sara Kim committed to: send the quote",
                                   due_at=now - timedelta(hours=1))
    open_loops.open_loop(owner_person_id=team.ben, description="Ben owes the plan",
                         due_at=now + timedelta(days=2))
    out = nudge_engine._select_stale_commitment_candidates(now, stale_days=3, cooldown_hours=48)
    assert [c.scope_key for c in out] == [f"{nudge_engine.SCOPE_PREFIX_COMMITMENT}:{overdue}"]
    assert out[0].person_id == team.sara
    assert "open loop" in out[0].intent_text and "send the quote" in out[0].intent_text


def test_closed_loop_is_not_chased(team: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="x committed to: y",
                                   due_at=now - timedelta(days=1))
    assert loop_id is not None
    assert open_loops.close_open_loop(loop_id, reason="done")
    assert not open_loops.close_open_loop(loop_id, reason="done")
    assert nudge_engine._select_stale_commitment_candidates(now, stale_days=3, cooldown_hours=48) == []


def test_expired_loops_close(team: SimpleNamespace) -> None:
    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="old promise",
                                   due_at=datetime.now(UTC))
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute("UPDATE scheduled_actions SET created_at=? WHERE id=?",
                     ((datetime.now(UTC) - timedelta(days=30)).isoformat(), loop_id))
    assert open_loops.expire_open_loops(ttl_days=21) == 1
    assert open_loops.list_open_loops() == []


def test_loops_count_as_awaiting_not_as_contact(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))
    assert episodic.list_awaiting_replies_by_person()[team.sara][0] == 1
    assert team.sara not in episodic.last_contact_at_by_person()
    # Never dispatched by the runner.
    assert all(a.kind != "open_loop" for a in episodic.list_pending_scheduled_actions())


def test_archiving_owner_closes_their_loops(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))
    people_store.archive_person(team.sara)
    assert open_loops.list_open_loops() == []


def test_reflection_context_lists_open_loops(team: SimpleNamespace) -> None:
    from openexecutive.workflows.executive_reflection import _render_reflection_context

    open_loops.open_loop(owner_person_id=team.sara, description="Sara Kim committed to: send the quote",
                         due_at=datetime.now(UTC) - timedelta(hours=2))
    lines = open_loops.render_for_reflection()
    text = _render_reflection_context(
        period_label="p", today_data={}, activity=[], recent_alerts=[],
        external_signals=[], open_loops=lines,
    )
    assert "OPEN LOOPS" in text and "OVERDUE" in text and "owner=Sara Kim" in text


# --------------------------------------------------------------------------- #
# Tools + API authorization
# --------------------------------------------------------------------------- #


async def test_close_tool_requires_principal_or_owner(team: SimpleNamespace) -> None:
    from openexecutive.orchestrator.open_loop_tools import handle_close_open_loop
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))

    async def call_as(caller: int | None) -> str:
        session = Session(caller_person_id=caller)
        token = current_session.set(session)
        try:
            return json.loads(await handle_close_open_loop({"loop_id": loop_id}))["status"]
        finally:
            current_session.reset(token)

    assert await call_as(None) == "refused"
    assert await call_as(team.ben) == "refused"
    assert await call_as(team.principal) == "closed"


async def _list_as(caller: int | None, *, session_id: str = "s", web: bool = False,
                   person_id: int | None = None) -> dict[str, Any]:
    from openexecutive.orchestrator.open_loop_tools import handle_list_open_loops
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    token = current_session.set(
        Session(session_id=session_id, caller_person_id=caller, from_web_chat=web)
    )
    try:
        args = {} if person_id is None else {"person_id": person_id}
        result: dict[str, Any] = json.loads(await handle_list_open_loops(args))
        return result
    finally:
        current_session.reset(token)


def _owners(result: dict[str, Any]) -> set[int]:
    return {lp["owner_person_id"] for lp in result.get("open_loops", [])}


async def test_list_tool_principal_sees_all_only_in_private(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))
    open_loops.open_loop(owner_person_id=team.ben, description="send the plan", due_at=datetime.now(UTC))
    everyone = {team.sara, team.ben}
    assert _owners(await _list_as(team.principal, web=True)) == everyone
    assert _owners(await _list_as(team.principal, session_id="slack:dm:U_PAT")) == everyone
    assert _owners(await _list_as(team.principal, session_id="discord:dm:1")) == everyone
    assert _owners(await _list_as(team.principal, session_id="telegram:42")) == everyone
    # Shared surfaces: a list there is read by everyone present.
    for shared in ("slack:channel:C1:U_PAT", "slack:thread:C1:1.2", "discord:thread:9",
                   "telegram:-100", "email:thread-1", "google_chat:spaces/x:t"):
        assert _owners(await _list_as(team.principal, session_id=shared)) == set(), shared


async def test_list_tool_teammate_sees_only_their_own(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))
    open_loops.open_loop(owner_person_id=team.ben, description="send the plan", due_at=datetime.now(UTC))
    assert _owners(await _list_as(team.sara, web=True)) == {team.sara}
    assert _owners(await _list_as(team.sara, session_id="slack:dm:U_SARA")) == {team.sara}
    assert (await _list_as(team.sara, web=True, person_id=team.ben))["status"] == "refused"
    assert (await _list_as(None, web=True))["status"] == "refused"


async def test_paraphrased_loop_text_is_dropped(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The quote is verbatim but the stored text is the model's own wording.
    _install_provider(monkeypatch, {"loops": [{
        "owner": "me", "kind": "commitment", "text": "deliver pricing to the vendor",
        "due_date": None, "quote": "I'll send the vendor quote by Thursday",
    }], "closed": []})
    counts = await _run("I'll send the vendor quote by Thursday.", team.sara)
    assert counts == {"opened": 0, "closed": 0, "dropped": 1}


def test_open_loops_route_is_principal_or_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import people as route

    def call(caller: int | None) -> int:
        monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda req: caller)
        try:
            route.get_person_open_loops(team.sara, request=None)  # type: ignore[arg-type]
            return 200
        except HTTPException as exc:
            return exc.status_code

    assert call(team.sara) == 200
    assert call(team.principal) == 200
    assert call(team.ben) == 403
    assert call(None) == 403


def test_reflection_is_not_given_the_close_tool() -> None:
    import inspect

    from openexecutive.workflows import executive_reflection

    assert '"close_open_loop"' in inspect.getsource(executive_reflection.ExecutiveReflectionWorkflow.run)


def test_feedback_route_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import sessions as route

    monkeypatch.setattr(
        "openexecutive.memory.session_store.get_session_owner", lambda sid: (True, 5)
    )
    monkeypatch.setattr(route, "set_message_feedback", lambda *a, **k: True)
    principal = SimpleNamespace(is_principal=True, archived=False)
    teammate = SimpleNamespace(is_principal=False, archived=False)
    monkeypatch.setattr("openexecutive.people.store.get_person",
                        lambda pid, db_path=None: principal if pid == 1 else teammate)
    body = route.MessageFeedback(feedback="down")

    def call(caller: int | None) -> int:
        monkeypatch.setattr(route, "_resolve_caller_person_id", lambda req: caller)
        try:
            return route.post_message_feedback("s1", 3, body, request=None).status_code  # type: ignore[arg-type]
        except HTTPException as exc:
            return exc.status_code

    assert call(5) == 204      # the session's own caller
    assert call(1) == 204      # the principal
    # Refused like an unknown session, so the route can't probe which exist.
    assert call(7) == 404      # someone else
    assert call(None) == 404   # unrostered


def test_close_open_loop_chip_only_when_closed() -> None:
    from openexecutive.orchestrator.action_chips import summarize_action

    closed = summarize_action(tool_name="close_open_loop", tool_input={"loop_id": 4},
                              tool_result=json.dumps({"status": "closed", "loop_id": 4}))
    assert closed is not None and closed["summary"] == "Closed open loop #4"
    for status in ("refused", "not_open", "not_found"):
        assert summarize_action(tool_name="close_open_loop", tool_input={"loop_id": 4},
                                tool_result=json.dumps({"status": status})) is None


def test_close_route_is_principal_or_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import people as route

    def call(caller: int | None) -> int:
        loop_id = open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                                       due_at=datetime.now(UTC))
        assert loop_id is not None
        monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda req: caller)
        try:
            route.close_open_loop_route(loop_id, route.OpenLoopClose(), request=None)  # type: ignore[arg-type]
            return 204
        except HTTPException as exc:
            open_loops.close_open_loop(loop_id, reason="cleanup")
            return exc.status_code

    assert call(team.ben) == 403
    assert call(None) == 403
    assert call(team.sara) == 204
    assert call(team.principal) == 204


def test_is_principal_or_self(team: SimpleNamespace) -> None:
    assert people_store.is_principal_or_self(team.sara, team.sara)
    assert people_store.is_principal_or_self(team.principal, team.sara)
    assert not people_store.is_principal_or_self(team.ben, team.sara)
    assert not people_store.is_principal_or_self(None, team.sara)
    # Something with no owner belongs to the principal alone.
    assert people_store.is_principal_or_self(team.principal, None)
    assert not people_store.is_principal_or_self(team.sara, None)


def test_owner_resolution_never_falls_back_from_an_unknown_full_name(team: SimpleNamespace) -> None:
    roster = people_store.list_people()
    speaker = people_store.get_person(team.principal)
    assert open_loops._resolve_owner("Ben", speaker=speaker, roster=roster).id == team.ben
    assert open_loops._resolve_owner("ben ortiz", speaker=speaker, roster=roster).id == team.ben
    # "Ben Jones from Acme" is not the rostered Ben Ortiz.
    assert open_loops._resolve_owner("Ben Jones", speaker=speaker, roster=roster) is None


def test_turn_with_attached_document_is_skipped() -> None:
    doc = "summarise this\n\n[Attached: plan.pdf]\nSara will send the numbers Monday."
    assert not open_loops.should_run(doc, has_open_loops=True)
    assert not open_loops.should_run("[Attached: a.pdf]\nI'll send it Friday\n\nthoughts?",
                                     has_open_loops=False)


def test_not_yet_due_loop_is_not_awaiting(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote",
                         due_at=datetime.now(UTC) + timedelta(days=2))
    assert team.sara not in episodic.list_awaiting_replies_by_person()
    open_loops.open_loop(owner_person_id=team.ben, description="send the plan",
                         due_at=datetime.now(UTC) - timedelta(hours=1))
    assert episodic.list_awaiting_replies_by_person()[team.ben][0] == 1


def test_activity_listing_can_skip_internal_rows(team: SimpleNamespace) -> None:
    for i in range(5):
        open_loops.open_loop(owner_person_id=team.sara, description=f"send item {i}",
                             due_at=datetime.now(UTC))
    episodic.insert_scheduled_action(run_at=datetime.now(UTC).isoformat(), channel="slack_dm",
                                     channel_ref="U_SARA", intent_text="ping", status="done")
    rows = episodic.list_scheduled_actions(status="done", limit=1, order="desc", exclude_internal=True)
    assert [r.channel for r in rows] == ["slack_dm"]


def test_engagement_followups_ignore_open_loops(team: SimpleNamespace) -> None:
    open_loops.open_loop(owner_person_id=team.sara, description="send the quote", due_at=datetime.now(UTC))
    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE status = 'done' AND kind != 'open_loop'"
        ).fetchone()[0]
    assert n == 0


def test_get_open_loop_and_archive_close_beyond_list_limit(team: SimpleNamespace) -> None:
    ids = [open_loops.open_loop(owner_person_id=team.sara, description=f"deliver thing {i}",
                                due_at=datetime.now(UTC)) for i in range(3)]
    assert open_loops.get_open_loop(ids[-1]).owner_person_id == team.sara  # type: ignore[union-attr]
    assert open_loops.close_loops_for_person(team.sara, reason="owner_archived") == 3
    assert open_loops.get_open_loop(ids[-1]) is None
