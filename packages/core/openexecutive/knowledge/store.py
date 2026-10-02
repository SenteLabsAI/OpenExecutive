from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Chroma's DefaultEmbeddingFunction builds a new ONNXMiniLM_L6_V2 on every
# call, so every query and upsert loads the model from disk (about 200 ms a
# query). Its ONNX session also keeps the CPU memory arena on and embeds 32
# texts per run, each padded to 256 tokens, so a large ingest peaks several
# hundred MB above the model. The stores share one session per process
# instead, with the arena off and _EMBED_BATCH texts per run: the same model,
# the same vectors, a bounded peak.
#
# It answers to DefaultEmbeddingFunction's name ("default") with an empty
# config, which is what existing collections record: Chroma refuses to open
# a collection with a differently named function, and rebuilds "default"
# from the stored config if older code opens one this created. It must NOT
# be a DefaultEmbeddingFunction instance: Chroma's Collection._embed skips
# any such instance and embeds with a fresh one from the config instead.
_EMBED_BATCH = 4
_embedding_function_lock = threading.Lock()
_shared_embedding_function: Any = None


def _embedding_function() -> Any:
    global _shared_embedding_function
    with _embedding_function_lock:
        if _shared_embedding_function is None:
            _shared_embedding_function = _build_embedding_function()
        return _shared_embedding_function


def _build_embedding_function() -> Any:
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    class _SharedMiniLM(ONNXMiniLM_L6_V2):  # type: ignore[misc]
        _session: Any = None
        _session_lock = threading.Lock()

        @staticmethod
        def name() -> str:
            return "default"

        def get_config(self) -> dict[str, Any]:
            return {}

        @property
        def model(self) -> Any:
            with self._session_lock:
                if self._session is None:
                    so = self.ort.SessionOptions()
                    so.log_severity_level = 3
                    so.graph_optimization_level = self.ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    so.enable_cpu_mem_arena = False
                    # Chroma's own choice: every available provider but CoreML.
                    providers = [
                        p for p in self.ort.get_available_providers()
                        if p != "CoreMLExecutionProvider"
                    ]
                    self._session = self.ort.InferenceSession(
                        os.path.join(self.DOWNLOAD_PATH, self.EXTRACTED_FOLDER_NAME, "model.onnx"),
                        providers=providers,
                        sess_options=so,
                    )
                return self._session

        def _forward(self, documents: list[str], batch_size: int = _EMBED_BATCH) -> Any:
            return super()._forward(documents, batch_size=_EMBED_BATCH)

    return _SharedMiniLM()


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
    # Files synced from shared Google Drive folders (knowledge.drive_sync).
    # Separate from COMPANY for the same reason as NOTION: a shared folder is
    # multi-writer and unreviewed. Retrieved under its own labelled,
    # lower-ranked section.
    DRIVE_COLLECTION = "drive_docs"
    # Files synced from OneDrive folders (knowledge.onedrive_sync). Isolated
    # and ranked like DRIVE, for the same reason.
    ONEDRIVE_COLLECTION = "onedrive_docs"
    # Files attached in an integration channel. Same isolation reasoning as
    # the two above, taken one step further: this collection is NEVER
    # queried — not by ``retriever.retrieve``, not by anything else.
    #
    # An attachment's content is chosen by whoever sent the message. It
    # passes through no upload endpoint, gets no review, and leaves no copy
    # in ``company/docs/``. It is already inlined into the turn that carried
    # it, which is where it is useful; what it must not become is company
    # knowledge that resurfaces in later, unrelated turns.
    #
    # These rows used to live in COMPANY_COLLECTION, isolated only by a
    # domain outside the specialist set. That isolated nothing — see
    # ``query`` below, and knowledge.general_catch_all in
    # architecture-facts.yaml. The collection is the boundary; a domain
    # value never was.
    ATTACHMENT_COLLECTION = "inbound_attachments"

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
            embedding_function=_embedding_function(),
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

    def iter_chunk_metadata(
        self, collection: str, where: dict[str, Any] | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return every ``(chunk_id, metadata)`` pair in *collection*.

        Chroma's ``where`` has no prefix/substring operator, so metadata
        patterns (rather than exact matches) have to be filtered in Python.
        ``where`` (an exact-match filter) narrows the scan first, for a
        collection that also holds rows the caller never needs — the built-in
        seed reads only ``type=builtin`` rows, not the external OER corpus
        that shares BUILTIN.
        """
        try:
            col = self._get_or_create_collection(collection)
            if where:
                rows = col.get(where=where, include=["metadatas"])
            else:
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

    def get_documents_by_ids(
        self, collection: str, ids: list[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return ``(chunk_id, text, metadata)`` for each id that exists.

        The counterpart to :meth:`iter_chunk_metadata`, which deliberately
        fetches metadata only: that scan runs on every boot, and pulling the
        text of every chunk along with it would cost the full collection in
        memory each time to find nothing in the steady state. Callers that
        need the text find candidate ids with the cheap scan first, then ask
        for those ids here.

        Empty list in, empty list out — guarded before the call, because
        ``col.get(ids=[])`` degrades to "fetch everything", the same footgun
        :meth:`delete_by_ids` guards against.

        Batched for the same reason ``add_documents`` batches: a channel that
        has been collecting attachments for months holds more rows than one
        request should materialise at once.
        """
        if not ids:
            return []
        out: list[tuple[str, str, dict[str, Any]]] = []
        batch_size = 100
        try:
            col = self._get_or_create_collection(collection)
            for i in range(0, len(ids), batch_size):
                rows = col.get(
                    ids=ids[i : i + batch_size], include=["documents", "metadatas"]
                )
                got_ids = rows.get("ids") or []
                docs = rows.get("documents") or []
                metas = rows.get("metadatas") or []
                for cid, doc, md in zip(got_ids, docs, metas, strict=False):
                    out.append(
                        (
                            str(cid),
                            str(doc) if doc is not None else "",
                            dict(md) if isinstance(md, dict) else {},
                        )
                    )
        except Exception:
            logging.getLogger(__name__).exception(
                "get_documents_by_ids failed for %d id(s) in %s", len(ids), collection
            )
            return []
        return out

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

    def delete_drive_docs(self) -> None:
        """Drop every synced Google Drive chunk."""
        self.delete_documents(collection=self.DRIVE_COLLECTION, where={"type": "drive"})

    def delete_onedrive_docs(self) -> None:
        """Drop every synced OneDrive chunk."""
        self.delete_documents(collection=self.ONEDRIVE_COLLECTION, where={"type": "onedrive"})

    def delete_attachment_docs(self) -> None:
        """Drop every inbound attachment chunk, plus any pre-isolation
        leftovers still tagged ``type=attachment`` in COMPANY.

        Drop-and-recreate rather than a ``where`` delete, unlike
        :meth:`delete_notion_docs`: the callers are client-slot restore and
        fixture reset, where one surviving row is one company's attachment
        visible to the next. "Deleted every row carrying the tag" is not the
        same guarantee as "the collection is empty", and here only the
        second one is good enough.

        The COMPANY sweep is belt-and-braces — every caller drops that
        collection wholesale a line or two earlier — but it keeps this
        method correct when called on its own.
        """
        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete_collection(self.ATTACHMENT_COLLECTION)
        self._get_or_create_collection(self.ATTACHMENT_COLLECTION)
        self.delete_documents(
            collection=self.COMPANY_COLLECTION, where={"type": "attachment"}
        )
