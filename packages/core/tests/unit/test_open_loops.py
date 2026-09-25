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


# --------------------------------------------------------------------------- #
# Solo mode: the principal's own DATED commitments become loops they own
# --------------------------------------------------------------------------- #

import hashlib  # noqa: E402

from openexecutive.memory import workspace_settings as ws  # noqa: E402

# sha256 of the team extraction prompt before solo existed — team is unchanged.
_TEAM_SYSTEM_SHA256 = "37a9be531f3175994bdb3af8f71e447ff36accc3c78160a8d091f540fec263f3"


def _dated_commitment(due: str | None, quote: str = "I'll send Northwind the proposal by Friday",
                      text: str = "send Northwind the proposal") -> dict[str, Any]:
    return {"loops": [{"owner": "me", "kind": "commitment", "text": text,
                       "due_date": due, "quote": quote}], "closed": []}


async def _run_mode(
    msg: str, person_id: int, mode: str | None, *, verified: bool = True
) -> dict[str, int]:
    # `verified` = the turn is the principal on a surface that verified it is
    # them (the web chat, their own Slack or Discord) — the Executive computes
    # it with people_tools.is_principal_on_verified_surface.
    return await open_loops.run_open_loop_pass(
        msg, "Noted.", person_id=person_id, session_id="s1", workspace_mode=mode,
        principal_verified=verified,
    )


async def test_solo_dated_principal_commitment_opens_a_loop_they_own(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, _isolated: list[tuple[str, dict]]
) -> None:
    due = (date.today() + timedelta(days=3)).isoformat()
    fake = _install_provider(monkeypatch, _dated_commitment(due))
    counts = await _run_mode("I'll send Northwind the proposal by Friday.", team.principal, "solo")
    assert counts == {"opened": 1, "closed": 0, "dropped": 0}
    [loop] = open_loops.list_open_loops(person_id=team.principal)
    assert loop.owner_person_id == team.principal
    assert loop.description == "Pat Principal committed to: send Northwind the proposal"
    assert loop.due_at[:10] in {due, (date.fromisoformat(due) + timedelta(days=1)).isoformat()}
    assert fake.calls[0]["system"] == open_loops._SYSTEM_SOLO
    assert any(d.get("op") == "loop_opened" and d.get("owner_person_id") == team.principal
               for _s, d in _isolated)


async def test_solo_principal_commitment_from_an_unverified_surface_is_dropped(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, _isolated: list[tuple[str, dict]]
) -> None:
    """An email's sender is its From header: a spoofed "I'll wire the deposit
    Friday" from the principal's address must not become their own promise."""
    due = (date.today() + timedelta(days=3)).isoformat()
    _install_provider(monkeypatch, _dated_commitment(due))
    counts = await _run_mode(
        "I'll send Northwind the proposal by Friday.", team.principal, "solo", verified=False
    )
    assert counts == {"opened": 0, "closed": 0, "dropped": 1}
    assert open_loops.list_open_loops() == []
    [extract] = [d for _s, d in _isolated if d.get("op") == "extract"]
    assert extract["dropped_items"] == [
        {"kind": "open", "reason": "principal_commitment_unverified"}
    ]
    # The default is fail-closed: a caller that does not say drops it too.
    counts = await open_loops.run_open_loop_pass(
        "I'll send Northwind the proposal by Friday.", "Noted.", person_id=team.principal,
        workspace_mode="solo",
    )
    assert counts["opened"] == 0


async def test_solo_contacts_loops_need_no_principal_verification(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verified-principal rule is for the principal's OWN commitments;
    what a contact promises opens as it does in team mode."""
    due = (date.today() + timedelta(days=3)).isoformat()
    _install_provider(monkeypatch, {"loops": [{
        "owner": "me", "kind": "commitment", "text": "send the invoice",
        "due_date": due, "quote": "I'll send the invoice by Friday"}], "closed": []})
    counts = await _run_mode("I'll send the invoice by Friday.", team.sara, "solo", verified=False)
    assert counts["opened"] == 1


async def test_solo_undated_principal_commitment_stays_dropped(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, _isolated: list[tuple[str, dict]]
) -> None:
    _install_provider(monkeypatch, _dated_commitment(
        None, quote="I'll look into pricing", text="look into pricing"))
    counts = await _run_mode("I'll look into pricing.", team.principal, "solo")
    assert counts == {"opened": 0, "closed": 0, "dropped": 1}
    assert open_loops.list_open_loops() == []
    [extract] = [d for _s, d in _isolated if d.get("op") == "extract"]
    assert extract["dropped_items"] == [{"kind": "open", "reason": "principal_commitment_undated"}]


async def test_solo_needs_the_date_in_the_quote_not_only_from_the_model(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model guessed a due date the principal never stated.
    guessed = (date.today() + timedelta(days=2)).isoformat()
    _install_provider(monkeypatch, _dated_commitment(
        guessed, quote="I'll draft the pitch deck", text="draft the pitch deck"))
    counts = await _run_mode("I'll draft the pitch deck.", team.principal, "solo")
    assert counts["opened"] == 0 and counts["dropped"] == 1


async def test_solo_unparseable_due_date_is_not_a_stated_one(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch, _dated_commitment("Friday"))
    counts = await _run_mode("I'll send Northwind the proposal by Friday.", team.principal, "solo")
    assert counts["opened"] == 0 and counts["dropped"] == 1


async def test_team_still_drops_a_dated_principal_commitment(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, _isolated: list[tuple[str, dict]]
) -> None:
    due = (date.today() + timedelta(days=3)).isoformat()
    fake = _install_provider(monkeypatch, _dated_commitment(due))
    counts = await _run_mode("I'll send Northwind the proposal by Friday.", team.principal, "team")
    assert counts == {"opened": 0, "closed": 0, "dropped": 1}
    assert fake.calls[0]["system"] == open_loops._SYSTEM
    [extract] = [d for _s, d in _isolated if d.get("op") == "extract"]
    assert extract["dropped_items"] == [{"kind": "open", "reason": "principal_commitment"}]


async def test_solo_teammate_loops_behave_as_in_team(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A contact's own undated commitment is still a loop (default due date).
    _install_provider(monkeypatch, {"loops": [{
        "owner": "me", "kind": "commitment", "text": "send the invoice",
        "due_date": None, "quote": "I'll send the invoice"}], "closed": []})
    counts = await _run_mode("I'll send the invoice.", team.sara, "solo")
    assert counts["opened"] == 1
    assert open_loops.list_open_loops()[0].owner_person_id == team.sara


async def test_pass_reads_the_workspace_when_no_mode_is_passed(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = (date.today() + timedelta(days=3)).isoformat()
    fake = _install_provider(monkeypatch, _dated_commitment(due))
    ws.restore_workspace_settings(ws.WorkspaceSettings(mode="solo"))
    counts = await _run_mode("I'll send Northwind the proposal by Friday.", team.principal, None)
    assert counts["opened"] == 1
    assert fake.calls[0]["system"] == open_loops._SYSTEM_SOLO


def test_team_extraction_prompt_is_unchanged_and_solo_differs() -> None:
    assert hashlib.sha256(open_loops._SYSTEM.encode()).hexdigest() == _TEAM_SYSTEM_SHA256
    assert open_loops._SYSTEM_SOLO != open_loops._SYSTEM
    assert "or the principal's own commitments." in open_loops._SYSTEM
    assert "does not say when it is due" in open_loops._SYSTEM_SOLO
    assert "{" not in open_loops._SYSTEM_SOLO  # a constant, never formatted


def test_stated_due_date_versus_default() -> None:
    assert open_loops._stated_due_date("2026-10-02") == date(2026, 10, 2)
    assert open_loops._stated_due_date(" 2026-10-02T17:00") == date(2026, 10, 2)
    for raw in (None, "", "Friday", 20261002):
        assert open_loops._stated_due_date(raw) is None


@pytest.mark.parametrize("quote", [
    "I'll send it by Friday", "I'll send it Friday", "tomorrow", "on the 14th",
    "by the 14th of October", "by the 30th.", "until the 2nd", "by Oct 3", "Oct. 3",
    "3 October", "on 3rd October", "before May 3", "by May", "2026-10-03", "10/03",
    "10/03/2026", "by 10/3", "end of Q4", "by Q1", "in 3 days", "next week", "by Wed",
    "on Sat", "this Fri", "due Thursday", "end of the month",
])
def test_stated_due_hint_matches_a_stated_date(quote: str) -> None:
    assert open_loops._STATED_DUE_HINT.search(quote), quote


@pytest.mark.parametrize("quote", [
    # Ambiguous words with no due word before them.
    "I may send the proposal", "I'll not mar the finish", "I march on", "I sat down to write it",
    "I'll get some sun first", "we'll wed the two plans", "q1 numbers look fine",
    # Ordinals that name a thing, not a day; ratios.
    "I'll send the 1st draft", "I'll work on the 1st draft", "I'll fix the 3rd section",
    "It's a 50/50 call", "half is 1/2 done",
    # No date at all.
    "I'll look into pricing", "I will review the budget", "by the way I'll send it",
])
def test_stated_due_hint_ignores_ambiguous_tokens(quote: str) -> None:
    match = open_loops._STATED_DUE_HINT.search(quote)
    assert match is None, (quote, match.group(0) if match else None)


def test_schedule_forwards_the_turns_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # drop conftest's no-op patch for this module-level fn
    seen: list[dict[str, Any]] = []

    async def _fake_pass(*a: Any, **k: Any) -> dict[str, int]:
        seen.append(k)
        return {}

    monkeypatch.setattr(open_loops, "run_open_loop_pass", _fake_pass)
    open_loops.schedule_open_loop_pass(
        "I'll send it by Friday", "ok", person_id=1, workspace_mode="solo",
        principal_verified=True,
    )
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.02)
    assert seen and seen[0]["workspace_mode"] == "solo"
    assert seen[0]["principal_verified"] is True


def test_principal_owned_overdue_loop_is_chased_with_the_principal(team: SimpleNamespace) -> None:
    now = datetime.now(UTC)
    loop_id = open_loops.open_loop(
        owner_person_id=team.principal,
        description="Pat Principal committed to: send Northwind the proposal",
        due_at=now - timedelta(hours=1),
    )
    out = nudge_engine._select_stale_commitment_candidates(now, stale_days=3, cooldown_hours=48)
    assert [c.scope_key for c in out] == [f"{nudge_engine.SCOPE_PREFIX_COMMITMENT}:{loop_id}"]
    assert out[0].person_id == team.principal


# --------------------------------------------------------------------------- #
# Solo brief: DUE THIS WEEK
# --------------------------------------------------------------------------- #


def _principal_loop(team: SimpleNamespace, text: str, due: datetime) -> int:
    loop_id = open_loops.open_loop(
        owner_person_id=team.principal, description=f"Pat Principal committed to: {text}",
        due_at=due,
    )
    assert loop_id is not None
    return loop_id


def test_principal_due_soon_lists_overdue_and_this_week_only(team: SimpleNamespace) -> None:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    late = _principal_loop(team, "renew the domain", now - timedelta(days=2))
    today = _principal_loop(team, "send the proposal", now + timedelta(hours=3))
    soon = _principal_loop(team, "file the VAT return", now + timedelta(days=3))
    _principal_loop(team, "plan the offsite", now + timedelta(days=10))
    open_loops.open_loop(owner_person_id=team.sara, description="Sara Kim committed to: x y z",
                         due_at=now + timedelta(days=1))
    due = open_loops.principal_due_soon(now=now)
    assert [(d["loop_id"], d["state"]) for d in due] == [
        (late, "overdue"), (today, "today"), (soon, "soon"),
    ]
    assert due[0]["due_date"] == "2026-09-23"
    assert due[1]["description"] == "Pat Principal committed to: send the proposal"
    assert open_loops.principal_due_soon(now=now, limit=1) == due[:1]


def test_principal_due_soon_is_empty_without_a_principal() -> None:
    people_store.upsert_person(full_name="Sara Kim")
    assert open_loops.principal_due_soon() == []


def _due_rows() -> list[dict[str, Any]]:
    return [
        {"loop_id": 4, "description": "Pat Principal committed to: renew the domain",
         "due_at": "2026-09-23T17:00:00+00:00", "due_date": "2026-09-23", "state": "overdue"},
        {"loop_id": 5, "description": "Pat Principal committed to: send the proposal",
         "due_at": "2026-09-28T17:00:00+00:00", "due_date": "2026-09-28", "state": "soon"},
    ]


def test_brief_context_renders_due_this_week_in_solo_only() -> None:
    from openexecutive.briefing.narrative import render_briefing_context

    data = {"departments": [], "people": [], "proposals": [], "due_soon": _due_rows()}
    solo = render_briefing_context(period_label="p", today_data=data, activity=[], mode="solo")
    assert "DUE THIS WEEK" in solo
    assert "- OVERDUE (was due 2026-09-23): Pat Principal committed to: renew the domain" in solo
    assert "- due Mon 2026-09-28: Pat Principal committed to: send the proposal" in solo
    team = render_briefing_context(period_label="p", today_data=data, activity=[])
    assert "DUE THIS WEEK" not in team
    assert team == render_briefing_context(
        period_label="p", today_data={**data, "due_soon": []}, activity=[]
    )
    quiet = render_briefing_context(
        period_label="p", today_data={**data, "due_soon": []}, activity=[], mode="solo"
    )
    assert "DUE THIS WEEK" not in quiet


def test_solo_standalone_brief_prompt_has_a_due_section() -> None:
    from openexecutive.briefing.narrative import (
        BRIEFING_NARRATIVE_SOLO_SYSTEM,
        STANDALONE_BRIEF_SOLO_SYSTEM,
        STANDALONE_BRIEF_SYSTEM,
    )

    assert "**Due this week**" in STANDALONE_BRIEF_SOLO_SYSTEM
    assert "DUE THIS WEEK" in STANDALONE_BRIEF_SOLO_SYSTEM
    assert "Due this week" not in STANDALONE_BRIEF_SYSTEM
    assert "what's due this week" in BRIEFING_NARRATIVE_SOLO_SYSTEM


def test_today_header_counts_due_items_in_solo(team: SimpleNamespace) -> None:
    from openexecutive.api.routes import today as today_route

    _principal_loop(team, "send the proposal", datetime.now(UTC) + timedelta(days=1))
    empty: dict[str, Any] = {"departments": [], "people": [], "proposals": []}
    solo = today_route._with_due_soon(empty, "solo", None)
    assert [d["state"] for d in solo["due_soon"]] == ["soon"]
    assert "due_soon" not in empty  # a copy
    assert today_route._nothing_needs_attention(solo, "solo") is False
    assert today_route._nothing_needs_attention(empty, "solo") is True
    # Team, and a teammate's scoped view, never get the block.
    assert today_route._with_due_soon(empty, "team", None) is empty
    assert today_route._with_due_soon(empty, "solo", {"name": "x", "role": "y"}) is empty
    ctx, _activity = today_route._narrative_context(empty, None, None, "team")
    assert "DUE THIS WEEK" not in ctx


def test_brief_fingerprint_moves_with_due_items_in_solo_only() -> None:
    from openexecutive.briefing.brief_state import build_brief_fingerprint

    base: dict[str, Any] = {"departments": [], "people": [], "proposals": []}
    kw: dict[str, Any] = dict(activity=[], handled=[], since=None)
    with_due = {**base, "due_soon": _due_rows()}
    assert build_brief_fingerprint(today_data=with_due, **kw) == build_brief_fingerprint(
        today_data=base, **kw
    )
    solo_none = build_brief_fingerprint(today_data=base, mode="solo", **kw)
    solo_due = build_brief_fingerprint(today_data=with_due, mode="solo", **kw)
    assert solo_due != solo_none
    assert build_brief_fingerprint(today_data={**base, "due_soon": []}, mode="solo", **kw) == solo_none
    went_overdue = [dict(r, state="overdue") for r in _due_rows()]
    assert build_brief_fingerprint(
        today_data={**base, "due_soon": went_overdue}, mode="solo", **kw
    ) != solo_due


def test_solo_morning_brief_carries_the_due_items(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from unittest.mock import MagicMock

    from openexecutive.api.routes import today as today_route
    from openexecutive.api.routes.today import ActivityResponse, TodayResponse
    from openexecutive.briefing import narrative as briefing_narrative
    from openexecutive.workflows.morning_brief import MorningBriefInput, MorningBriefWorkflow

    _principal_loop(team, "send the proposal", datetime.now(UTC) + timedelta(days=1))
    captured: dict[str, Any] = {}

    async def _synth(**kw: Any) -> str:
        captured.update(kw)
        return "BRIEF"

    monkeypatch.setattr(briefing_narrative, "synthesize_briefing_narrative", _synth)
    # The brief's bookkeeping reads other stores; keep them out of this test
    # (and out of ./episodic_memory.db).
    from openexecutive.briefing import brief_state

    monkeypatch.setattr(brief_state, "since_for",
                        lambda kind, now=None: datetime.now(UTC) - timedelta(days=1))
    monkeypatch.setattr(brief_state, "last_delivered", lambda kind: None)
    monkeypatch.setattr(brief_state, "handled_since", lambda since, limit=20: [])
    monkeypatch.setattr(brief_state, "pending_watch_suggestions", lambda: 0)
    monkeypatch.setattr(today_route, "_build_today",
                        lambda: TodayResponse(departments=[], people=[], proposals=[]))
    monkeypatch.setattr(today_route, "_build_activity",
                        lambda limit, since=None: ActivityResponse(items=[]))

    async def _drain(mode: str) -> None:
        captured.clear()
        ws.restore_workspace_settings(ws.WorkspaceSettings(mode=mode))  # type: ignore[arg-type]
        [e async for e in MorningBriefWorkflow().run(MorningBriefInput(force_full=True), MagicMock())]

    asyncio.run(_drain("solo"))
    assert [d["description"] for d in captured["today_data"]["due_soon"]] == [
        "Pat Principal committed to: send the proposal"
    ]
    asyncio.run(_drain("team"))
    assert "due_soon" not in captured["today_data"]


def test_solo_commitment_eval_scenario_is_shipped_and_valid() -> None:
    from openexecutive.evals.scenarios import validate_scenario_yaml

    path = Path(open_loops.__file__).parents[1] / "evals" / "_scenarios" / "attunement_002.yaml"
    scenario = validate_scenario_yaml(path.read_text(encoding="utf-8"))
    assert scenario["workspace_mode"] == "solo"
    assert "by Friday" in scenario["query"]
