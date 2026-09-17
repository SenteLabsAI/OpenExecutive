"""Company-document identity: the orphan sweep and the `general` catch-all.

Companions to `test_documents_upload.py`, covering the two halves of the fix
that live below the API layer:

* `reconcile_company_docs` — deployed stores still hold chunks the old upload
  path indexed under a random `tmpXXXXXXXX` staging name (#113). Nothing can
  reach them: DELETE matches on `filename`, and a re-upload lands on different
  ids, so they are neither replaceable nor removable.
* `retriever._with_general` — `general` is the default on `POST /documents`
  and the default option in the UI's picker, but it maps to no specialist
  (#114), so an unclassified document was retrievable by none of them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.knowledge.loader import (
    GENERAL_DOMAIN,
    UPLOAD_DOMAINS,
    _make_chunk_id,
    reconcile_company_docs,
)
from openexecutive.knowledge.retriever import DOMAIN_ALIASES, _with_general

from ._fake_store import FakeStore as _FakeStore


def _orphan(name: str, index: int = 0) -> tuple[str, dict[str, Any]]:
    """A row as the pre-fix upload path wrote it: temp name, temp-path source."""
    return (
        _make_chunk_id(f"/tmp/{name}", index),
        {"domain": "finance", "filename": name, "source": f"/tmp/{name}", "chunk_index": index},
    )


def _docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


# ── the sweep ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_drops_orphaned_temp_chunks(tmp_path: Path) -> None:
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md"), _orphan("tmp8qd9liu4.md", 1)]))

    swept, _ = await reconcile_company_docs(store, _docs_dir(tmp_path))

    assert swept == 2
    assert store.rows == {}


@pytest.mark.asyncio
async def test_sweep_spares_real_documents(tmp_path: Path) -> None:
    docs = _docs_dir(tmp_path)
    (docs / "plan.md").write_text("# Plan\nGrow revenue.")
    store = _FakeStore(
        {
            _make_chunk_id("plan.md", 0): {
                "domain": "strategy",
                "filename": "plan.md",
                "source": "plan.md",
                "chunk_index": 0,
            },
            **dict([_orphan("tmp1du9epr4.md")]),
        }
    )

    swept, indexed = await reconcile_company_docs(store, docs)

    assert swept == 1
    assert indexed == 0, "a document already indexed under its real name is left alone"
    assert store.filenames() == {"plan.md"}


@pytest.mark.asyncio
async def test_sweep_preserves_the_domain_of_indexed_documents(tmp_path: Path) -> None:
    """Re-ingesting from disk would retag everything `general` — the on-disk
    copy carries no record of the domain the uploader chose."""
    docs = _docs_dir(tmp_path)
    (docs / "plan.md").write_text("# Plan\nGrow revenue.")
    store = _FakeStore(
        {
            _make_chunk_id("plan.md", 0): {
                "domain": "finance",
                "filename": "plan.md",
                "source": "plan.md",
                "chunk_index": 0,
            }
        }
    )

    await reconcile_company_docs(store, docs)

    assert [m["domain"] for m in store.rows.values()] == ["finance"]


@pytest.mark.asyncio
async def test_sweep_reindexes_a_document_with_no_chunks(tmp_path: Path) -> None:
    """The recovery path: the file survived on disk, its chunks were orphans."""
    docs = _docs_dir(tmp_path)
    (docs / "plan.md").write_text("# Plan\n" + "grow revenue thirty percent. " * 50)
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md")]))

    swept, indexed = await reconcile_company_docs(store, docs)

    assert (swept, indexed) == (1, 1)
    assert store.filenames() == {"plan.md"}
    assert {m["domain"] for m in store.rows.values()} == {GENERAL_DOMAIN}


@pytest.mark.asyncio
async def test_sweep_is_idempotent(tmp_path: Path) -> None:
    docs = _docs_dir(tmp_path)
    (docs / "plan.md").write_text("# Plan\n" + "grow revenue thirty percent. " * 50)
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md")]))

    await reconcile_company_docs(store, docs)
    snapshot = dict(store.rows)
    second = await reconcile_company_docs(store, docs)

    assert second == (0, 0)
    assert store.rows == snapshot


@pytest.mark.asyncio
async def test_sweep_spares_a_real_document_named_like_a_temp_file(tmp_path: Path) -> None:
    docs = _docs_dir(tmp_path)
    (docs / "tmp1du9epr4.md").write_text("# Notes\nA real file, awkwardly named.")
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md")]))

    swept, _ = await reconcile_company_docs(store, docs)

    assert swept == 0, "a document still present on disk is never swept"


@pytest.mark.asyncio
async def test_sweep_skips_entirely_when_the_docs_dir_is_missing(tmp_path: Path) -> None:
    """An absent docs dir is ambiguous: "no documents" and "the volume is not
    mounted yet" look identical. Sweeping in the second case destroys exactly
    the documents that were recoverable, because the file that would have
    spared each one is invisible."""
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md")]))

    swept, indexed = await reconcile_company_docs(store, tmp_path / "does-not-exist")

    assert (swept, indexed) == (0, 0)
    assert store.rows, "an unmounted docs dir must not be read as 'delete everything'"


@pytest.mark.asyncio
async def test_sweep_continues_past_an_unreadable_document(tmp_path: Path) -> None:
    docs = _docs_dir(tmp_path)
    (docs / "broken.pdf").write_bytes(b"not a real pdf")
    (docs / "plan.md").write_text("# Plan\n" + "grow revenue thirty percent. " * 50)
    store = _FakeStore()

    _, indexed = await reconcile_company_docs(store, docs)

    assert indexed == 1
    assert store.filenames() == {"plan.md"}


@pytest.mark.asyncio
async def test_sweep_ignores_subdirectories(tmp_path: Path) -> None:
    """Notion sync writes Markdown into docs/notion/, which belongs to the
    notion_wiki collection — it must not be pulled into company_docs."""
    docs = _docs_dir(tmp_path)
    (docs / "notion").mkdir()
    (docs / "notion" / "page.md").write_text("# Wiki\n" + "wiki content. " * 50)
    store = _FakeStore()

    swept, indexed = await reconcile_company_docs(store, docs)

    assert (swept, indexed) == (0, 0)
    assert store.rows == {}


def test_delete_by_ids_ignores_an_empty_list() -> None:
    """Chroma's `col.delete()` with neither `ids` nor `where` deletes the
    ENTIRE collection, and the sweep passes an empty list whenever there is
    nothing orphaned — i.e. on every boot of a healthy install."""
    from openexecutive.knowledge.store import ChromaDBStore

    calls: list[Any] = []
    store = ChromaDBStore.__new__(ChromaDBStore)  # no ChromaDB client needed
    store._get_or_create_collection = lambda name: calls.append(name)  # type: ignore[method-assign]

    store.delete_by_ids("company_docs", [])

    assert calls == [], "an empty id list must not reach Chroma at all"


# ── the `general` catch-all ───────────────────────────────────────────────


def test_general_is_an_accepted_upload_domain() -> None:
    assert GENERAL_DOMAIN in UPLOAD_DOMAINS


def test_with_general_widens_a_specialist_filter() -> None:
    assert _with_general(["finance"]) == ["finance", GENERAL_DOMAIN]


def test_with_general_does_not_duplicate() -> None:
    assert _with_general([GENERAL_DOMAIN]) == [GENERAL_DOMAIN]


def test_with_general_leaves_an_absent_filter_alone() -> None:
    assert _with_general(None) is None, "None already matches every domain"


@pytest.mark.parametrize("specialist", sorted(DOMAIN_ALIASES))
def test_every_specialist_retrieves_general_company_docs(specialist: str) -> None:
    """The whole point of #114: an unclassified upload must be visible to all
    specialists rather than to none."""
    assert GENERAL_DOMAIN in (_with_general(DOMAIN_ALIASES[specialist]) or [])


def test_with_general_leaves_an_empty_filter_alone() -> None:
    """`store.query` treats both None and [] as "no filter", so [] already
    matches every domain — widening it to ["general"] would *narrow* it to
    general-only, inverting this function's purpose."""
    assert _with_general([]) == []


@pytest.mark.asyncio
async def test_recovered_document_lands_under_general_despite_the_install_path(
    tmp_path: Path,
) -> None:
    """`infer_domain_from_path` scans every component of the absolute path, so
    an install rooted under e.g. /srv/finance/ would tag recovered documents
    `finance`. The reconcile must pass the domain explicitly."""
    docs = tmp_path / "finance" / "company" / "docs"
    docs.mkdir(parents=True)
    (docs / "plan.md").write_text("# Plan\n" + "grow revenue thirty percent. " * 50)
    store = _FakeStore()

    await reconcile_company_docs(store, docs)

    assert {m["domain"] for m in store.rows.values()} == {GENERAL_DOMAIN}


@pytest.mark.asyncio
async def test_sweep_reports_zero_when_the_delete_fails(tmp_path: Path) -> None:
    """The boot log must not claim to have dropped rows that are still there."""
    docs = _docs_dir(tmp_path)
    store = _FakeStore(dict([_orphan("tmp1du9epr4.md")]))
    store.delete_by_ids = lambda collection, ids: 0  # type: ignore[assignment]

    swept, _ = await reconcile_company_docs(store, docs)

    assert swept == 0


class _RecordingStore(_FakeStore):
    """Captures the domain filter each collection is queried with."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: dict[str, list[str] | None] = {}

    def query(
        self,
        query_text: str,
        collection: str,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        self.queries[collection] = domain_filter
        return []


def test_retrieve_widens_only_the_company_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the wiring, not just the helper: reverting the `_with_general`
    call in `retrieve()` would otherwise leave the whole suite green while
    #114 silently regresses."""
    from openexecutive.knowledge import retriever
    from openexecutive.knowledge.store import ChromaDBStore

    store = _RecordingStore()
    monkeypatch.setattr(retriever, "_default_review_store", lambda: _NullReviewStore())

    retriever.retrieve(
        query="what is our runway and hiring plan for next quarter",
        specialist_name="cfo",
        store=store,  # type: ignore[arg-type]
    )

    assert store.queries[ChromaDBStore.COMPANY_COLLECTION] == ["finance", GENERAL_DOMAIN]
    assert store.queries[ChromaDBStore.BUILTIN_COLLECTION] == ["finance"]
    assert store.queries[ChromaDBStore.NOTION_COLLECTION] == ["finance"]


class _NullReviewStore:
    # `get_withheld_*` superseded the rejected-only lookups when `pending`
    # became a real retrieval gate; these are the methods `retrieve()` calls.
    def get_withheld_keys(self, *a: Any, **k: Any) -> set[tuple[str, str]]:
        return set()

    def get_withheld_source_ids(self, *a: Any, **k: Any) -> set[str]:
        return set()

    def get_priority_map(self, *a: Any, **k: Any) -> dict[str, str]:
        return {}

    def list_annotations(self, *a: Any, **k: Any) -> list[Any]:
        return []
