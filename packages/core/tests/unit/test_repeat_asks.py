"""The offer to do a repeated ask on a schedule (workflows.repeat_asks)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from openexecutive.workflows import repeat_asks

NY = ZoneInfo("America/New_York")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "asks.db"
    people = {7: SimpleNamespace(id=7), 8: SimpleNamespace(id=8)}
    monkeypatch.setattr(repeat_asks, "_verified_person", lambda session, pid: people.get(pid))
    return path


def _monday(week: int, hour: int = 8, minute: int = 40) -> datetime:
    # Mondays from 2026-10-19, 08:40 New York time.
    base = datetime(2026, 10, 19, hour, minute, tzinfo=NY)
    return (base + timedelta(weeks=week)).astimezone(UTC)


def _ask(db, text, at, person_id=7, calls=None):
    return repeat_asks.note_ask(
        text, session=object(), person_id=person_id, calls=calls, now=at, zone=NY, db_path=db,
    )


def test_key_words_drop_filler_and_fold_plurals():
    assert repeat_asks.key_words("Can you send me the pipeline summaries for this week?") == {
        "pipeline", "summary",
    }


def test_third_monday_offers_weekly_at_the_hour_they_asked(db):
    assert _ask(db, "Send me the pipeline summary", _monday(0)) is None
    assert _ask(db, "can you pull the pipeline summary please", _monday(1)) is None
    offer = _ask(db, "Pipeline summary for this week?", _monday(2))
    assert offer is not None
    # 08:00 New York (EDT on 10-19, EST after 11-01) is 13:00 UTC in November.
    assert offer["cadence"] == "weekly@mon@13:00"
    assert offer["ask"] == "Pipeline summary for this week?"
    assert len(offer["asked_at"]) == 3
    assert [o["id"] for o in repeat_asks.open_offers(7, db_path=db)] == [offer["id"]]


def test_same_day_asks_count_once(db):
    at = _monday(0)
    for minutes in (0, 5, 10, 15):
        assert _ask(db, "pipeline summary", at + timedelta(minutes=minutes)) is None


def test_asks_inside_one_week_are_daily(db):
    start = datetime(2026, 11, 2, 9, 15, tzinfo=NY).astimezone(UTC)
    _ask(db, "cash position update", start)
    _ask(db, "cash position update", start + timedelta(days=1))
    offer = _ask(db, "what's the cash position update", start + timedelta(days=2, hours=2))
    assert offer is not None and offer["cadence"] == "daily@14:00"


def test_different_asks_and_other_people_do_not_count(db):
    _ask(db, "pipeline summary", _monday(0))
    _ask(db, "pipeline summary", _monday(1), person_id=8)
    assert _ask(db, "hiring plan for engineering", _monday(2)) is None
    assert _ask(db, "pipeline summary", _monday(2)) is None
    assert repeat_asks.open_offers(8, db_path=db) == []


def test_unverified_speaker_is_not_kept(db):
    for week in range(3):
        assert _ask(db, "pipeline summary", _monday(week), person_id=99) is None


def test_a_turn_that_scheduled_something_makes_no_offer(db):
    _ask(db, "pipeline summary", _monday(0))
    _ask(db, "pipeline summary", _monday(1))
    calls = [("schedule_followup", "ok")]
    assert _ask(db, "pipeline summary", _monday(2), calls=calls) is None


def test_open_offer_is_not_made_twice_and_not_now_holds_it(db):
    for week in range(3):
        offer = _ask(db, "pipeline summary", _monday(week))
    assert offer is not None
    assert _ask(db, "pipeline summary", _monday(3)) is None
    assert repeat_asks.answer_offer(offer["id"], 8, accept=False, db_path=db) is False
    assert repeat_asks.answer_offer(offer["id"], 7, accept=False, now=_monday(3), db_path=db)
    assert repeat_asks.open_offers(7, db_path=db) == []
    assert _ask(db, "pipeline summary", _monday(4)) is None
    # After the hold the same ask can be offered again.
    for week in range(12, 15):
        later = _ask(db, "pipeline summary", _monday(week))
    assert later is not None


def test_set_it_up_retires_the_offer_for_good(db):
    for week in range(3):
        offer = _ask(db, "pipeline summary", _monday(week))
    assert repeat_asks.answer_offer(offer["id"], 7, accept=True, db_path=db)
    assert repeat_asks.answer_offer(offer["id"], 7, accept=True, db_path=db) is False
    for week in range(12, 15):
        assert _ask(db, "pipeline summary", _monday(week)) is None


def test_routes_show_and_answer_only_the_callers_own(db, monkeypatch):
    from openexecutive.api import caller as api_caller
    from openexecutive.api.main import create_app

    for week in range(3):
        offer = _ask(db, "pipeline summary", _monday(week))
    monkeypatch.setattr(repeat_asks, "_connect", lambda path, real=repeat_asks._connect: real(db))
    who = {"id": 7}
    monkeypatch.setattr(api_caller, "person_id_for", lambda caller: who["id"])
    client = TestClient(create_app())

    assert [o["id"] for o in client.get("/workflow-offers").json()["offers"]] == [offer["id"]]
    who["id"] = 8
    assert client.get("/workflow-offers").json()["offers"] == []
    assert client.post(f"/workflow-offers/{offer['id']}/dismiss").status_code == 404
    who["id"] = None
    assert client.get("/workflow-offers").json()["offers"] == []
    who["id"] = 7
    assert client.post(f"/workflow-offers/{offer['id']}/accept").status_code == 200
    assert client.get("/workflow-offers").json()["offers"] == []


@pytest.mark.parametrize("entry", ["stream_chat", "stream_chat_with_committee"])
def test_the_reply_ends_with_the_offer(entry, tmp_path, monkeypatch):
    """The chat stream hands the offer to the web chat after the reply."""
    import asyncio
    from unittest.mock import patch

    from openexecutive.audit import AuditLogger, set_audit_logger
    from openexecutive.memory import episodic
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session
    from tests.unit.test_executive_memory_text import _provider

    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "turn.db")
    set_audit_logger(AuditLogger(db_path=tmp_path / "turn.db"))
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **kw: None)
    offer = {"id": 3, "ask": "pipeline summary", "cadence": "weekly@mon@13:00", "asked_at": []}
    seen: list[str] = []

    def _note(text, **_kw):
        seen.append(text)
        return offer

    items: list = []

    async def _run() -> None:
        session = Session(session_id=f"t-repeat-{entry}")
        async for item in getattr(Executive(), entry)(
            "pipeline summary please", session, person_id=42, peer_memory_context=""
        ):
            items.append(item)

    with (
        patch("openexecutive.orchestrator.executive.get_provider", return_value=_provider()),
        patch("openexecutive.workflows.repeat_asks.note_ask", new=_note),
        patch("openexecutive.memory.honcho_client.sync_turn", new=lambda *a, **k: None),
        patch(
            "openexecutive.orchestrator.executive._sync_consulted_departments_to_honcho",
            new=lambda *a, **k: None,
        ),
        patch("openexecutive.memory.episodic.schedule_extraction", new=lambda *a, **k: None),
        patch("openexecutive.attunement.open_loops.schedule_open_loop_pass", new=lambda *a, **k: None),
    ):
        asyncio.run(_run())

    assert seen == ["pipeline summary please"]
    events = [i for i in items if isinstance(i, dict) and i.get("type") == "repeat_offer"]
    assert events == [{"type": "repeat_offer", "offer": offer, "session_id": f"t-repeat-{entry}"}]
