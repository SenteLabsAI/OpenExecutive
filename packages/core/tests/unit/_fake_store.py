"""A shared in-memory stand-in for `ChromaDBStore`.

Models the parts of the store the company-document paths depend on — id-keyed
upsert, `where`-filtered delete, and the metadata scan the boot reconcile uses
— so tests can exercise the real upsert/delete semantics without standing up
ChromaDB and an embedding model.

Both semantics matter to the regressions these tests guard (#113): chunk ids
are derived from the document's filename, so an upsert that keys on id is what
makes a re-upload replace rather than duplicate, and a delete that honours the
`where` clause is what makes `DELETE /documents/{filename}` reach the rows it
claims to remove.
"""
from __future__ import annotations

from typing import Any


class FakeStore:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = dict(rows or {})

    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str,
    ) -> None:
        for chunk_id, meta in zip(ids, metadatas, strict=True):
            self.rows[chunk_id] = meta

    def delete_documents(self, collection: str, where: dict[str, Any]) -> None:
        key, value = next(iter(where.items()))
        self.rows = {i: m for i, m in self.rows.items() if m.get(key) != value}

    def iter_chunk_metadata(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        return list(self.rows.items())

    def delete_by_ids(self, collection: str, ids: list[str]) -> int:
        # Returns the count actually removed, like ChromaDBStore.delete_by_ids —
        # callers report that number, so the fake must not overstate it either.
        return sum(self.rows.pop(chunk_id, None) is not None for chunk_id in ids)

    # -- assertion helpers ------------------------------------------------
    @property
    def added(self) -> list[dict[str, Any]]:
        return list(self.rows.values())

    def filenames(self) -> set[str]:
        return {m["filename"] for m in self.rows.values()}
