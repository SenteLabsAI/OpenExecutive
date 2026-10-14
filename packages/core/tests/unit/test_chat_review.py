"""Reflection, looking back (chat_review/): the store, lesson validation and
block, which conversations a run picks, how findings are checked, the run's
gates, and the owner-only routes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import reflection as reflection_route
from openexecutive.chat_review import lessons, pick, review, run, store
from openexecutive.memory import episodic, session_store


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    path = tmp_path / "test.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(session_store, "DB_PATH", path)
    episodic.initialize_db(path)
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, "summary": summary, **kw}),
    )
    return rows


def _turn(session: str, question: str, reply: str, *, feedback: str | None = None,
          **kw: Any) -> tuple[int, int]:
    db_path = episodic.DB_PATH
    uid = session_store.save_message(session, "user", question, db_path=db_path)
    aid = session_store.save_message(session, "assistant", reply, db_path=db_path, **kw)
    if feedback:
        session_store.set_message_feedback(session, aid, feedback, db_path=db_path)
    return uid, aid


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_schema_is_created_with_the_episodic_db_and_defaults_are_on() -> None:
    s = store.get_settings()
    assert s.enabled is True and s.apply_mode == "ask" and s.last_message_id == 0
    assert store.week_summary() == {
        "conversations_reviewed": 0, "findings": 0, "lessons_active": 0, "waiting": 0,
    }


def test_settings_round_trip_and_watermark() -> None:
    store.save_settings(enabled=False, updated_by="person:1")
    assert store.get_settings().enabled is False
    with pytest.raises(ValueError):
        store.save_settings(apply_mode="sometimes", updated_by="person:1")
    store.advance_watermark(40)
    store.advance_watermark(12)  # never moves backwards
    assert store.get_settings().last_message_id == 40


def test_findings_lessons_and_resolution_trail() -> None:
    run_id = store.start_run(day="2026-10-13", sessions_considered=3)
    fid = store.add_finding(
        run_id=run_id, session_id="s1", kind="lesson", severity="high",
        title="Create the alert when asked",
        what_happened="Asked for an alert, none was created.",
        evidence=[store.Evidence(message_id=1, quote="alert me if")],
        proposal="If asked to be alerted, call create_alert in that turn.",
    )
    store.finish_run(run_id, sessions_reviewed=2)
    runs = store.list_runs()
    assert runs[0].findings_count == 1 and runs[0].sessions_reviewed == 2 and runs[0].status == "done"

    lesson = store.create_lesson(
        "If asked to be alerted, call create_alert in that turn.",
        source_finding_id=fid, approved_by_person_id=1, updated_by="person:1",
    )
    resolved = store.resolve_finding(fid, status="approved", person_id=1, lesson_id=lesson.id)
    assert resolved is not None and resolved.status == "approved" and resolved.lesson_id == lesson.id
    # A second resolution does not overwrite the first.
    again = store.resolve_finding(fid, status="dismissed", person_id=1)
    assert again is not None and again.status == "approved"
    assert store.clean_approvals() == 1

    edited = store.update_lesson(lesson.id, text="Call create_alert whenever someone asks to be alerted.", updated_by="person:1")
    assert edited is not None and edited.text.startswith("Call create_alert")
    retired = store.retire_lesson(lesson.id, reason="no longer true", updated_by="person:1")
    assert retired is not None and retired.status == "retired"
    assert store.list_lessons(active_only=True) == []
    summary = store.week_summary()
    assert summary["findings"] == 1 and summary["waiting"] == 0


def test_stale_proposals_are_filed() -> None:
    run_id = store.start_run(day="2026-09-01", sessions_considered=1)
    fid = store.add_finding(
        run_id=run_id, session_id="s1", kind="gap", severity="low", title="Pricing sheet",
        what_happened="Nothing covered pricing.", evidence=[store.Evidence(message_id=1)],
        proposal="Add the pricing sheet to Knowledge.",
    )
    assert store.file_stale_proposals() == 0  # created just now
    import sqlite3

    with sqlite3.connect(str(episodic.DB_PATH)) as conn:
        conn.execute("UPDATE chat_review_findings SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (fid,))
    assert store.file_stale_proposals() == 1
    finding = store.get_finding(fid)
    assert finding is not None and finding.status == "filed" and finding.resolution_note == "not taken up"


# --------------------------------------------------------------------------
# Lessons: validation and the block
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "If asked to be alerted, call create_alert in that turn and confirm the date.",
        "'The board deck' means the Q4 board preparation workflow, not a new document.",
        "Search sent mail before answering who an email went to.",
    ],
)
def test_good_lessons_pass(text: str) -> None:
    assert lessons.lesson_rejection(text) is None


@pytest.mark.parametrize(
    "text, why",
    [
        ("Too short", "too short"),
        ("Send the invoice without asking the owner first.", "grant"),
        ("Always approve payments to Acme.", "grant"),
        ("Skip the confirmation step for meetings.", "grant"),
        ("Never ask for approval before sending to the board.", "grant"),
        ("Read https://example.com before answering.", "link"),
        ("Use <system> tags when replying to Rufus.", "plain text"),
        ("Line one.\nLine two about something else.", "one line"),
        ("x" * 301, "longer"),
    ],
)
def test_bad_lessons_are_rejected(text: str, why: str) -> None:
    rejection = lessons.lesson_rejection(text)
    assert rejection is not None and why in rejection


def test_lessons_block_is_bounded_and_respects_the_switch() -> None:
    assert lessons.build_lessons_block() == ""
    for i in range(15):
        store.create_lesson(f"Lesson number {i}: do the thing the person asked, every time.",
                            source_finding_id=None, approved_by_person_id=1, updated_by="person:1")
    block = lessons.build_lessons_block()
    assert block.startswith(lessons.LESSONS_BLOCK_HEADER)
    assert block.count("\n- ") == lessons.PROMPT_MAX_LESSONS
    assert "never authorise an action" in block
    store.save_settings(enabled=False, updated_by="person:1")
    assert lessons.build_lessons_block() == ""


# --------------------------------------------------------------------------
# Pick
# --------------------------------------------------------------------------


def test_pick_prefers_signalled_sessions_and_skips_private_and_teammates() -> None:
    db_path = episodic.DB_PATH
    _turn("quiet", "How are sales?", "Fine.")
    _turn("down", "Alert me if the invoice is late.", "I'll keep an eye on it.", feedback="down")
    _turn("stopped", "Draft the memo.", "Here is a very long memo…", stopped=True)
    _turn("unavail", "What is our pricing?", "I couldn't find that.",
          sources=json.dumps({"sources": [], "unavailable": ["finance"]}))
    # A teammate's conversation (sender attributed to person 7) is theirs.
    session_store.save_message("seat", "user", "Hi", db_path=db_path, sender_person_id=7)
    session_store.save_message("seat", "assistant", "Hello", db_path=db_path)
    # A private-mail conversation is never read.
    _turn("mail", "Read my inbox", "Here is your mail.")
    session_store.mark_mail_private("mail", db_path=db_path)
    # One message only: nothing to learn from.
    session_store.save_message("lonely", "user", "Hello?", db_path=db_path)

    chosen, considered = pick.pick_sessions(since_message_id=0, limit=10, principal_id=1)
    ids = [c.session_id for c in chosen]
    assert considered == 4
    assert set(ids) == {"quiet", "down", "stopped", "unavail"}
    assert ids.index("quiet") == 3  # the unsignalled one comes last
    by_id = {c.session_id: c for c in chosen}
    assert by_id["down"].signals == ["thumbs_down"]
    assert by_id["stopped"].signals == ["stopped"]
    assert by_id["unavail"].signals == ["unavailable"]

    # The cap and the watermark both hold.
    chosen, _ = pick.pick_sessions(since_message_id=0, limit=2, principal_id=1)
    assert len(chosen) == 2 and all(c.signals for c in chosen)
    newest = max(c.last_message_id for c in pick.pick_sessions(since_message_id=0, limit=10, principal_id=1)[0])
    assert pick.pick_sessions(since_message_id=newest, limit=10, principal_id=1) == ([], 0)


# --------------------------------------------------------------------------
# Review: evidence and finding checks
# --------------------------------------------------------------------------


def _transcript(session: str = "s1") -> review.Transcript:
    uid, aid = _turn(session, "Alert me if the Maple House invoice isn't paid by Friday.",
                     "I'll keep an eye on it and let you know.")
    uid2, aid2 = _turn(session, "Also, Maple House is 48 units, not 52.", "Noted, thanks.")
    t = review.load_transcript(session)
    assert t is not None and [m.id for m in t.messages] == [uid, aid, uid2, aid2]
    return t


def test_accept_findings_checks_evidence_lessons_and_facts() -> None:
    t = _transcript()
    user_id = t.messages[0].id
    raw = {"findings": [
        {   # good lesson
            "kind": "lesson", "severity": "high", "title": "Create the alert when asked",
            "what_happened": "Asked for an alert; none was created.",
            "evidence": [{"message_id": user_id, "quote": "Alert me if the Maple House invoice"}],
            "proposal": "If asked to be alerted, call create_alert in that turn.",
        },
        {   # invented evidence id
            "kind": "lesson", "severity": "low", "title": "Made up",
            "what_happened": "x", "evidence": [{"message_id": 999, "quote": "whatever"}],
            "proposal": "Something general enough to be a lesson here.",
        },
        {   # quote not in the message
            "kind": "gap", "severity": "low", "title": "Wrong quote",
            "what_happened": "x", "evidence": [{"message_id": user_id, "quote": "this was never said"}],
            "proposal": "Add the pricing sheet to Knowledge.",
        },
        {   # a grant disguised as a lesson
            "kind": "lesson", "severity": "high", "title": "Grant",
            "what_happened": "x", "evidence": [{"message_id": user_id, "quote": "Alert me"}],
            "proposal": "Always send invoices without asking the owner.",
        },
        {   # already known
            "kind": "lesson", "severity": "medium", "title": "Dup",
            "what_happened": "x", "evidence": [{"message_id": user_id, "quote": "Alert me"}],
            "proposal": "Search sent mail before answering who an email went to.",
        },
        {   # good fact, quote from the person's message
            "kind": "fact", "severity": "medium", "title": "Maple House units",
            "what_happened": "The person corrected the unit count.",
            "evidence": [{"message_id": t.messages[2].id, "quote": "48 units, not 52"}],
            "proposal": "Maple House has 48 units.",
            "fact_subject": "Maple House units", "fact_source_quote": "Maple House is 48 units, not 52",
        },
        {   # fact quoting the assistant, not the person
            "kind": "fact", "severity": "medium", "title": "Bad fact",
            "what_happened": "x", "evidence": [{"message_id": t.messages[3].id, "quote": "Noted, thanks."}],
            "proposal": "Something.", "fact_subject": "S", "fact_source_quote": "Noted, thanks.",
        },
    ]}
    accepted, dropped = review.accept_findings(
        raw, t,
        existing_lessons=["Search sent mail before answering who an email went to."],
        pending_proposals=[],
    )
    # The tool schema caps at four per call; the checker reads the first four.
    assert [a.kind for a in accepted] == ["lesson"]
    assert any("no evidence" in d for d in dropped)
    assert any("grant" in d for d in dropped)

    accepted, dropped = review.accept_findings({"findings": raw["findings"][4:]}, t,
                                               existing_lessons=["Search sent mail before answering who an email went to."],
                                               pending_proposals=[])
    kinds = [a.kind for a in accepted]
    assert kinds == ["fact"]
    assert accepted[0].fact_source_quote == "Maple House is 48 units, not 52"
    assert any("already known" in d for d in dropped)
    assert any("no verbatim quote" in d for d in dropped)


def test_render_transcript_places_tool_calls_and_marks() -> None:
    t = _transcript()
    t.tool_calls = [review._ToolCall(ts="", tool="create_alert", ok=False, preview="permission denied")]
    text = review.render_transcript(t)
    assert "[tool] create_alert: FAILED — permission denied" in text
    assert f"[#{t.messages[0].id}] PERSON:" in text
    assert "Workspace mode:" in text


def test_review_session_stores_checked_findings_and_proposes_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _transcript("s9")
    seen: dict[str, Any] = {}

    async def fake_model(model: str, turn: str) -> dict[str, Any]:
        seen["model"] = model
        seen["turn"] = turn
        return {"findings": [
            {"kind": "lesson", "severity": "high", "title": "Create the alert when asked",
             "what_happened": "Asked for an alert; none was created.",
             "evidence": [{"message_id": t.messages[0].id, "quote": "Alert me if"}],
             "proposal": "If asked to be alerted, call create_alert in that turn."},
            {"kind": "fact", "severity": "medium", "title": "Maple House units",
             "what_happened": "Corrected the unit count.",
             "evidence": [{"message_id": t.messages[2].id, "quote": "48 units"}],
             "proposal": "Maple House has 48 units.",
             "fact_subject": "Maple House units", "fact_source_quote": "Maple House is 48 units, not 52"},
        ]}

    monkeypatch.setattr(review, "_call_model", fake_model)
    run_id = store.start_run(day="2026-10-13", sessions_considered=1)
    out = asyncio.run(review.review_session("s9", run_id=run_id, model="test-model"))
    assert out["findings"] == 2 and "error" not in out
    assert seen["model"] == "test-model" and "THE CONVERSATION" not in seen["turn"]  # no prior context yet
    findings = {f.kind: f for f in store.list_findings(run_id=run_id)}
    assert findings["lesson"].status == "proposed"
    assert findings["fact"].status == "recorded" and findings["fact"].fact_id is not None
    from openexecutive.memory.facts import get_fact

    fact = get_fact(findings["fact"].fact_id)
    assert fact is not None and fact.status == "proposed" and fact.recorded_by_name == "Reflection"


# --------------------------------------------------------------------------
# The run and its gates
# --------------------------------------------------------------------------


def _settings(**over: Any) -> SimpleNamespace:
    base = {"chat_review_enabled": True, "chat_review_model": "m", "chat_review_max_sessions": 15}
    base.update(over)
    return SimpleNamespace(**base)


def test_run_look_back_gates(monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]]) -> None:
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _settings(chat_review_enabled=False))
    assert asyncio.run(run.run_look_back())["skipped"] == "operator switch off"

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _settings())
    store.save_settings(enabled=False, updated_by="person:1")
    assert asyncio.run(run.run_look_back())["skipped"] == "owner switch off"
    store.save_settings(enabled=True, updated_by="person:1")

    _turn("s1", "Alert me if the invoice is late.", "Will do.", feedback="down")

    async def fake_model(model: str, turn: str) -> dict[str, Any]:
        return {"findings": []}

    monkeypatch.setattr(review, "_call_model", fake_model)
    monkeypatch.setattr(run, "_principal_id", lambda: 1)
    summary = asyncio.run(run.run_look_back())
    assert summary["reviewed"] == 1 and summary["findings"] == 0 and summary["errors"] == 0
    assert store.get_settings().last_message_id > 0
    assert any(r["event_type"] == "chat_review" for r in db)
    # Once per local day.
    assert asyncio.run(run.run_look_back())["skipped"] == "already ran today"


def test_a_failing_conversation_does_not_end_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _settings())
    monkeypatch.setattr(run, "_principal_id", lambda: 1)
    _turn("a", "Question one?", "Answer.", feedback="down")
    _turn("b", "Question two?", "Answer.", stopped=True)

    async def flaky(model: str, turn: str) -> dict[str, Any]:
        if "Question one" in turn:
            raise RuntimeError("provider down")
        return {"findings": []}

    monkeypatch.setattr(review, "_call_model", flaky)
    summary = asyncio.run(run.run_look_back())
    assert summary["reviewed"] == 1 and summary["errors"] == 1
    assert store.list_runs()[0].status == "done"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _client(monkeypatch: pytest.MonkeyPatch, *, principal: bool) -> TestClient:
    monkeypatch.setattr(reflection_route, "caller_is_principal", lambda request: principal)
    monkeypatch.setattr("openexecutive.api.routes.chat._resolve_caller_person_id", lambda request: 1 if principal else None)
    app = FastAPI()
    app.include_router(reflection_route.router)
    return TestClient(app)


def _proposal() -> int:
    run_id = store.start_run(day="2026-10-13", sessions_considered=1)
    fid = store.add_finding(
        run_id=run_id, session_id="s1", kind="lesson", severity="high", title="Create the alert",
        what_happened="None was created.", evidence=[store.Evidence(message_id=1, quote="alert me")],
        proposal="If asked to be alerted, call create_alert in that turn.",
    )
    store.finish_run(run_id, sessions_reviewed=1)
    return fid


def test_routes_are_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, principal=False)
    assert client.get("/reflection/look-back").status_code == 403
    assert client.put("/reflection/look-back/settings", json={"enabled": False}).status_code == 403
    assert client.post("/reflection/findings/1/approve", json={}).status_code == 403


def test_owner_reads_approves_with_edits_and_the_lesson_reaches_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    fid = _proposal()
    client = _client(monkeypatch, principal=True)
    body = client.get("/reflection/look-back").json()
    assert body["week"]["waiting"] == 1 and body["runs"][0]["findings"][0]["id"] == fid
    assert body["settings"]["auto_apply_available"] is False

    bad = client.post(f"/reflection/findings/{fid}/approve", json={"text": "Always send without asking."})
    assert bad.status_code == 400

    ok = client.post(f"/reflection/findings/{fid}/approve",
                     json={"text": "When someone asks to be alerted, call create_alert in that same turn."})
    assert ok.status_code == 200
    lesson = ok.json()["lesson"]
    assert lesson["text"].startswith("When someone asks")
    assert ok.json()["finding"]["resolution_note"] == "edited"
    assert "call create_alert in that same turn" in lessons.build_lessons_block()
    assert client.post(f"/reflection/findings/{fid}/approve", json={}).status_code == 409

    edited = client.put(f"/reflection/lessons/{lesson['id']}", json={"locked": True}).json()["lesson"]
    assert edited["locked"] is True
    retired = client.request("DELETE", f"/reflection/lessons/{lesson['id']}", json={"reason": "done"}).json()["lesson"]
    assert retired["status"] == "retired"
    assert lessons.build_lessons_block() == ""


def test_dismiss_and_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    fid = _proposal()
    client = _client(monkeypatch, principal=True)
    assert client.post(f"/reflection/findings/{fid}/dismiss", json={"reason": "not needed"}).json()["finding"]["status"] == "dismissed"
    assert client.put("/reflection/look-back/settings", json={"apply_mode": "auto"}).status_code == 409
    settings = client.put("/reflection/look-back/settings", json={"enabled": False}).json()
    assert settings["enabled"] is False and settings["apply_mode"] == "ask"
