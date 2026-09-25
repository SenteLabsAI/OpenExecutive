"""Built-in knowledge shipped after first boot reaches existing installs.

`seed_builtin_knowledge` and `seed_failures` used to return early whenever
their collection was non-empty, so a doc added in a later release was never
indexed on an install seeded by an earlier one — it shipped, registered as an
approved trusted default, and was never retrievable. They now top up: every
`SHIPPED_BUILTIN_FILES` entry with no chunks in the collection is indexed,
exactly once, and a complete store indexes nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openexecutive.knowledge import loader, shipped_manifest
from openexecutive.knowledge.store import ChromaDBStore

BUILTIN = ChromaDBStore.BUILTIN_COLLECTION
FAILURES = ChromaDBStore.FAILURES_COLLECTION


class _Store:
    """Per-collection, id-keyed stand-in for ChromaDBStore's seed surface."""

    def __init__(self) -> None:
        self.cols: dict[str, dict[str, dict[str, Any]]] = {}
        self.scans: list[tuple[str, dict[str, Any] | None]] = []

    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str,
    ) -> None:
        col = self.cols.setdefault(collection, {})
        for chunk_id, meta in zip(ids, metadatas, strict=True):
            col[chunk_id] = dict(meta)

    def get_collection_count(self, collection: str) -> int:
        return len(self.cols.get(collection, {}))

    def iter_chunk_metadata(
        self, collection: str, where: dict[str, Any] | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        self.scans.append((collection, where))
        rows = self.cols.get(collection, {}).items()
        return [
            (cid, dict(m))
            for cid, m in rows
            if not where or all(m.get(k) == v for k, v in where.items())
        ]

    def sources(self, collection: str) -> list[str]:
        return [m["source"] for m in self.cols.get(collection, {}).values()]


def _write(root: Path, rel: str, words: int = 30) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {rel}\n\n" + " ".join(f"w{i}" for i in range(words)), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A private `knowledge/builtin` tree and a manifest the test controls."""
    root = tmp_path / "pkg" / "knowledge" / "builtin"
    root.mkdir(parents=True)
    monkeypatch.setattr(loader, "BUILTIN_KNOWLEDGE_PATH", root)
    monkeypatch.setattr(loader, "FAILURES_KNOWLEDGE_PATH", root / "failures")

    def ship(*rels: str) -> None:
        monkeypatch.setattr(shipped_manifest, "SHIPPED_BUILTIN_FILES", frozenset(rels))

    return root, ship


# ── pure helper ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/app/openexecutive/knowledge/builtin/finance/a.md", "finance/a.md"),
        (
            "/old/venv/lib/python3.11/site-packages/openexecutive/knowledge/builtin/"
            "failures/finance/enron.md",
            "failures/finance/enron.md",
        ),
        # The LAST knowledge/builtin pair wins, so an install that happens to
        # live under another such pair still resolves to the shipped path.
        ("/srv/knowledge/builtin/x/openexecutive/knowledge/builtin/hr/b.md", "hr/b.md"),
        ("/tmp/plan.md", None),
        ("plan.md", None),
        ("/app/openexecutive/knowledge/builtin", None),
    ],
)
def test_builtin_relative_source(source: str, expected: str | None) -> None:
    assert loader._builtin_relative_source(source) == expected


# ── BUILTIN top-up ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_boot_indexes_the_whole_tree(tree) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    _write(root, "sales/b.md")
    _write(root, "skills/general/s.md")
    _write(root, "failures/finance/f.md")
    ship("finance/a.md", "sales/b.md", "failures/finance/f.md")
    store = _Store()

    written = await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]

    assert written > 0
    rels = {loader._builtin_relative_source(s) for s in store.sources(BUILTIN)}
    assert rels == {"finance/a.md", "sales/b.md"}, "skills and failures have their own collections"
    domains = {m["domain"] for m in store.cols[BUILTIN].values()}
    assert domains == {"finance", "sales"}


@pytest.mark.asyncio
async def test_pre_seeded_collection_picks_up_a_newly_shipped_doc_once(tree) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]
    before = dict(store.cols[BUILTIN])

    # A later release ships a new doc.
    _write(root, "sales/new.md", words=40)
    ship("finance/a.md", "sales/new.md")

    written = await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]

    assert written == 1
    new_rows = {cid: m for cid, m in store.cols[BUILTIN].items() if cid not in before}
    assert len(new_rows) == 1
    (meta,) = new_rows.values()
    assert meta["filename"] == "new.md"
    assert meta["domain"] == "sales"
    assert meta["type"] == "builtin"
    # Existing rows are untouched.
    assert {cid: store.cols[BUILTIN][cid] for cid in before} == before

    # Re-running adds nothing.
    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    assert len(store.cols[BUILTIN]) == len(before) + 1


@pytest.mark.asyncio
async def test_complete_store_is_one_filtered_scan_and_no_writes(tree) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]
    store.scans.clear()

    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    # Narrowed to seeded rows: the external OER corpus sharing BUILTIN
    # (type=external) is never pulled on boot.
    assert store.scans == [(BUILTIN, {"type": "builtin"})]


@pytest.mark.asyncio
async def test_untagged_rows_still_count_as_indexed(tree) -> None:
    """Rows without a `type` tag fall back to a full scan instead of being
    read as absent — which would re-index (and, after a move, duplicate)."""
    root, ship = tree
    path = _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    store.add_documents(
        texts=["t"],
        metadatas=[{"domain": "finance", "filename": "a.md", "source": str(path), "chunk_index": 0}],
        ids=[loader._make_chunk_id(str(path), 0)],
        collection=BUILTIN,
    )

    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    assert store.scans == [(BUILTIN, {"type": "builtin"}), (BUILTIN, None)]


@pytest.mark.asyncio
async def test_relocated_install_does_not_duplicate(tree) -> None:
    """Rows written from another absolute install path still count as indexed."""
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    old = "/old/venv/site-packages/openexecutive/knowledge/builtin/finance/a.md"
    store = _Store()
    store.add_documents(
        texts=["t"],
        metadatas=[{"domain": "finance", "filename": "a.md", "source": old,
                    "chunk_index": 0, "type": "builtin"}],
        ids=[loader._make_chunk_id(old, 0)],
        collection=BUILTIN,
    )

    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    assert store.sources(BUILTIN) == [old]


@pytest.mark.asyncio
async def test_top_up_ignores_user_uploads_and_missing_files(tree) -> None:
    """Only manifest entries are topped up; a manifest entry absent on disk is skipped."""
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]

    _write(root, "finance/users_own_draft.md")  # a user upload, not shipped
    ship("finance/a.md", "legal/deleted_here.md")  # shipped, but not on disk

    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    names = {m["filename"] for m in store.cols[BUILTIN].values()}
    assert names == {"a.md"}


@pytest.mark.asyncio
async def test_force_reindexes_in_place(tree) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    first = await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]

    again = await loader.seed_builtin_knowledge(store=store, force=True)  # type: ignore[arg-type]

    assert again == first
    assert len(store.cols[BUILTIN]) == first, "same ids: upserted, not duplicated"


# ── FAILURES top-up ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failures_top_up_is_scoped_to_failure_entries(tree) -> None:
    root, ship = tree
    _write(root, "failures/finance/old.md")
    _write(root, "finance/a.md")
    ship("failures/finance/old.md", "finance/a.md")
    store = _Store()
    await loader.seed_failures(store=store)  # type: ignore[arg-type]
    assert {m["filename"] for m in store.cols[FAILURES].values()} == {"old.md"}

    _write(root, "failures/sales/new_case.md")
    ship("failures/finance/old.md", "failures/sales/new_case.md", "finance/a.md", "sales/b.md")
    _write(root, "sales/b.md")

    written = await loader.seed_failures(store=store)  # type: ignore[arg-type]

    assert written == 1
    metas = list(store.cols[FAILURES].values())
    assert {m["filename"] for m in metas} == {"old.md", "new_case.md"}
    new = next(m for m in metas if m["filename"] == "new_case.md")
    assert (new["domain"], new["type"]) == ("sales", "failure_case")
    assert BUILTIN not in store.cols, "playbook docs never land in FAILURES' seed"
    assert store.scans[-1] == (FAILURES, {"type": "failure_case"})
    assert await loader.seed_failures(store=store) == 0  # type: ignore[arg-type]


# ── against a real ChromaDB store ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_up_against_chromadb(tree, tmp_path: Path) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = ChromaDBStore(persist_directory=str(tmp_path / "chroma"))
    await loader.seed_builtin_knowledge(store=store)
    # An external OER row sharing the collection must not look like a gap
    # or be touched.
    store.add_documents(
        texts=["external passage"],
        metadatas=[{"domain": "finance", "filename": "oer.md", "source": "/cache/oer.md",
                    "source_id": "oer", "chunk_index": 0, "type": "external"}],
        ids=["oer-0"],
        collection=BUILTIN,
    )
    count = store.get_collection_count(BUILTIN)

    _write(root, "sales/new.md")
    ship("finance/a.md", "sales/new.md")
    assert await loader.seed_builtin_knowledge(store=store) == 1
    assert store.get_collection_count(BUILTIN) == count + 1
    assert await loader.seed_builtin_knowledge(store=store) == 0
    assert store.get_collection_count(BUILTIN) == count + 1

    sales = store.query("w1 w2 w3", collection=BUILTIN, domain_filter=["sales"], n_results=5)
    assert [r["metadata"]["filename"] for r in sales] == ["new.md"]


@pytest.mark.asyncio
async def test_top_up_failure_is_logged_and_retried_not_raised(tree, monkeypatch) -> None:
    """An upgrade boot must not fail because a newly shipped doc can't be embedded."""
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]
    before = dict(store.cols[BUILTIN])

    _write(root, "sales/new.md")
    ship("finance/a.md", "sales/new.md")
    real_add = store.add_documents

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(store, "add_documents", boom)
    assert await loader.seed_builtin_knowledge(store=store) == 0  # type: ignore[arg-type]
    assert store.cols[BUILTIN] == before

    # The doc is still missing, so the next boot picks it up.
    monkeypatch.setattr(store, "add_documents", real_add)
    assert await loader.seed_builtin_knowledge(store=store) == 1  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_first_boot_failure_still_raises(tree, monkeypatch) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "add_documents", boom)
    with pytest.raises(RuntimeError):
        await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_blank_shipped_file_is_not_missing_forever(tree) -> None:
    root, ship = tree
    _write(root, "finance/a.md")
    ship("finance/a.md")
    store = _Store()
    await loader.seed_builtin_knowledge(store=store)  # type: ignore[arg-type]

    blank = root / "finance" / "blank.md"
    blank.write_text("  \n", encoding="utf-8")
    ship("finance/a.md", "finance/blank.md")
    missing = loader._missing_shipped_files(
        store,  # type: ignore[arg-type]
        BUILTIN,
        chunk_type="builtin",
        failures=False,
    )
    assert missing == []
