from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class KnowledgeStore(ABC):
    @abstractmethod
    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str,
    ) -> None: ...

    @abstractmethod
    def query(
        self,
        query_text: str,
        collection: str,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def collection_exists(self, collection: str) -> bool: ...

    @abstractmethod
    def get_collection_count(self, collection: str) -> int: ...

    @abstractmethod
    def delete_documents(self, collection: str, where: dict[str, Any]) -> None: ...


class ChromaDBStore(KnowledgeStore):
    BUILTIN_COLLECTION = "builtin_knowledge"
    COMPANY_COLLECTION = "company_docs"
    FAILURES_COLLECTION = "failure_cases"
    # Web-research artifacts persisted from executive_research runs. Kept
    # SEPARATE from COMPANY_COLLECTION so unvetted, machine-generated
    # research never blends into curated company knowledge — it is
    # retrieved under its own clearly-labelled, lower-ranked section.
    RESEARCH_COLLECTION = "recent_research"
    # Synced Notion wiki pages. Kept SEPARATE from COMPANY_COLLECTION
    # because a Notion share is multi-writer and unreviewed — anyone
    # who can edit a shared page can inject text the agents will read.
    # Retrieved under its own clearly-labelled, lower-ranked section.
    NOTION_COLLECTION = "notion_wiki"

    def __init__(self, persist_directory: str | Path = "./chroma_db") -> None:
        import chromadb
        from chromadb.config import Settings

        self._client = chromadb.PersistentClient(
            path=str(persist_directory),
            settings=Settings(anonymized_telemetry=False),
        )

    def _get_or_create_collection(self, name: str) -> Any:
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def add_documents(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
        collection: str = BUILTIN_COLLECTION,
    ) -> None:
        col = self._get_or_create_collection(collection)
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            col.upsert(
                documents=texts[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
                ids=ids[i : i + batch_size],
            )

    def query(
        self,
        query_text: str,
        collection: str = BUILTIN_COLLECTION,
        domain_filter: list[str] | None = None,
        n_results: int = 5,
    ) -> list[dict[str, Any]]:
        col = self._get_or_create_collection(collection)

        count = col.count()
        if count == 0:
            return []

        where: dict[str, Any] | None = None
        if domain_filter:
            if len(domain_filter) == 1:
                where = {"domain": domain_filter[0]}
            else:
                where = {"domain": {"$in": domain_filter}}

        query_kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": min(n_results, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_kwargs["where"] = where

        results = col.query(**query_kwargs)

        output = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
                strict=False,
            ):
                output.append({"text": doc, "metadata": meta, "distance": dist})
        return output

    def collection_exists(self, collection: str) -> bool:
        try:
            self._client.get_collection(collection)
            return True
        except Exception:
            return False

    def get_collection_count(self, collection: str) -> int:
        try:
            col = self._client.get_collection(collection)
            return col.count()
        except Exception:
            return 0

    def delete_documents(self, collection: str, where: dict[str, Any]) -> None:
        try:
            col = self._get_or_create_collection(collection)
            col.delete(where=where)
        except Exception:
            pass

    def iter_chunk_metadata(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        """Return every ``(chunk_id, metadata)`` pair in *collection*.

        Chroma's ``where`` has no prefix/substring operator, so metadata
        patterns (rather than exact matches) have to be filtered in Python.
        Only used against the small ``company_docs`` collection.
        """
        try:
            col = self._get_or_create_collection(collection)
            rows = col.get(include=["metadatas"])
        except Exception:
            return []
        ids = rows.get("ids") or []
        metas = rows.get("metadatas") or []
        return [
            (str(cid), dict(md) if isinstance(md, dict) else {})
            for cid, md in zip(ids, metas, strict=False)
        ]

    def delete_by_ids(self, collection: str, ids: list[str]) -> int:
        """Delete specific chunk ids; returns how many were actually deleted.

        No-op on an empty list — Chroma treats a delete with neither ids nor
        where as 'delete everything', so the guard must come before the call,
        not inside it.

        Returns 0 rather than ``len(ids)`` when the delete raises, so a caller
        reporting the count cannot claim to have removed rows that are still
        there.
        """
        if not ids:
            return 0
        try:
            col = self._get_or_create_collection(collection)
            col.delete(ids=ids)
            return len(ids)
        except Exception:
            logging.getLogger(__name__).exception(
                "delete_by_ids failed for %d id(s) in %s", len(ids), collection
            )
            return 0

    def delete_company_docs(self) -> None:
        """Delete and recreate the company_docs collection, clearing all indexed documents."""
        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete_collection(self.COMPANY_COLLECTION)
        # Recreate with the same HNSW settings so subsequent upserts work normally.
        self._get_or_create_collection(self.COMPANY_COLLECTION)

    def delete_notion_docs(self) -> None:
        """Drop synced Notion chunks from the isolated collection and any
        leftover COMPANY rows tagged ``type=notion`` (pre-isolation ingest)."""
        self.delete_documents(collection=self.NOTION_COLLECTION, where={"type": "notion"})
        self.delete_documents(collection=self.COMPANY_COLLECTION, where={"type": "notion"})
