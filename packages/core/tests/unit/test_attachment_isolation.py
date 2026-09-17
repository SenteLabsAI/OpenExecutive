"""Integration attachments are isolated from company knowledge.

Two guarantees, both regressions of a real bug:

1. ``retriever.retrieve`` never queries the attachment collection, so an
   attachment cannot resurface as company knowledge in a later turn.
2. Rows written by the old path — which put them in ``company_docs`` and
   relied on a non-specialist domain to keep them out — are migrated across
   on boot rather than left behind.

The bug that motivated both: an unrecognised domain does NOT exclude a row.
``ChromaDBStore.query`` builds a ``where`` clause only when ``domain_filter``
is truthy, and an Executive-level ``retrieve`` passes none, so those chunks
matched and were rendered under "From your company documents" — top-ranked
and indistinguishable from a curated upload.
"""
from __future__ import annotations

from typing import Any

from openexecutive.knowledge.loader import (
    ATTACHMENT_DOMAIN,
    ATTACHMENT_SOURCE_PREFIX,
    migrate_attachments_out_of_company_docs,
)
from openexecutive.knowledge.store import ChromaDBStore


class FakeStore:
    """Collection-aware in-memory store.

    Local rather than the shared ``tests.unit._fake_store``, which is flat and
    drops the ``collection`` argument — it cannot express "this row went to a
    different collection", which is the whole subject here. Same shape as the
    fakes in ``test_recent_research_knowledge`` and ``test_notion_sync``.
    """

    def __init__(self) -> None:
        # collection -> chunk_id -> {"text": str, "metadata": dict}
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.add_failures: set[str] = set()

    def _col(self, collection: str) -> dict[str, dict[str, Any]]:
        return self.collections.setdefault(collection, {})

    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str,
    ) -> None:
        if collection in self.add_failures:
            raise RuntimeError(f"simulated write failure into {collection}")
        col = self._col(collection)
        for chunk_id, text, meta in zip(ids, texts, metadatas, strict=True):
            col[chunk_id] = {"text": text, "metadata": dict(meta)}

    def iter_chunk_metadata(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        return [(cid, dict(r["metadata"])) for cid, r in self._col(collection).items()]

    def get_documents_by_ids(
        self, collection: str, ids: list[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        if not ids:
            return []
        col = self._col(collection)
        return [
            (cid, col[cid]["text"], dict(col[cid]["metadata"]))
            for cid in ids
            if cid in col
        ]

    def delete_by_ids(self, collection: str, ids: list[str]) -> int:
        if not ids:
            return 0
        col = self._col(collection)
        return sum(col.pop(cid, None) is not None for cid in ids)

    def query(
        self,
        query_text: str,
        collection: str,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Models the one behaviour this whole change turns on: a falsy
        domain filter is NO filter, so it matches every row — the same
        `if domain_filter:` gate `ChromaDBStore.query` uses."""
        rows = []
        for r in self._col(collection).values():
            if domain_filter and r["metadata"].get("domain") not in domain_filter:
                continue
            rows.append({"text": r["text"], "metadata": r["metadata"], "distance": 0.1})
        return rows[:n_results]

    # -- helpers ----------------------------------------------------------
    def seed(
        self,
        collection: str,
        chunk_id: str,
        text: str,
        **metadata: Any,
    ) -> None:
        self._col(collection)[chunk_id] = {"text": text, "metadata": metadata}

    def names(self, collection: str) -> set[str]:
        return {r["metadata"].get("filename") for r in self._col(collection).values()}


def _seed_legacy_attachment(store: FakeStore, name: str, text: str) -> str:
    """A row exactly as the pre-fix path wrote it: in COMPANY, no type tag."""
    chunk_id = f"id-{name}"
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        chunk_id,
        text,
        domain="company_docs",
        filename=f"{ATTACHMENT_SOURCE_PREFIX}{name}",
        source=f"{ATTACHMENT_SOURCE_PREFIX}{name}",
        chunk_index=0,
    )
    return chunk_id


def _migrate(store: FakeStore) -> int:
    return migrate_attachments_out_of_company_docs(store)  # type: ignore[arg-type]


# ── migration ─────────────────────────────────────────────────────────────


def test_legacy_rows_move_to_the_isolated_collection_with_their_text():
    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "ACME gross margin is 91 percent.")
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-plan",
        "Curated plan.",
        domain="strategy",
        filename="plan.md",
        source="plan.md",
        chunk_index=0,
    )

    assert _migrate(store) == 1

    assert store.names(ChromaDBStore.COMPANY_COLLECTION) == {"plan.md"}
    attachments = store.collections[ChromaDBStore.ATTACHMENT_COLLECTION]
    assert len(attachments) == 1
    moved = next(iter(attachments.values()))
    # The text has to survive: attachments are never written to company/docs/,
    # so the store holds the only copy and a lossy move destroys it.
    assert moved["text"] == "ACME gross margin is 91 percent."


def test_migration_preserves_chunk_ids():
    """Ids are what make a re-ingest of the same attachment upsert rather than
    duplicate, and what make this migration idempotent."""
    store = FakeStore()
    chunk_id = _seed_legacy_attachment(store, "leak.md", "text")

    _migrate(store)

    assert chunk_id in store.collections[ChromaDBStore.ATTACHMENT_COLLECTION]


def test_migration_normalizes_domain_and_type():
    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "text")

    _migrate(store)

    moved = next(iter(store.collections[ChromaDBStore.ATTACHMENT_COLLECTION].values()))
    assert moved["metadata"]["type"] == "attachment"
    assert moved["metadata"]["domain"] == ATTACHMENT_DOMAIN


def test_migration_is_idempotent():
    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "text")

    assert _migrate(store) == 1
    before = dict(store.collections[ChromaDBStore.ATTACHMENT_COLLECTION])

    assert _migrate(store) == 0
    assert store.collections[ChromaDBStore.ATTACHMENT_COLLECTION] == before


def test_migration_is_a_noop_on_a_clean_store():
    store = FakeStore()
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-plan",
        "Curated plan.",
        domain="strategy",
        filename="plan.md",
        source="plan.md",
    )

    assert _migrate(store) == 0
    assert ChromaDBStore.ATTACHMENT_COLLECTION not in store.collections
    assert store.names(ChromaDBStore.COMPANY_COLLECTION) == {"plan.md"}


def test_migration_does_not_match_a_curated_document_named_like_one():
    """`startswith`, not `in` — a real company document about attachments is
    not an attachment."""
    store = FakeStore()
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-policy",
        "How we handle attachments.",
        domain="operations",
        filename="attachments-policy.md",
        source="attachments-policy.md",
    )

    assert _migrate(store) == 0
    assert store.names(ChromaDBStore.COMPANY_COLLECTION) == {"attachments-policy.md"}


def test_migration_writes_before_it_deletes():
    """The ordering is the safety property.

    Chroma spans no transaction across two collections. A crash after the
    write leaves a duplicate the next run collapses; a crash after a delete
    destroys the only copy of an attachment, which has no file on disk to be
    re-ingested from. So a failed write must leave COMPANY untouched.
    """
    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "irreplaceable")
    store.add_failures.add(ChromaDBStore.ATTACHMENT_COLLECTION)

    try:
        _migrate(store)
    except RuntimeError:
        pass

    assert store.names(ChromaDBStore.COMPANY_COLLECTION) == {
        f"{ATTACHMENT_SOURCE_PREFIX}leak.md"
    }


def test_migration_also_catches_rows_already_carrying_the_type_tag():
    """Rows written by an interim build land in COMPANY with the tag but no
    prefix guarantee; both markers have to be recognised."""
    store = FakeStore()
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-tagged",
        "text",
        domain=ATTACHMENT_DOMAIN,
        filename="oddly-named.md",
        source="oddly-named.md",
        type="attachment",
    )

    assert _migrate(store) == 1
    assert store.collections[ChromaDBStore.COMPANY_COLLECTION] == {}


# ── retrieval isolation ───────────────────────────────────────────────────


def _fake_review_store() -> Any:
    """ReviewStore stub returning empty sets.

    `retrieve` falls back to the real `_default_review_store()`, which opens a
    SQLite file that a clean checkout does not have — so without this the test
    passes or fails depending on whether some earlier test created the DB.
    Same shape as the fixture in `test_retriever_gating`.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        get_withheld_keys=lambda _ct: set(),
        get_withheld_source_ids=lambda: set(),
        get_priority_map=lambda _ct: {},
        list_annotations=lambda domains=None, active_only=True: [],
    )


def _retrieve_collections_queried(**retrieve_kwargs: Any) -> list[str]:
    """Run the real `retrieve` and report which collections it asked for."""
    from unittest.mock import MagicMock

    from openexecutive.knowledge import retriever

    seen: list[str] = []

    def _query(query_text: str, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        seen.append(collection)
        return []

    store = MagicMock()
    store.query.side_effect = _query
    retriever.retrieve(
        query="what is the gross margin for the quarter, in detail?",
        store=store,
        review_store=_fake_review_store(),
        **retrieve_kwargs,
    )
    return seen


def test_retrieve_never_queries_the_attachment_collection():
    """Both the unfiltered Executive turn and a specialist-routed call."""
    for kwargs in ({"specialist_name": None}, {"specialist_name": "finance"}):
        seen = _retrieve_collections_queried(**kwargs)
        assert ChromaDBStore.COMPANY_COLLECTION in seen, "company still retrieved"
        assert ChromaDBStore.ATTACHMENT_COLLECTION not in seen, (
            f"attachment collection was queried with {kwargs}"
        )


def test_a_curated_upload_named_like_an_attachment_is_not_migrated():
    """The prefix is not proof of origin.

    A colon is legal in a filename and `POST /documents` passes one through,
    so a real company document can be called `attachment:q4-plan.md`. Moving
    it would strip it from the collection it belongs in, leave an undeletable
    copy behind, and — because the file is still on disk — have the boot
    reconcile re-index it as `general`, widening an HR document to every
    specialist. The legacy `domain` is what distinguishes the two, and
    UPLOAD_DOMAINS will never accept that value from an upload.
    """
    store = FakeStore()
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-curated",
        "Compensation bands for Q4.",
        domain="hr",  # a real upload domain, not the legacy marker
        filename=f"{ATTACHMENT_SOURCE_PREFIX}q4-comp-plan.md",
        source=f"{ATTACHMENT_SOURCE_PREFIX}q4-comp-plan.md",
    )

    assert _migrate(store) == 0
    assert store.names(ChromaDBStore.COMPANY_COLLECTION) == {
        f"{ATTACHMENT_SOURCE_PREFIX}q4-comp-plan.md"
    }
    assert ChromaDBStore.ATTACHMENT_COLLECTION not in store.collections


def test_migration_reports_only_what_it_actually_removed():
    """`delete_by_ids` returns 0 when the delete raises — it says so in its own
    docstring precisely so a caller cannot claim rows it did not remove.

    Reporting `len(ids)` regardless would log "moved N chunks" on a boot where
    all N are still in COMPANY and still retrievable: the leak announced as
    closed while it is open.
    """
    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "text")
    store.delete_by_ids = lambda collection, ids: 0  # type: ignore[assignment]

    assert _migrate(store) == 0


def test_migration_does_not_clobber_a_newer_copy():
    """Ids collide by design — md5 of the same source name on both sides.

    A row already in the destination was written by the post-fix path and is
    newer than the COMPANY copy, so upserting the old text over it would
    resurrect superseded content. Drop the stale original instead.
    """
    store = FakeStore()
    chunk_id = _seed_legacy_attachment(store, "notes.md", "OLD v1")
    store.seed(
        ChromaDBStore.ATTACHMENT_COLLECTION,
        chunk_id,
        "NEW v2",
        domain=ATTACHMENT_DOMAIN,
        filename=f"{ATTACHMENT_SOURCE_PREFIX}notes.md",
        source=f"{ATTACHMENT_SOURCE_PREFIX}notes.md",
        type="attachment",
    )

    assert _migrate(store) == 1
    assert store.collections[ChromaDBStore.COMPANY_COLLECTION] == {}
    assert store.collections[ChromaDBStore.ATTACHMENT_COLLECTION][chunk_id][
        "text"
    ] == "NEW v2"


def test_unfiltered_retrieval_matches_every_domain_including_unrecognised_ones():
    """The bug itself, as a standing regression.

    This is the premise the old code got wrong: a domain nothing maps to was
    believed to exclude a row. It does not — a falsy filter is NO filter. If
    this ever stops holding, the isolation rationale needs rewriting, not the
    test.
    """
    store = FakeStore()
    store.seed(
        ChromaDBStore.COMPANY_COLLECTION,
        "id-x",
        "secret",
        domain="a-domain-no-specialist-maps-to",
        filename="x.md",
    )

    unfiltered = store.query("q", ChromaDBStore.COMPANY_COLLECTION, domain_filter=None)
    filtered = store.query(
        "q", ChromaDBStore.COMPANY_COLLECTION, domain_filter=["finance"]
    )

    assert len(unfiltered) == 1, "an absent filter must match every domain"
    assert filtered == [], "an explicit filter must still exclude"


def test_migrated_attachment_is_no_longer_reachable_by_an_unfiltered_retrieve():
    """End to end: the leak reproduces before the migration and is gone after.

    Drives the real `retriever.retrieve` on the no-specialist path — the one
    that passes no domain filter — against a store that honours filters the
    way ChromaDB does.
    """
    from openexecutive.knowledge import retriever

    store = FakeStore()
    _seed_legacy_attachment(store, "leak.md", "ACME gross margin is 91 percent.")

    query = "what is the gross margin for the quarter, in detail?"
    rs = _fake_review_store()
    before = retriever.retrieve(
        query=query, specialist_name=None, store=store, review_store=rs
    )
    assert "91 percent" in before, "expected to reproduce the leak pre-migration"

    _migrate(store)

    after = retriever.retrieve(
        query=query, specialist_name=None, store=store, review_store=rs
    )
    assert "91 percent" not in after
