"""The embedding function every ChromaDBStore collection uses.

Chroma's DefaultEmbeddingFunction reloads the model on every call and keeps
the ONNX memory arena, so the store shares one lean session instead. Existing
collections record the function they were created with, so the shared one must
answer to the same name and produce the same vectors.
"""

from __future__ import annotations

from pathlib import Path

import chromadb
import numpy as np
from chromadb.config import Settings
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from openexecutive.knowledge import store as store_mod
from openexecutive.knowledge.store import ChromaDBStore


def test_one_function_is_shared_under_chromas_default_name() -> None:
    ef = store_mod._embedding_function()
    assert ef is store_mod._embedding_function()
    assert ef.name() == DefaultEmbeddingFunction.name() == "default"
    assert ef.get_config() == DefaultEmbeddingFunction().get_config()
    # Chroma's Collection._embed ignores a DefaultEmbeddingFunction instance
    # and embeds with a fresh one rebuilt from the collection config.
    assert not isinstance(ef, DefaultEmbeddingFunction)


def test_vectors_match_chromas_default() -> None:
    texts = ["quarterly revenue forecast for the board", "hiring plan", "word " * 300]
    ours = np.array(store_mod._embedding_function()(texts))
    chromas = np.array(DefaultEmbeddingFunction()(texts))
    assert np.allclose(ours, chromas, atol=1e-5)


def test_session_runs_without_the_memory_arena() -> None:
    ef = store_mod._embedding_function()
    ef(["warm"])
    session = ef.model
    assert session.get_session_options().enable_cpu_mem_arena is False
    assert session is ef.model  # built once, not per call


def test_embeds_a_few_texts_per_run(monkeypatch) -> None:
    minilm = store_mod._embedding_function()
    runs: list[int] = []
    real_run = minilm.model.run

    class Spy:
        def run(self, names, feed):
            runs.append(len(feed["input_ids"]))
            return real_run(names, feed)

    monkeypatch.setattr(minilm, "_session", Spy())
    minilm(["text"] * 10)
    assert runs == [store_mod._EMBED_BATCH] * 2 + [10 - 2 * store_mod._EMBED_BATCH]


def test_opens_a_collection_created_with_chromas_default(tmp_path: Path) -> None:
    # Every collection on an existing install was created this way.
    client = chromadb.PersistentClient(path=str(tmp_path), settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection(ChromaDBStore.COMPANY_COLLECTION, metadata={"hnsw:space": "cosine"})
    col.upsert(
        documents=["quarterly revenue forecast for the board", "engineering hiring plan"],
        metadatas=[{"domain": "finance"}, {"domain": "operations"}],
        ids=["revenue", "hiring"],
    )

    store = ChromaDBStore(persist_directory=tmp_path)
    hits = store.query("revenue forecast", collection=ChromaDBStore.COMPANY_COLLECTION, n_results=1)
    assert hits[0]["metadata"]["domain"] == "finance"
    store.add_documents(
        ["vendor contract renewal terms"], [{"domain": "legal"}], ["contract"],
        collection=ChromaDBStore.COMPANY_COLLECTION,
    )
    assert store.get_collection_count(ChromaDBStore.COMPANY_COLLECTION) == 3


def test_store_queries_and_upserts_embed_through_the_shared_session(tmp_path: Path, monkeypatch) -> None:
    ef = store_mod._embedding_function()
    batches: list[int] = []
    real_forward = type(ef)._forward

    def spy(self, documents, batch_size=store_mod._EMBED_BATCH):
        batches.append(len(documents))
        return real_forward(self, documents, batch_size)

    monkeypatch.setattr(type(ef), "_forward", spy)
    store = ChromaDBStore(persist_directory=tmp_path)
    store.add_documents(
        ["quarterly revenue forecast", "engineering hiring plan"],
        [{"domain": "finance"}, {"domain": "operations"}], ["a", "b"],
        collection=ChromaDBStore.COMPANY_COLLECTION,
    )
    store.query("revenue", collection=ChromaDBStore.COMPANY_COLLECTION, n_results=1)
    assert batches == [2, 1]
