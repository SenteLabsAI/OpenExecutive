"""Tests for the short-message bypass and distance-threshold gating in retrieve()."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openexecutive.knowledge.retriever import (
    _DISTANCE_THRESHOLD,
    retrieve,
)


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `_emit_retrieval_audit` out of the default ./episodic_memory.db.

    retrieve() and retrieve_failures() both emit an audit row. Unpatched they
    write to the module default, and the leaked rows only break *other*
    modules' assertions in a full-suite run.
    """
    import openexecutive.knowledge.retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "_audit_log", lambda *a, **k: None)


@pytest.fixture
def fake_review_store() -> SimpleNamespace:
    """ReviewStore stub that returns empty sets — keep retrieval focused on the gate."""
    return SimpleNamespace(
        get_withheld_keys=lambda _ct: set(),
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )


def _make_store(builtin_hits: list[dict[str, Any]], company_hits: list[dict[str, Any]]) -> MagicMock:
    """ChromaDBStore stub. query() dispatches on collection name."""
    store = MagicMock()
    from openexecutive.knowledge.store import ChromaDBStore

    def fake_query(*, query_text: str, collection: str, domain_filter: Any, n_results: int) -> list[dict[str, Any]]:
        if collection == ChromaDBStore.BUILTIN_COLLECTION:
            return builtin_hits
        if collection == ChromaDBStore.COMPANY_COLLECTION:
            return company_hits
        return []

    store.query.side_effect = fake_query
    return store


@pytest.mark.parametrize("greeting", ["Hi", "ok", "  Hi  ", "?", ""])
def test_short_message_bypasses_retrieval(
    fake_review_store: SimpleNamespace, greeting: str
) -> None:
    """Very short greetings must not trigger a vector-store query."""
    store = _make_store(builtin_hits=[{"text": "x", "metadata": {}, "distance": 0.1}], company_hits=[])

    out = retrieve(query=greeting, store=store, review_store=fake_review_store)

    assert out == ""
    store.query.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "What now?",  # English follow-up — 9 chars
        "How come?",  # English follow-up — 9 chars
        "我们的营销策略",  # Chinese — 7 chars, would be 1 token under \w+
        "How should I price the assessment?",  # full question
        "ROI",  # 3-letter business acronym — must fire
        "CFO",
        "P&L",
    ],
)
def test_meaningful_query_fires_retrieval(
    fake_review_store: SimpleNamespace, query: str
) -> None:
    """Real questions — including non-whitespace-tokenized scripts and 3-letter
    business acronyms (ROI, CFO, P&L) — must fire RAG."""
    store = _make_store(
        builtin_hits=[{"text": "useful", "metadata": {"filename": "f.md"}, "distance": 0.2}],
        company_hits=[],
    )
    out = retrieve(query=query, store=store, review_store=fake_review_store)
    assert "useful" in out
    store.query.assert_called()


def test_distance_threshold_drops_weak_builtin_hits(fake_review_store: SimpleNamespace) -> None:
    """Only hits with distance <= _DISTANCE_THRESHOLD survive in builtin."""
    threshold = _DISTANCE_THRESHOLD
    store = _make_store(
        builtin_hits=[
            {"text": "strong match", "metadata": {"filename": "good.md"}, "distance": threshold - 0.1},
            {"text": "weak match", "metadata": {"filename": "bad.md"}, "distance": threshold + 0.1},
        ],
        company_hits=[],
    )
    out = retrieve(query="a real question about strategy", store=store, review_store=fake_review_store)
    assert "strong match" in out
    assert "weak match" not in out


def test_distance_threshold_drops_weak_company_hits(fake_review_store: SimpleNamespace) -> None:
    """Same threshold applies to the company-docs collection."""
    threshold = _DISTANCE_THRESHOLD
    store = _make_store(
        builtin_hits=[],
        company_hits=[
            {"text": "strong company doc", "metadata": {"filename": "good.md"}, "distance": threshold - 0.1},
            {"text": "weak company doc", "metadata": {"filename": "bad.md"}, "distance": threshold + 0.1},
        ],
    )
    out = retrieve(query="a real question about strategy", store=store, review_store=fake_review_store)
    assert "strong company doc" in out
    assert "weak company doc" not in out


def test_all_weak_hits_yields_empty_string(fake_review_store: SimpleNamespace) -> None:
    """When everything is below threshold, return empty rather than poison the prompt."""
    threshold = _DISTANCE_THRESHOLD
    store = _make_store(
        builtin_hits=[{"text": "noise", "metadata": {"filename": "n.md"}, "distance": threshold + 0.2}],
        company_hits=[{"text": "more noise", "metadata": {"filename": "m.md"}, "distance": threshold + 0.2}],
    )
    out = retrieve(query="a real question about strategy", store=store, review_store=fake_review_store)
    assert out == ""


def test_missing_or_none_distance_is_treated_as_far(
    fake_review_store: SimpleNamespace,
) -> None:
    """`distance` of None or missing must NOT crash the filter and must be
    treated as out-of-bounds so we never silently surface chunks of unknown
    relevance."""
    store = _make_store(
        builtin_hits=[
            {"text": "no distance", "metadata": {"filename": "x.md"}, "distance": None},
            {"text": "absent key", "metadata": {"filename": "y.md"}},
        ],
        company_hits=[],
    )
    out = retrieve(query="a real question about strategy", store=store, review_store=fake_review_store)
    assert out == ""


def test_perfect_match_distance_zero_is_kept(
    fake_review_store: SimpleNamespace,
) -> None:
    """A verbatim hit (distance=0.0) is the STRONGEST possible match. Naive
    `r.get('distance') or 1.0` would falsy-coerce 0.0 to 1.0 and silently
    drop it — this test guards against that regression."""
    store = _make_store(
        builtin_hits=[
            {"text": "perfect", "metadata": {"filename": "f.md"}, "distance": 0.0},
        ],
        company_hits=[
            {"text": "also perfect", "metadata": {"filename": "g.md"}, "distance": 0.0},
        ],
    )
    out = retrieve(query="a real question about strategy", store=store, review_store=fake_review_store)
    assert "perfect" in out
    assert "also perfect" in out


# ---------------------------------------------------------------------------
# The pending gate: withheld items never reach a specialist
# ---------------------------------------------------------------------------


def _hit(filename: str, text: str, *, distance: float = 0.1) -> dict[str, Any]:
    return {
        "text": text,
        "distance": distance,
        "metadata": {"filename": filename, "domain": "finance", "type": "builtin"},
    }


def _review_store(withheld: set[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        get_withheld_keys=lambda _ct: withheld,
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )


def test_withheld_builtin_chunk_is_not_retrieved() -> None:
    """A doc queued for curation must not reach the Executive."""
    store = _make_store(
        [_hit("queued.md", "WITHHELD CONTENT"), _hit("trusted.md", "TRUSTED CONTENT")],
        [],
    )
    out = retrieve(
        query="a real question about capital structure",
        store=store,
        review_store=_review_store({("finance", "queued.md")}),
    )
    assert "TRUSTED CONTENT" in out
    assert "WITHHELD CONTENT" not in out


def test_nothing_withheld_returns_everything() -> None:
    store = _make_store([_hit("a.md", "ALPHA"), _hit("b.md", "BETA")], [])
    out = retrieve(
        query="a real question about capital structure",
        store=store,
        review_store=_review_store(set()),
    )
    assert "ALPHA" in out
    assert "BETA" in out


def test_withheld_external_source_is_not_retrieved() -> None:
    hit = _hit("oer.md", "EXTERNAL CONTENT")
    hit["metadata"]["source_id"] = "openstax-finance"
    store = _make_store([hit, _hit("trusted.md", "TRUSTED CONTENT")], [])
    rs = _review_store(set())
    rs.get_withheld_source_ids = lambda: {"openstax-finance"}

    out = retrieve(query="a real question about capital structure", store=store, review_store=rs)

    assert "TRUSTED CONTENT" in out
    assert "EXTERNAL CONTENT" not in out


def test_retrieve_failures_honors_withheld_items() -> None:
    """Failure case studies live in their own collection and used to ignore
    review state entirely — rejecting one did nothing."""
    from openexecutive.knowledge.retriever import retrieve_failures
    from openexecutive.knowledge.store import ChromaDBStore

    store = MagicMock()

    def fake_query(*, query_text: str, collection: str, domain_filter: Any, n_results: int):
        if collection == ChromaDBStore.FAILURES_COLLECTION:
            return [
                _hit("bad_case.md", "REJECTED FAILURE STORY"),
                _hit("good_case.md", "USEFUL FAILURE STORY"),
            ]
        return []

    store.query.side_effect = fake_query

    out = retrieve_failures(
        query="what went wrong with this rollout",
        store=store,
        review_store=_review_store({("finance", "bad_case.md")}),
    )

    assert "USEFUL FAILURE STORY" in out
    assert "REJECTED FAILURE STORY" not in out
