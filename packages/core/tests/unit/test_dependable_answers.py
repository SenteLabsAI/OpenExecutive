"""Dependable answers: keep what worked, and say what the answer looked at.

  * One specialist that raises, or returns nothing, no longer loses the whole
    reply — its tool_result says it is unavailable, the others' stand, and the
    missing area (never the specialist) is reported unless another call
    covered it.
  * A failed knowledge retrieval leaves a specialist without context rather
    than failing the turn.
  * The documents and web pages a turn looked at (the Executive's own search
    and every specialist that answered) are collected by the web chat route,
    sent once as a ``sources`` event after the reply, stopped or timed out
    too, saved on the assistant row, and returned when the chat is reloaded.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator import router
from openexecutive.orchestrator.answer_sources import (
    MAX_SOURCES,
    TurnSources,
    area_for,
    record_web_sources,
    safe_link,
    title_from_filename,
)
from openexecutive.orchestrator.executive import Executive
from openexecutive.orchestrator.schedule_tools import current_session
from openexecutive.orchestrator.session import Session

# ---------------------------------------------------------------------------
# The collector
# ---------------------------------------------------------------------------


def test_sources_are_deduplicated_and_capped() -> None:
    sources = TurnSources()
    sources.add("company", "Q3 plan.pdf")
    sources.add("company", "  Q3   plan.pdf ")  # same after whitespace clean-up
    sources.add("knowledge", "Q3 plan.pdf")  # same title, different kind: kept
    for i in range(MAX_SOURCES * 2):
        sources.add("company", f"doc {i}")
    payload = sources.payload()
    assert len(payload["sources"]) == MAX_SOURCES
    assert payload["sources"][:2] == [
        {"kind": "company", "title": "Q3 plan.pdf", "url": None},
        {"kind": "knowledge", "title": "Q3 plan.pdf", "url": None},
    ]


def test_web_sources_cannot_crowd_out_documents() -> None:
    sources = TurnSources()
    for i in range(20):
        sources.add("web", f"Page {i}", f"https://example.org/{i}")
    sources.add("company", "Board deck.pdf")
    kinds = [s["kind"] for s in sources.payload()["sources"]]
    assert kinds.count("web") == 6
    assert "company" in kinds


def test_pages_the_reply_cites_are_kept_when_the_lists_are_full() -> None:
    # An early round fills every list; the reply cites two pages later on.
    sources = TurnSources()
    for i in range(MAX_SOURCES + 3):
        sources.add("company", f"doc {i}")
    for i in range(10):
        sources.add("web", f"Result {i}", f"https://r.example/{i}")
    sources.add("web", "Result 7", "https://r.example/7", cited=True)
    sources.add("web", "Cited page", "https://cited.example/a", cited=True)

    listed = sources.payload()["sources"]
    assert len(listed) == MAX_SOURCES
    assert [s["title"] for s in listed if s["kind"] == "web"] == ["Result 7", "Cited page"]
    assert [s["title"] for s in listed if s["kind"] == "company"] == [f"doc {i}" for i in range(10)]


def test_search_results_fill_the_room_that_is_left() -> None:
    sources = TurnSources()
    sources.add("company", "Board deck.pdf")
    for i in range(10):
        sources.add("web", f"Result {i}", f"https://r.example/{i}")
    sources.add("web", "Result 2", "https://r.example/2", cited=True)  # listed once
    listed = sources.payload()["sources"]
    assert [s["title"] for s in listed] == [
        "Result 2", "Board deck.pdf", "Result 0", "Result 1", "Result 3", "Result 4", "Result 5",
    ]


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://reuters.com/a", "https://reuters.com/a"),
        ("http://example.org/x?y=1", "http://example.org/x?y=1"),
        ("/artifacts/alert%3A12", "/artifacts/alert%3A12"),
        ("javascript:alert(1)", None),
        ("data:text/html,hi", None),
        ("//evil.example/x", None),
        ("/\\evil.example", None),
        ("/artifacts/../x", None),
        ("/artifacts/", None),
        ("/artifacts/a/b", None),
        ("/settings", None),
        ("ftp://example.org/f", None),
        ("https://", None),
        ("", None),
        (None, None),
    ],
)
def test_only_http_links_and_in_app_documents_survive(url: str | None, kept: str | None) -> None:
    assert safe_link(url) == kept
    sources = TurnSources()
    sources.add("document", "A document", url)
    assert sources.payload()["sources"][0]["url"] == kept


def test_a_web_page_never_links_inside_the_app() -> None:
    sources = TurnSources()
    sources.add("web", "A page", "/artifacts/alert%3A12")
    assert sources.payload()["sources"] == [{"kind": "web", "title": "A page", "url": None}]


def test_long_and_empty_titles() -> None:
    sources = TurnSources()
    sources.add("company", "")
    sources.add("company", "   ")
    sources.add("company", "x" * 500)
    [only] = sources.payload()["sources"]
    assert len(only["title"]) == 120 and only["title"].endswith("…")


def test_unavailable_areas_never_name_a_specialist() -> None:
    sources = TurnSources()
    assert sources.is_empty()
    sources.mark_unavailable("cfo")
    sources.mark_unavailable("cfo")
    sources.mark_unavailable("gc")
    sources.mark_unavailable("made_up_agent")
    assert sources.payload()["unavailable"] == ["finance", "legal", "one area"]
    assert not sources.is_empty()
    assert sources.event("s-1") == {"type": "sources", "session_id": "s-1", **sources.payload()}
    for specialist in router.SPECIALIST_REGISTRY:
        area = area_for(specialist)
        assert specialist not in area and "specialist" not in area


def test_an_area_is_missing_only_if_nothing_answered_for_it() -> None:
    # e.g. one of two finance questions failed, or a retry went through.
    sources = TurnSources()
    sources.mark_unavailable("cfo")
    sources.mark_unavailable("gc")
    sources.mark_answered("cfo")
    assert sources.payload()["unavailable"] == ["legal"]


def test_threads_can_record_at_once_and_the_cap_holds() -> None:
    sources = TurnSources()

    def add_many(n: int) -> None:
        for i in range(50):
            sources.add("company", f"t{n}-{i}")

    threads = [threading.Thread(target=add_many, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sources.payload()["sources"]) == MAX_SOURCES


def test_title_from_filename() -> None:
    assert title_from_filename("board_composition_and_governance.md") == "Board composition and governance"
    assert title_from_filename("finance/unit-economics.MD") == "Unit economics"
    assert title_from_filename("") == ""


# ---------------------------------------------------------------------------
# Web pages from a model response
# ---------------------------------------------------------------------------


def _text(text: str, citations: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text, citations=citations)


def _search(results: Any) -> SimpleNamespace:
    return SimpleNamespace(type="web_search_tool_result", content=results)


def _hit(url: str, title: str | None = "A title") -> SimpleNamespace:
    return SimpleNamespace(url=url, title=title)


def test_cited_pages_come_first_then_a_few_results_per_search() -> None:
    sources = TurnSources()
    content = [
        _search([_hit(f"https://r.example/{i}", f"Result {i}") for i in range(10)]),
        _search(SimpleNamespace(error_code="max_uses_exceeded")),  # an error, not a list
        _text("Revenue grew.", [_hit("https://cited.example/a", "Cited page")]),
        _text("No citations here."),
    ]
    record_web_sources(sources, content)
    titles = [s["title"] for s in sources.payload()["sources"]]
    assert titles == ["Cited page", "Result 0", "Result 1", "Result 2"]


def test_a_page_without_a_title_is_named_by_its_site() -> None:
    sources = TurnSources()
    record_web_sources(sources, [_search([_hit("https://www.ft.com/x", None)])])
    assert sources.payload()["sources"][0]["title"] == "www.ft.com"


def test_odd_response_content_never_raises() -> None:
    sources = TurnSources()
    odd: list[Any] = [None, 42, _text("x", citations="not a list"), _search([None])]  # type: ignore[arg-type]
    record_web_sources(sources, odd)
    record_web_sources(sources, None)  # type: ignore[arg-type]
    assert sources.is_empty()


# ---------------------------------------------------------------------------
# Knowledge retrieval names what it returned
# ---------------------------------------------------------------------------


@pytest.fixture
def quiet_retrieval_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    import openexecutive.knowledge.retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "_audit_log", lambda *a, **k: None)


def test_retrieve_records_the_documents_it_returns(quiet_retrieval_audit: None) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.knowledge.store import ChromaDBStore

    def fake_query(*, query_text: str, collection: str, domain_filter: Any, n_results: int) -> list[dict[str, Any]]:
        if collection == ChromaDBStore.BUILTIN_COLLECTION:
            return [{"text": "Five forces…", "metadata": {"filename": "porters_five_forces.md", "domain": "strategy"}, "distance": 0.2}]
        if collection == ChromaDBStore.COMPANY_COLLECTION:
            return [{"text": "Our Q3 plan…", "metadata": {"filename": "Q3 plan.pdf", "domain": "strategy"}, "distance": 0.1}]
        return []

    store = MagicMock()
    store.query.side_effect = fake_query
    review_store = SimpleNamespace(
        get_withheld_keys=lambda _ct: set(),
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )
    sources = TurnSources()
    text = retrieve(
        query="How should we position against rivals?",
        store=store,
        review_store=review_store,  # type: ignore[arg-type]
        record_source=sources.add,
    )
    assert "Our Q3 plan" in text
    assert sources.payload()["sources"] == [
        {"kind": "company", "title": "Q3 plan.pdf", "url": None},
        {"kind": "knowledge", "title": "Porters five forces", "url": None},
    ]


def test_notion_research_and_earlier_documents_are_named() -> None:
    from openexecutive.knowledge.retriever import _record_sources

    sources = TurnSources()
    _record_sources(
        sources.add,
        company=[],
        notion=[{"metadata": {"filename": "Hiring plan"}}],
        research=[
            {"metadata": {"type": "artifact", "artifact_id": "alert:12", "title": "Churn memo"}},
            {"metadata": {"type": "recent_research", "created_at": "2026-09-20T10:00:00+00:00"}},
        ],
        builtin=[],
    )
    assert sources.payload()["sources"] == [
        {"kind": "notion", "title": "Hiring plan", "url": None},
        {"kind": "document", "title": "Churn memo", "url": "/artifacts/alert%3A12"},
        {"kind": "research", "title": "Research notes from 2026-09-20", "url": None},
    ]


def test_a_recording_failure_never_breaks_retrieval() -> None:
    from openexecutive.knowledge.retriever import _record_sources

    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("display bug")

    _record_sources(explode, [{"metadata": {"filename": "a.pdf"}}], [], [], [])  # does not raise


# ---------------------------------------------------------------------------
# The router keeps what worked
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_specialists(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """route_parallel with retrieval, failure cases and department memory
    stubbed; each specialist's behaviour comes from ``answers``."""
    answers: dict[str, Any] = {}
    seen_knowledge: dict[str, str] = {}

    async def route_to_specialist(*, specialist_name: str, retrieved_knowledge: str = "", **_k: Any) -> str:
        seen_knowledge[specialist_name] = retrieved_knowledge
        answer = answers[specialist_name]
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)

    def retrieve(*, query: str, specialist_name: str, record_source: Any = None) -> str:
        if record_source is not None:
            record_source("company", f"{specialist_name} notes.pdf")
        return f"knowledge for {specialist_name}"

    async def no_memory(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(router, "route_to_specialist", route_to_specialist)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", retrieve)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve_failures", lambda **_k: "")
    monkeypatch.setattr(router, "_prefetch_department_for_call", no_memory)
    return {"answers": answers, "seen_knowledge": seen_knowledge}


def _calls(*specialists: str) -> list[dict[str, str]]:
    return [{"specialist": s, "query": "q", "context": ""} for s in specialists]


def test_one_failing_specialist_leaves_the_others(fake_specialists: dict[str, Any]) -> None:
    fake_specialists["answers"].update({"cso": "Strategy view", "cfo": RuntimeError("overloaded")})
    failed: list[int] = []
    sources = TurnSources()
    results = asyncio.run(
        router.route_parallel(_calls("cso", "cfo"), record_source=sources.add, failed_calls_out=failed)
    )
    assert results[0] == "Strategy view"
    assert results[1].startswith("UNAVAILABLE: the cfo specialist could not answer")
    assert "RuntimeError" in results[1] and "overloaded" not in results[1]
    assert "In your reply, say in one short sentence" in results[1]
    assert failed == [1]
    # The failed specialist's documents never reached the answer.
    assert [s["title"] for s in sources.payload()["sources"]] == ["cso notes.pdf"]


def test_documents_are_recorded_in_call_order(
    fake_specialists: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_specialists["answers"].update({"cso": "Strategy view", "cmo": "Marketing view"})
    real = router.route_to_specialist

    async def cso_is_slow(**kwargs: Any) -> str:
        if kwargs["specialist_name"] == "cso":
            await asyncio.sleep(0.05)
        return await real(**kwargs)

    monkeypatch.setattr(router, "route_to_specialist", cso_is_slow)
    sources = TurnSources()
    asyncio.run(router.route_parallel(_calls("cso", "cmo"), record_source=sources.add))
    assert [s["title"] for s in sources.payload()["sources"]] == ["cso notes.pdf", "cmo notes.pdf"]


def test_on_the_web_chat_the_reply_need_not_mention_it(fake_specialists: dict[str, Any]) -> None:
    fake_specialists["answers"]["cfo"] = TimeoutError()
    [result] = asyncio.run(router.route_parallel(_calls("cfo"), tell_user_when_unavailable=False))
    assert "The app tells the user which part is missing" in result
    assert "In your reply" not in result


def test_an_empty_analysis_counts_as_unavailable(fake_specialists: dict[str, Any]) -> None:
    fake_specialists["answers"]["cfo"] = "   "
    failed: list[int] = []
    [result] = asyncio.run(router.route_parallel(_calls("cfo"), failed_calls_out=failed))
    assert "it returned no analysis" in result and failed == [0]


def test_cancellation_still_stops_the_batch(fake_specialists: dict[str, Any]) -> None:
    fake_specialists["answers"].update({"cfo": asyncio.CancelledError(), "cso": "ok"})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(router.route_parallel(_calls("cfo", "cso")))


def test_a_failed_retrieval_leaves_the_specialist_running(
    fake_specialists: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_retrieve(**_k: Any) -> str:
        raise RuntimeError("chroma down")

    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", broken_retrieve)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve_failures", broken_retrieve)
    fake_specialists["answers"]["cso"] = "Strategy view"
    results = asyncio.run(router.route_parallel(_calls("cso")))
    assert results == ["Strategy view"]
    assert fake_specialists["seen_knowledge"]["cso"] == ""


# ---------------------------------------------------------------------------
# The Executive's loop
# ---------------------------------------------------------------------------


class _ToolUse:
    type = "tool_use"

    def __init__(self, id_: str, specialist: str) -> None:
        self.id = id_
        self.name = "consult_specialist"
        self.input = {"specialist": specialist, "query": "what now?"}


class _Final:
    usage = None

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _Stream:
    def __init__(self, final: _Final) -> None:
        self._final = final

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def get_final_message(self) -> _Final:
        return self._final


class _Provider:
    def __init__(self, finals: list[_Final]) -> None:
        self._finals = list(finals)
        self.calls: list[dict[str, Any]] = []

    def messages_stream(self, **kwargs: Any) -> _Stream:
        self.calls.append(kwargs)
        return _Stream(self._finals.pop(0))


@pytest.fixture
def no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openexecutive.orchestrator.executive.audit_log", lambda *a, **k: None)


def _run_loop(provider: _Provider, sources: TurnSources, session: Session | None, **loop_kwargs: Any) -> None:
    async def go() -> None:
        token = current_session.set(session)  # type: ignore[arg-type]
        try:
            with patch("openexecutive.orchestrator.executive.get_provider", return_value=provider):
                async for _ in Executive()._stream_agent_loop(
                    system_blocks=[],
                    messages=[{"role": "user", "content": "what now?"}],
                    model="claude-test",
                    turn_sources=sources,
                    **loop_kwargs,
                ):
                    pass
        finally:
            current_session.reset(token)

    asyncio.run(go())


def _consult_then_answer(extra: list[Any] | None = None) -> _Provider:
    return _Provider(
        [
            _Final([_ToolUse("tu-cfo", "cfo"), _ToolUse("tu-cso", "cso")], "tool_use"),
            _Final([*(extra or []), _text("Here is the plan.")], "end_turn"),
        ]
    )


@pytest.mark.parametrize(("web_chat", "told_to_mention"), [(True, False), (False, True)])
def test_the_turn_survives_a_failed_specialist(
    fake_specialists: dict[str, Any], no_audit: None, web_chat: bool, told_to_mention: bool
) -> None:
    fake_specialists["answers"].update({"cfo": RuntimeError("529"), "cso": "Strategy view"})
    provider = _consult_then_answer()
    sources = TurnSources()
    _run_loop(provider, sources, Session(session_id="s", from_web_chat=web_chat))

    assert len(provider.calls) == 2  # the reply was written, not an error
    results = {b["tool_use_id"]: b["content"] for b in provider.calls[1]["messages"][-1]["content"]}
    assert results["tu-cso"] == "Strategy view"
    assert results["tu-cfo"].startswith("UNAVAILABLE")
    assert ("In your reply" in results["tu-cfo"]) is told_to_mention
    assert sources.payload()["unavailable"] == ["finance"]
    assert [s["title"] for s in sources.payload()["sources"]] == ["cso notes.pdf"]


def test_an_area_that_answers_on_a_later_round_is_not_missing(
    fake_specialists: dict[str, Any], no_audit: None
) -> None:
    outcomes = iter([RuntimeError("529"), "Finance view"])

    async def flaky_cfo(*, specialist_name: str, **_k: Any) -> str:
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    provider = _Provider(
        [
            _Final([_ToolUse("tu-1", "cfo")], "tool_use"),
            _Final([_ToolUse("tu-2", "cfo")], "tool_use"),
            _Final([_text("Here is the plan.")], "end_turn"),
        ]
    )
    sources = TurnSources()
    with patch.object(router, "route_to_specialist", flaky_cfo):
        _run_loop(provider, sources, Session(session_id="s", from_web_chat=True))
    assert len(provider.calls) == 3
    assert sources.payload()["unavailable"] == []
    assert [s["title"] for s in sources.payload()["sources"]] == ["cfo notes.pdf"]


def test_a_failed_specialist_is_not_counted_as_consulted(
    fake_specialists: dict[str, Any], no_audit: None
) -> None:
    # The committee picks its critics from the consulted list, and department
    # memory records the turn for each one, so a failure must not count.
    fake_specialists["answers"].update({"cfo": RuntimeError("529"), "cso": "Strategy view"})
    consulted: list[str] = []
    outputs: dict[str, str] = {}
    _run_loop(
        _consult_then_answer(),
        TurnSources(),
        Session(session_id="s", from_web_chat=True),
        consulted_out=consulted,
        specialist_outputs_out=outputs,
    )
    assert consulted == ["cso"]
    assert outputs == {"cso": "Strategy view"}


def test_web_pages_the_reply_used_are_recorded(fake_specialists: dict[str, Any], no_audit: None) -> None:
    from anthropic.types import (
        CitationsWebSearchResultLocation,
        TextBlock,
        WebSearchResultBlock,
        WebSearchToolResultBlock,
    )

    fake_specialists["answers"].update({"cfo": "Finance view", "cso": "Strategy view"})
    web = [
        WebSearchToolResultBlock(
            type="web_search_tool_result",
            tool_use_id="srvtoolu_1",
            content=[
                WebSearchResultBlock(
                    type="web_search_result",
                    url="https://news.example/1",
                    title="Market update",
                    encrypted_content="enc",
                )
            ],
        ),
        TextBlock(
            type="text",
            text="Rates are up.",
            citations=[
                CitationsWebSearchResultLocation(
                    type="web_search_result_location",
                    url="https://bank.example/rates",
                    title="Rate decision",
                    cited_text="Rates",
                    encrypted_index="ei",
                )
            ],
        ),
    ]
    provider = _consult_then_answer(extra=web)
    sources = TurnSources()
    _run_loop(provider, sources, None)
    web_titles = [s["title"] for s in sources.payload()["sources"] if s["kind"] == "web"]
    assert web_titles == ["Rate decision", "Market update"]
    assert sources.payload()["unavailable"] == []


# ---------------------------------------------------------------------------
# stream_chat and the committee path record into the route's collector
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A whole stream_chat turn also audits and schedules memory work; keep
    every write it makes out of the repo's ./episodic_memory.db."""
    from openexecutive.audit import AuditLogger, set_audit_logger

    monkeypatch.chdir(tmp_path)
    set_audit_logger(AuditLogger(tmp_path / "audit.db"))
    yield
    set_audit_logger(None)


def _drain(gen: AsyncIterator[Any]) -> list[Any]:
    async def go() -> list[Any]:
        return [item async for item in gen]

    return asyncio.run(go())


def test_stream_chat_records_into_the_callers_collector(isolated_turn: None) -> None:
    async def loop(*_a: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        kwargs["turn_sources"].add("company", "Q3 plan.pdf")
        yield "Answer."

    sources = TurnSources()
    session = Session(session_id="s-1", company_profile=CompanyProfile())
    with patch.object(Executive, "_stream_agent_loop", new=loop):
        items = _drain(Executive().stream_chat(user_message="Hi", session=session, turn_sources=sources))
    assert "Answer." in items
    # The web chat route sends the event, however the reply ends; never the Executive.
    assert not [i for i in items if isinstance(i, dict) and i.get("type") == "sources"]
    assert sources.payload()["sources"] == [{"kind": "company", "title": "Q3 plan.pdf", "url": None}]


def test_the_committee_draft_records_into_the_callers_collector(isolated_turn: None) -> None:
    async def loop(*_a: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        kwargs["turn_sources"].mark_unavailable("cfo")
        if False:
            yield ""

    sources = TurnSources()
    session = Session(session_id="s-3", company_profile=CompanyProfile())
    with patch.object(Executive, "_stream_agent_loop", new=loop):
        _drain(Executive().stream_chat_with_committee(user_message="Hi", session=session, turn_sources=sources))
    assert sources.payload()["unavailable"] == ["finance"]


# ---------------------------------------------------------------------------
# Saved with the reply, and back on reload
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db = (tmp_path / "episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(session_store, "DB_PATH", db)
    episodic.initialize_db(db)
    return db


def test_sources_are_saved_and_reloaded(temp_db: Path) -> None:
    payload = {"sources": [{"kind": "company", "title": "Q3 plan.pdf", "url": None}], "unavailable": ["finance"]}
    session_store.save_message("s-1", "user", "hi", db_path=temp_db)
    session_store.save_message("s-1", "assistant", "answer", db_path=temp_db, sources=json.dumps(payload))
    session_store.save_message("s-1", "assistant", "plain", db_path=temp_db)
    messages = session_store.load_messages("s-1", db_path=temp_db)
    assert "sources" not in messages[0]
    assert messages[1]["sources"] == payload
    assert "sources" not in messages[2]


def test_unreadable_saved_sources_are_ignored(temp_db: Path) -> None:
    session_store.save_message("s-1", "assistant", "answer", db_path=temp_db, sources="{not json")
    session_store.save_message("s-1", "assistant", "answer", db_path=temp_db, sources='["a list"]')
    assert all("sources" not in m for m in session_store.load_messages("s-1", db_path=temp_db))


def test_an_existing_database_gains_the_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
    episodic.initialize_db(db)
    episodic.initialize_db(db)  # idempotent
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)")}
    assert "sources" in columns


# ---------------------------------------------------------------------------
# The web chat route sends and saves them, however the reply ends
# ---------------------------------------------------------------------------

CLIENT_TURN = "sources-turn-0001"


@pytest.fixture
def chat_route_with(temp_db: Path, isolated_turn: None, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Runs one web chat turn. The route's own search finds "Q3 plan.pdf";
    ``reply`` stands in for the Executive's stream and gets its keyword
    arguments. Returns the SSE events and the saved assistant row's sources."""
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title

    chat_route._sessions.clear()
    chat_route._active_stops.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())

    def retrieve(*, record_source: Any = None, **_k: Any) -> str:
        if record_source is not None:
            record_source("company", "Q3 plan.pdf")
        return "[Q3 plan.pdf] Hiring freeze until Q4."

    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", retrieve)

    async def no_title(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", no_title)

    def run(reply: Any) -> tuple[list[dict[str, Any]], Any]:
        class FakeExecutive:
            _THINKING = exec_mod.Executive._THINKING

            def __init__(self, **_k: Any) -> None:
                pass

            def stream_chat(self, **kwargs: Any) -> AsyncIterator[Any]:
                return reply(**kwargs)  # type: ignore[no-any-return]

        monkeypatch.setattr(exec_mod, "Executive", FakeExecutive)
        app = FastAPI()
        app.include_router(chat_route.router)
        response = TestClient(app).post("/chat", json={"message": "hi", "client_turn_id": CLIENT_TURN})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        with sqlite3.connect(temp_db) as conn:
            rows = conn.execute("SELECT sources FROM chat_messages WHERE role = 'assistant'").fetchall()
        saved = json.loads(rows[0][0]) if rows and rows[0][0] else None
        return events, saved

    return run


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


def test_the_route_sends_and_saves_what_the_reply_looked_at(chat_route_with: Any) -> None:
    async def reply(*, turn_sources: TurnSources, **_k: Any) -> AsyncIterator[Any]:
        turn_sources.add("web", "Rate decision", "https://bank.example/rates", cited=True)
        turn_sources.mark_unavailable("cfo")
        yield "Here you go."

    events, saved = chat_route_with(reply)
    expected = {
        "sources": [
            {"kind": "web", "title": "Rate decision", "url": "https://bank.example/rates"},
            # The route's own search, which the Executive answered from.
            {"kind": "company", "title": "Q3 plan.pdf", "url": None},
        ],
        "unavailable": ["finance"],
    }
    [event] = [e for e in events if e["type"] == "sources"]
    assert {k: event[k] for k in expected} == expected
    types = _types(events)
    assert types.index("chunk") < types.index("sources") < types.index("done")
    assert saved == expected


def test_a_stopped_reply_keeps_its_sources(chat_route_with: Any) -> None:
    from openexecutive.api.routes import chat as chat_route

    async def reply(*, turn_sources: TurnSources, **_k: Any) -> AsyncIterator[Any]:
        turn_sources.mark_unavailable("gc")
        yield "Partial answer"
        chat_route._request_stop(CLIENT_TURN, "local")
        await asyncio.sleep(30)
        yield "never sent"

    events, saved = chat_route_with(reply)
    types = _types(events)
    assert types.index("sources") < types.index("stopped") < types.index("done")
    assert saved == {"sources": [{"kind": "company", "title": "Q3 plan.pdf", "url": None}], "unavailable": ["legal"]}


def test_a_timed_out_reply_keeps_its_sources(chat_route_with: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.config import Settings

    original_init = Settings.__init__

    def short_timeout(self: Settings, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        object.__setattr__(self, "chat_stream_timeout_s", 0.3)

    monkeypatch.setattr(Settings, "__init__", short_timeout)

    async def reply(*, turn_sources: TurnSources, **_k: Any) -> AsyncIterator[Any]:
        turn_sources.mark_unavailable("cfo")
        yield "Partial answer"
        await asyncio.sleep(30)
        yield "never sent"

    events, saved = chat_route_with(reply)
    types = _types(events)
    assert types.index("sources") < types.index("error") < types.index("done")
    assert saved == {"sources": [{"kind": "company", "title": "Q3 plan.pdf", "url": None}], "unavailable": ["finance"]}


def test_a_reply_that_looked_at_nothing_sends_nothing(
    chat_route_with: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_k: "")

    async def reply(**_k: Any) -> AsyncIterator[Any]:
        yield "Hello."

    events, saved = chat_route_with(reply)
    assert "sources" not in _types(events) and saved is None
