"""retrieve(max_chars=...): the chat's cap on knowledge excerpts.

Every excerpt is re-sent on each model call of a turn, so the chat keeps the
closest matches, across every collection, up to KNOWLEDGE_CHAT_MAX_CHARS.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openexecutive.knowledge.retriever import _fit_to_budget, retrieve


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    import openexecutive.knowledge.retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "_audit_log", lambda *a, **k: None)


def _review_store() -> SimpleNamespace:
    return SimpleNamespace(
        get_withheld_keys=lambda _ct: set(),
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )


def _hit(name: str, distance: float, size: int) -> dict[str, Any]:
    return {"text": (name * size)[:size], "metadata": {"filename": f"{name}.md"}, "distance": distance}


def _store(builtin: list[dict[str, Any]], company: list[dict[str, Any]]) -> MagicMock:
    from openexecutive.knowledge.store import ChromaDBStore

    def fake_query(**kw: Any) -> list[dict[str, Any]]:
        if kw["collection"] == ChromaDBStore.BUILTIN_COLLECTION:
            return builtin
        if kw["collection"] == ChromaDBStore.COMPANY_COLLECTION:
            return company
        return []

    store = MagicMock()
    store.query.side_effect = fake_query
    return store


def test_closest_chunks_across_groups_fill_the_budget() -> None:
    builtin = [_hit("a", 0.30, 400), _hit("b", 0.10, 400)]
    company = [_hit("c", 0.20, 400), _hit("d", 0.50, 400)]
    kept_builtin, kept_company = _fit_to_budget(800, builtin, company)
    assert [r["metadata"]["filename"] for r in kept_builtin] == ["b.md"]
    assert [r["metadata"]["filename"] for r in kept_company] == ["c.md"]


def test_a_shorter_weaker_chunk_still_fits_after_one_is_skipped() -> None:
    kept, = _fit_to_budget(
        700, [_hit("a", 0.1, 500), _hit("b", 0.2, 300), _hit("c", 0.3, 150)]
    )
    assert [r["metadata"]["filename"] for r in kept] == ["a.md", "c.md"]


def test_the_closest_chunk_is_kept_even_over_the_budget() -> None:
    kept, = _fit_to_budget(100, [_hit("a", 0.1, 500), _hit("b", 0.2, 50)])
    assert [r["metadata"]["filename"] for r in kept] == ["a.md"]


def test_groups_keep_their_own_order() -> None:
    kept, = _fit_to_budget(10_000, [_hit("a", 0.4, 10), _hit("b", 0.1, 10)])
    assert [r["metadata"]["filename"] for r in kept] == ["a.md", "b.md"]


def test_retrieve_caps_the_block_when_asked() -> None:
    store = _store(
        builtin=[_hit("far", 0.40, 3000), _hit("near", 0.10, 3000)],
        company=[_hit("doc", 0.20, 3000)],
    )
    out = retrieve(
        query="How should we price the renewal?",
        store=store,
        review_store=_review_store(),
        max_chars=6000,
    )
    assert "[near.md]" in out and "[doc.md]" in out
    assert "[far.md]" not in out


@pytest.mark.parametrize("max_chars", [None, 0])
def test_retrieve_without_a_cap_keeps_every_chunk(max_chars: int | None) -> None:
    store = _store(
        builtin=[_hit("far", 0.40, 3000), _hit("near", 0.10, 3000)],
        company=[_hit("doc", 0.20, 3000)],
    )
    out = retrieve(
        query="How should we price the renewal?",
        store=store,
        review_store=_review_store(),
        max_chars=max_chars,
    )
    assert "[near.md]" in out and "[doc.md]" in out and "[far.md]" in out


def test_chat_cap_setting_defaults_to_six_thousand() -> None:
    from openexecutive.config import get_settings

    assert get_settings().knowledge_chat_max_chars == 6000
