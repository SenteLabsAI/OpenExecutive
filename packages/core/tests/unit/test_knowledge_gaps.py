"""Questions it could not answer (memory/knowledge_gaps.py, the grading in
memory/chat_review.py, api/routes/knowledge_questions.py): which questions
land on the list, that one question keeps one line, how a line leaves, and
who sees it."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive import providers
from openexecutive.api.routes import knowledge_questions as route
from openexecutive.audit import logger as audit_logger
from openexecutive.audit import usage
from openexecutive.audit.logger import AuditLogger
from openexecutive.memory import chat_review as cr
from openexecutive.memory import episodic, facts, session_store
from openexecutive.memory import knowledge_gaps as kg
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}
REFUND = "What is our refund policy for annual plans?"


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    episodic.initialize_db(session_store.DB_PATH)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAT_REVIEW_ENABLED", "true")
    monkeypatch.setattr(audit_logger, "_default_logger", AuditLogger(db_path=path))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(usage, "log_model_usage", lambda *a, **k: None)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return SimpleNamespace(principal=people_store.get_person(principal), teammate=people_store.get_person(teammate))


# --------------------------------------------------------------------------- #
# The list itself
# --------------------------------------------------------------------------- #


def test_the_same_question_asked_again_adds_to_one_line(db: Path) -> None:
    first = kg.note_unanswered(REFUND, person_id=1, session_id="s-1", asked_at=NOW, db_path=db)
    again = kg.note_unanswered("what's the refund policy on our annual plans", person_id=1,
                               session_id="s-2", asked_at=NOW + timedelta(hours=1), db_path=db)
    other = kg.note_unanswered("Who signs off on vendor contracts over $10,000?", person_id=1,
                               session_id="s-1", db_path=db)
    # Lines are per person: someone else's asking never joins this one.
    theirs = kg.note_unanswered(REFUND, person_id=2, session_id="s-3", db_path=db)
    assert first == again and other != first and theirs not in (first, other)
    lines = kg.list_open(db_path=db)
    refund = next(x for x in lines if x["id"] == first)
    assert [(a["person_id"], a["session_id"]) for a in refund["asks"]] == [(1, "s-1"), (1, "s-2")]
    assert refund["asks"][0]["question"] == REFUND


def test_matching_is_by_content_words() -> None:
    assert kg.same_question("Who approves vendor contracts?", "who approves our vendor contracts")
    assert not kg.same_question("Who approves vendor contracts?", "Who approves hiring plans?")
    assert not kg.same_question("refund?", "refund?")


def test_a_question_must_come_from_their_own_words() -> None:
    message = "hey, quick one: what is our refund policy for annual plans? customer asking"
    assert kg.clean_question(REFUND, message) == REFUND
    assert kg.clean_question("What is the board meeting date?", message) == ""
    assert kg.clean_question("Refund?", message) == ""


def test_a_line_leaves_and_a_dismissed_one_stays_gone_for_who_asked(db: Path) -> None:
    line = kg.note_unanswered(REFUND, person_id=1, session_id="s-1", db_path=db)
    assert line is not None
    assert kg.close(line, "dismissed", db_path=db)
    assert kg.list_open(db_path=db) == []
    assert kg.note_unanswered(REFUND, person_id=1, session_id="s-3", db_path=db) is None
    assert kg.list_open(db_path=db) == []
    # Someone else asking it is their own line.
    theirs = kg.note_unanswered(REFUND, person_id=2, session_id="s-4", db_path=db)
    assert theirs not in (None, line)
    assert kg.close(theirs, "taught", db_path=db)
    assert not kg.close(theirs, "document", db_path=db)


def test_a_later_answer_that_knew_it_takes_it_off(db: Path) -> None:
    kg.note_unanswered(REFUND, person_id=1, session_id="s-1", db_path=db)
    kg.note_unanswered(REFUND, person_id=2, session_id="s-2", db_path=db)
    # Only the asker's own line: someone else's turn never closes another's.
    assert kg.note_answered("refund policy for annual plans?", person_id=1, db_path=db) == 1
    [left] = kg.list_open(db_path=db)
    assert [a["person_id"] for a in left["asks"]] == [2]


def test_deleting_the_chat_drops_its_asks(db: Path) -> None:
    kg.note_unanswered(REFUND, person_id=1, session_id="s-1", db_path=db)
    kg.note_unanswered(REFUND, person_id=1, session_id="s-2", db_path=db)
    kg.note_unanswered("Who signs off on vendor contracts?", person_id=1, session_id="s-2", db_path=db)
    cr.forget_session("s-2", db_path=db)
    [left] = kg.list_open(db_path=db)
    assert [(a["question"], a["session_id"]) for a in left["asks"]] == [(REFUND, "s-1")]


def test_deleting_the_chat_drops_its_asks_even_when_its_turns_cannot_be(
    db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    kg.note_unanswered(REFUND, person_id=1, session_id="s-1", db_path=db)

    def locked(*_: Any, **__: Any) -> Any:
        raise cr.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cr, "_connect", locked)
    cr.forget_session("s-1", db_path=db)
    assert kg.list_open(db_path=db) == []


def test_company_sources_are_told_from_playbooks_and_the_web() -> None:
    assert not kg.has_company_source(None)
    assert not kg.has_company_source({"sources": [{"kind": "knowledge"}, {"kind": "web"}]})
    assert kg.has_company_source({"sources": [{"kind": "web"}, {"kind": "company"}]})
    assert kg.has_company_source({"sources": [{"kind": "drive"}]})
    assert not kg.has_company_source({"sources": [{"kind": "research"}, {"kind": "document"}]})


def test_a_dismissed_question_stays_dismissed_past_the_retention(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = kg.note_unanswered(REFUND, person_id=1, session_id="s-1", asked_at=NOW, db_path=db)
    assert line is not None and kg.close(line, "dismissed", db_path=db)
    monkeypatch.setattr(kg, "_now", lambda: NOW + timedelta(days=kg.KEEP_DAYS + 5))
    # Another person's asking runs the prune; the dismissed line survives it.
    kg.note_unanswered("Who signs off on vendor contracts?", person_id=2, session_id="s-2", db_path=db)
    assert kg.note_unanswered(REFUND, person_id=1, session_id="s-3", db_path=db) is None


# --------------------------------------------------------------------------- #
# Found by the look-back
# --------------------------------------------------------------------------- #


class _Provider:
    def __init__(self, grades: list[dict[str, Any]]) -> None:
        self.grades = grades

    async def messages_create(self, **kw: Any) -> Any:
        if (kw.get("tool_choice") or {}).get("name") == cr.GRADE_TOOL["name"]:
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use", id="g0", name=cr.GRADE_TOOL["name"], input={"grades": self.grades},
            )])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="nothing")])


def _web(person_id: int, session_id: str = "s-1", **kw: Any) -> Session:
    return Session(session_id=session_id, from_web_chat=True, caller_person_id=person_id,
                   web_caller_signed_in=True, **kw)


def _review(db: Path, grades: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, session_id: str = "s-1") -> None:
    monkeypatch.setattr(providers, "get_provider", lambda _model: _Provider(grades))
    asyncio.run(cr.run_scheduled(session_id, NOW + timedelta(hours=1), db_path=db))


def _saved_reply(db: Path, reply: str, sources: dict[str, Any] | None, session_id: str = "s-1") -> None:
    session_store.create_session(session_id, "Chat", NOW.isoformat(), db_path=db)
    session_store.save_message(session_id, "assistant", reply, db_path=db,
                               sources=json.dumps(sources) if sources is not None else None)


def test_a_company_question_no_document_covered_lands_on_the_list(
    db: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = roster.principal.id
    reply = "I don't have your refund policy on file. Most SaaS companies refund within 30 days."
    cr.record_turn(REFUND, reply, session=_web(pid), person_id=pid, calls=[], now=NOW)
    _saved_reply(db, reply, {"sources": [{"kind": "web", "title": "Refunds", "url": "https://x.example"}],
                             "unavailable": []})
    cr.record_turn("thanks, and say hi to the team", "Will do.", session=_web(pid), person_id=pid,
                   calls=[], now=NOW + timedelta(minutes=1))
    _review(db, [
        {"turn": 1, "verdict": "fine", "question": REFUND, "answer": "not_known"},
        {"turn": 2, "verdict": "fine"},
    ], monkeypatch)
    [line] = kg.list_open(db_path=db)
    [ask] = line["asks"]
    assert ask["question"] == REFUND and ask["person_id"] == pid and ask["place"] == "chat"


def test_a_question_a_document_came_back_for_is_not_listed(
    db: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = roster.principal.id
    reply = "I'm not sure, the policy doc doesn't say."
    cr.record_turn(REFUND, reply, session=_web(pid), person_id=pid, calls=[], now=NOW)
    _saved_reply(db, reply, {"sources": [{"kind": "company", "title": "Pricing", "url": None}], "unavailable": []})
    _review(db, [{"turn": 1, "verdict": "fine", "question": REFUND, "answer": "not_known"}], monkeypatch)
    assert kg.list_open(db_path=db) == []


def test_a_mail_private_chat_is_never_listed(
    db: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = roster.principal.id
    cr.record_turn(REFUND, "I don't know.", session=_web(pid), person_id=pid, calls=[], mail=True, now=NOW)
    _review(db, [{"turn": 1, "verdict": "fine", "question": REFUND, "answer": "not_known"}], monkeypatch)
    assert kg.list_open(db_path=db) == []


def test_a_later_answer_from_a_document_closes_the_line(
    db: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = roster.principal.id
    kg.note_unanswered(REFUND, person_id=pid, session_id="s-0", db_path=db)
    reply = "Annual plans are refundable within 30 days."
    cr.record_turn(REFUND, reply, session=_web(pid), person_id=pid, calls=[], now=NOW)
    _saved_reply(db, reply, {"sources": [{"kind": "company", "title": "Pricing", "url": None}], "unavailable": []})
    _review(db, [{"turn": 1, "verdict": "fine", "question": REFUND, "answer": "covered"}], monkeypatch)
    assert kg.list_open(db_path=db) == []


def test_a_covered_answer_with_no_document_leaves_the_line(
    db: Path, roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply that only echoed the asker isn't an answer the list trusts."""
    tid = roster.teammate.id
    kg.note_unanswered(REFUND, person_id=roster.principal.id, session_id="s-0", db_path=db)
    cr.record_turn(REFUND, "Yes, annual plans are never refunded.", session=_web(tid),
                   person_id=tid, calls=[], now=NOW)
    _review(db, [{"turn": 1, "verdict": "fine", "question": REFUND, "answer": "covered"}], monkeypatch)
    assert len(kg.list_open(db_path=db)) == 1


def test_the_grader_is_asked_for_the_question_and_document_cause() -> None:
    item = cr.GRADE_TOOL["input_schema"]["properties"]["grades"]["items"]["properties"]
    assert item["answer"]["enum"] == ["covered", "not_known"]
    assert "document" in item["cause"]["enum"] and "document" in cr.CAUSES["company"]


# --------------------------------------------------------------------------- #
# The Knowledge page's routes
# --------------------------------------------------------------------------- #


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_the_owner_sees_the_list_and_a_teammate_nothing(client: TestClient, roster: SimpleNamespace) -> None:
    kg.note_unanswered(REFUND, person_id=roster.principal.id, session_id="s-1")
    session_store.create_session("slack:channel:C7:ts1", "Pricing", NOW.isoformat(), roster.teammate.id)
    kg.note_unanswered("Who signs off on vendor contracts?", person_id=roster.teammate.id,
                       session_id="slack:channel:C7:ts1", place="Slack")
    page = client.get("/knowledge/questions", headers=OWNER).json()
    assert page["count"] == 2
    assert {(q["asked_by"], q["place"]) for q in page["questions"]} == {("You", "chat"), ("Ben Teammate", "Slack")}
    assert client.get("/knowledge/questions", headers=TEAMMATE).json() == {"questions": [], "count": 0}
    assert client.get("/knowledge/questions").json()["count"] == 0


def test_a_teammates_own_chat_stays_off_the_owners_list(client: TestClient, roster: SimpleNamespace) -> None:
    session_store.create_session("web-ben", "Pay", NOW.isoformat(), roster.teammate.id)
    line = kg.note_unanswered("What is my salary band?", person_id=roster.teammate.id, session_id="web-ben")
    assert client.get("/knowledge/questions", headers=OWNER).json()["count"] == 0
    assert client.post(f"/knowledge/questions/{line}/dismiss", headers=OWNER).status_code == 404


def test_a_shared_line_shows_only_the_asks_the_owner_may_read(client: TestClient, roster: SimpleNamespace) -> None:
    """A teammate's own chat matched to the owner's question adds nothing the
    owner can see: not its wording, who asked, where, when, or the count."""
    session_store.create_session("web-ben", "Pay", NOW.isoformat(), roster.teammate.id)
    kg.note_unanswered("What is the severance policy if I resign before the March layoffs?",
                       person_id=roster.teammate.id, session_id="web-ben", asked_at=NOW)
    assert client.get("/knowledge/questions", headers=OWNER).json()["count"] == 0
    kg.note_unanswered("What is our severance policy on layoffs?", person_id=roster.principal.id,
                       session_id="s-1", asked_at=NOW - timedelta(hours=1))
    kg.note_unanswered("What is our severance policy on layoffs?", person_id=roster.teammate.id,
                       session_id="web-ben", asked_at=NOW + timedelta(hours=1), place="Slack")
    [shown] = client.get("/knowledge/questions", headers=OWNER).json()["questions"]
    assert shown["question"] == "What is our severance policy on layoffs?"
    assert (shown["asked_by"], shown["place"], shown["times"]) == ("You", "chat", 1)
    assert shown["last_asked_at"] == (NOW - timedelta(hours=1)).isoformat()


def test_rows_join_only_on_wording_the_owner_may_read(client: TestClient, roster: SimpleNamespace) -> None:
    """The owner's probe never lands on a teammate's hidden line, and two
    readable lines asking the same thing show (and close) as one row."""
    session_store.create_session("web-ben", "Pay", NOW.isoformat(), roster.teammate.id)
    kg.note_unanswered("What is the severance policy if I resign before March planning?",
                       person_id=roster.teammate.id, session_id="web-ben", asked_at=NOW)
    mine = kg.note_unanswered("severance policy layoffs?", person_id=roster.principal.id,
                              session_id="s-1", asked_at=NOW + timedelta(minutes=5))
    [row] = client.get("/knowledge/questions", headers=OWNER).json()["questions"]
    assert (row["id"], row["times"], row["question"]) == (mine, 1, "severance policy layoffs?")
    session_store.create_session("slack:channel:C7:ts1", "Ops", NOW.isoformat(), roster.teammate.id)
    kg.note_unanswered("Who signs off on vendor contracts?", person_id=roster.principal.id,
                       session_id="s-1", asked_at=NOW)
    joined = kg.note_unanswered("who signs off on our vendor contracts", person_id=roster.teammate.id,
                                session_id="slack:channel:C7:ts1", asked_at=NOW + timedelta(hours=1),
                                place="Slack")
    rows = client.get("/knowledge/questions", headers=OWNER).json()["questions"]
    vendor = next(r for r in rows if "vendor" in r["question"])
    assert (vendor["times"], vendor["asked_by"], vendor["place"]) == (2, "Ben Teammate", "Slack")
    # Any line of the row finds it, so a row id that changed under the page still works.
    assert joined is not None and joined != vendor["id"]
    assert client.post(f"/knowledge/questions/{joined}/dismiss", headers=OWNER).json()["count"] == 1


def test_an_answer_too_long_for_a_fact_is_refused(client: TestClient, roster: SimpleNamespace) -> None:
    line = kg.note_unanswered(REFUND, person_id=roster.principal.id, session_id="s-1")
    resp = client.post(f"/knowledge/questions/{line}/answer", headers=OWNER, json={"answer": "x " * 300})
    assert resp.status_code == 422
    assert facts.list_facts() == [] and client.get("/knowledge/questions", headers=OWNER).json()["count"] == 1


def test_telling_the_answer_keeps_a_fact_and_closes_the_line(client: TestClient, roster: SimpleNamespace) -> None:
    line = kg.note_unanswered(REFUND, person_id=roster.principal.id, session_id="s-1")
    resp = client.post(f"/knowledge/questions/{line}/answer", headers=OWNER,
                       json={"answer": "Annual plans are refundable within 30 days."})
    assert resp.status_code == 200 and resp.json()["count"] == 0
    [fact] = facts.list_facts()
    assert fact.subject == REFUND and fact.statement == "Annual plans are refundable within 30 days."
    assert client.post(f"/knowledge/questions/{line}/answer", headers=OWNER,
                       json={"answer": "again"}).status_code == 404


def test_a_document_or_dismiss_closes_it_and_only_the_owner_may(client: TestClient, roster: SimpleNamespace) -> None:
    line = kg.note_unanswered(REFUND, person_id=roster.principal.id, session_id="s-1")
    other = kg.note_unanswered("Who signs off on vendor contracts?", person_id=roster.principal.id, session_id="s-1")
    assert client.post(f"/knowledge/questions/{line}/document", headers=TEAMMATE).status_code == 403
    assert client.post(f"/knowledge/questions/{line}/document", headers=OWNER).json()["count"] == 1
    assert client.post(f"/knowledge/questions/{other}/dismiss", headers=OWNER).json()["count"] == 0
    assert kg.get(line)["status"] == "resolved" and kg.get(other)["status"] == "dismissed"


def test_a_question_is_cut_to_what_a_fact_subject_holds(db: Path) -> None:
    long = "What is our refund policy for annual plans " + "and monthly plans " * 10 + "?"
    line = kg.note_unanswered(long, person_id=1, session_id="s-1", db_path=db)
    assert line is not None and len(kg.get(line, db_path=db)["asks"][0]["question"]) == facts.SUBJECT_MAX
