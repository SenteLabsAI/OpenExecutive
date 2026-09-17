from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openexecutive.knowledge.loader import (
    BUILTIN_KNOWLEDGE_PATH,
    DOMAIN_MAP,
    FAILURES_KNOWLEDGE_PATH,
    UPLOAD_DOMAINS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge")

_VALID_FILENAME = re.compile(r"^[a-zA-Z0-9_\-]+\.md$")
_VALID_SOURCE_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")


class BuiltinFileMeta(BaseModel):
    domain: str
    filename: str
    size_bytes: int


class BuiltinFileContent(BaseModel):
    domain: str
    filename: str
    content: str


class BuiltinFileWrite(BaseModel):
    domain: str
    filename: str
    content: str


class BuiltinListResponse(BaseModel):
    files: list[BuiltinFileMeta]


class BuiltinWriteResponse(BaseModel):
    domain: str
    filename: str
    chunks_indexed: int


def _validate_domain(domain: str) -> None:
    if domain not in DOMAIN_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown domain: {domain}")


def _validate_filename(filename: str) -> None:
    if not _VALID_FILENAME.match(filename):
        raise HTTPException(
            status_code=400,
            detail="Filename must be alphanumeric with dashes or underscores and end in .md",
        )


def _resolve_path(domain: str, filename: str) -> Path:
    return BUILTIN_KNOWLEDGE_PATH / domain / filename


def _get_store(request: Request):  # type: ignore[return]
    if hasattr(request.app.state, "store"):
        return request.app.state.store
    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore

    return ChromaDBStore(persist_directory=get_settings().vector_store_path)


def _review_store():  # type: ignore[no-untyped-def]
    """ReviewStore bound to the DB every other consumer reads.

    `ReviewStore()`'s default is captured at import, so a runtime override of
    `EPISODIC_DB_PATH` (client slots, tests) left this module writing review
    rows into one database while `routes/review._store` and
    `retriever._default_review_store` read another — the gate would silently
    consult state this module never wrote.
    """
    from openexecutive.knowledge.review_store import ReviewStore
    from openexecutive.memory.episodic import DB_PATH

    return ReviewStore(db_path=DB_PATH)


def _existing_review_item(item_id: str):  # type: ignore[no-untyped-def]
    """Look up a review item without 500ing or materialising the database.

    `sqlite3.connect` creates the file, and a DB with no `review_items` table
    raises — neither should turn a legitimate upload into a server error.
    Absent review state simply means nothing is known to be trusted.
    """
    from openexecutive.memory.episodic import DB_PATH

    if not DB_PATH.exists():
        return None
    try:
        return _review_store().get_item(item_id)
    except Exception:
        logger.warning("review state unreadable during upload check", exc_info=True)
        return None


@router.get("/builtin", response_model=BuiltinListResponse)
async def list_builtin_files() -> BuiltinListResponse:
    files: list[BuiltinFileMeta] = []
    for domain in sorted(DOMAIN_MAP.keys()):
        domain_dir = BUILTIN_KNOWLEDGE_PATH / domain
        if not domain_dir.exists():
            continue
        for f in sorted(domain_dir.glob("*.md")):
            files.append(
                BuiltinFileMeta(domain=domain, filename=f.name, size_bytes=f.stat().st_size)
            )
    return BuiltinListResponse(files=files)


@router.get("/builtin/{domain}/{filename}", response_model=BuiltinFileContent)
async def get_builtin_file(domain: str, filename: str) -> BuiltinFileContent:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return BuiltinFileContent(domain=domain, filename=filename, content=path.read_text(encoding="utf-8"))


@router.post("/builtin", response_model=BuiltinWriteResponse)
async def create_builtin_file(body: BuiltinFileWrite, request: Request) -> BuiltinWriteResponse:
    _validate_domain(body.domain)
    _validate_filename(body.filename)
    path = _resolve_path(body.domain, body.filename)
    if path.exists():
        raise HTTPException(status_code=409, detail="File already exists. Use PUT to update.")

    from openexecutive.knowledge.loader import ingest_builtin_file
    from openexecutive.knowledge.review_store import ContentType, build_item_id

    # Defence in depth. Shipped failure docs used to derive ids in this very
    # namespace, so an upload of a name like `board/theranos.md` landed on a
    # row already marked `approved` + `trusted_default = 1` and inherited it —
    # trusted, unqueued, and labelled "ships with the product" in the UI. The
    # FAILURE namespace fixes that at the source; this makes sure no future
    # id-space change can quietly re-open it.
    item_id = build_item_id(ContentType.BUILTIN, body.domain, body.filename)
    existing = _existing_review_item(item_id)
    if existing is not None and existing.trusted_default:
        raise HTTPException(
            status_code=409,
            detail="That name belongs to content shipped with Open Executive.",
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")

    chunks = await ingest_builtin_file(path, _get_store(request))

    _review_store().register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=body.domain,
        filename=body.filename,
    )

    return BuiltinWriteResponse(domain=body.domain, filename=body.filename, chunks_indexed=chunks)


@router.put("/builtin/{domain}/{filename}", response_model=BuiltinWriteResponse)
async def update_builtin_file(
    domain: str, filename: str, body: BuiltinFileWrite, request: Request
) -> BuiltinWriteResponse:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found. Use POST to create.")

    from openexecutive.knowledge.loader import ingest_builtin_file
    from openexecutive.knowledge.review_store import ContentType, build_item_id
    from openexecutive.knowledge.store import ChromaDBStore

    store = _get_store(request)
    store.delete_documents(
        collection=ChromaDBStore.BUILTIN_COLLECTION,
        where={"source": str(path)},
    )
    path.write_text(body.content, encoding="utf-8")
    chunks = await ingest_builtin_file(path, store)

    rs = _review_store()
    item_id = build_item_id(ContentType.BUILTIN, domain, filename)
    rs.register(item_id=item_id, content_type=ContentType.BUILTIN, domain=domain, filename=filename)
    rs.touch_modified(item_id)

    return BuiltinWriteResponse(domain=domain, filename=filename, chunks_indexed=chunks)


# ---------------------------------------------------------------------------
# External / OER reference library (read-only)
# ---------------------------------------------------------------------------


class ExternalSourceInfo(BaseModel):
    id: str
    title: str
    publisher: str
    license: str
    phase: int
    domains: list[str]
    type: str
    url: str
    slug: str | None = None
    chunks: int
    files: int
    is_ingested: bool
    last_fetched_at: float | None = None


class ExternalSourcesResponse(BaseModel):
    sources: list[ExternalSourceInfo]
    total_chunks: int


class ExternalPeekChunk(BaseModel):
    domain: str
    filename: str
    chunk_index: int
    text: str


class ExternalPeekResponse(BaseModel):
    source_id: str
    chunks: list[ExternalPeekChunk]


def _validate_source_id(source_id: str) -> None:
    if not _VALID_SOURCE_ID.match(source_id):
        raise HTTPException(status_code=400, detail="Invalid source id")


def _cache_mtime(cache_dir: Path) -> float | None:
    """Newest mtime of any non-hidden file in the source's local cache.

    Used as a "last fetched at" indicator — that's when the on-disk artifact
    was last written, which is also when the most recent ingest read it.
    """
    if not cache_dir.exists():
        return None
    newest: float | None = None
    for p in cache_dir.rglob("*"):
        if not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(cache_dir).parts):
            continue
        m = p.stat().st_mtime
        if newest is None or m > newest:
            newest = m
    return newest


@router.get("/external", response_model=ExternalSourcesResponse)
async def list_external_sources(request: Request) -> ExternalSourcesResponse:
    """List every source declared in sources.yaml with live ingest stats."""
    from openexecutive.knowledge.external_sources import load_manifest
    from openexecutive.knowledge.store import ChromaDBStore

    manifest = load_manifest()
    store = _get_store(request)

    # Single bulk fetch is faster than per-source queries when there are 10+ sources.
    try:
        col = store._client.get_collection(ChromaDBStore.BUILTIN_COLLECTION)
        rows = col.get(include=["metadatas"])
    except Exception:
        rows = {"metadatas": []}

    chunk_counts: dict[str, int] = defaultdict(int)
    files_per_source: dict[str, set[str]] = defaultdict(set)
    for md in rows["metadatas"] or []:
        sid = md.get("source_id")
        if not sid:
            continue
        chunk_counts[sid] += 1
        if fn := md.get("filename"):
            files_per_source[sid].add(fn)

    sources: list[ExternalSourceInfo] = []
    for src in manifest:
        chunks = chunk_counts.get(src.id, 0)
        sources.append(
            ExternalSourceInfo(
                id=src.id,
                title=src.title,
                publisher=src.publisher,
                license=src.license,
                phase=src.phase,
                domains=src.domains,
                type=src.type,
                url=src.url,
                slug=src.slug,
                chunks=chunks,
                files=len(files_per_source.get(src.id, set())),
                is_ingested=chunks > 0,
                last_fetched_at=_cache_mtime(src.cache_dir),
            )
        )
    from openexecutive.knowledge.review_store import ReviewStore

    ingested = [
        {"id": s.id, "domains": s.domains}
        for s in sources
        if s.is_ingested
    ]
    if ingested:
        from openexecutive.memory.episodic import DB_PATH as _REVIEW_DB

        ReviewStore.sync_external_registrations(ingested, _REVIEW_DB)

    return ExternalSourcesResponse(sources=sources, total_chunks=sum(chunk_counts.values()))


@router.get("/external/{source_id}/peek", response_model=ExternalPeekResponse)
async def peek_external_source(
    source_id: str, request: Request, limit: int = 5
) -> ExternalPeekResponse:
    """Return the first N indexed chunks of a source so a human can spot-check them."""
    from openexecutive.knowledge.external_sources import load_manifest
    from openexecutive.knowledge.store import ChromaDBStore

    _validate_source_id(source_id)
    if not any(src.id == source_id for src in load_manifest()):
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")

    limit = max(1, min(limit, 25))
    store = _get_store(request)
    try:
        col = store._client.get_collection(ChromaDBStore.BUILTIN_COLLECTION)
        rows = col.get(
            where={"source_id": source_id},
            limit=limit,
            include=["documents", "metadatas"],
        )
    except Exception:
        rows = {"documents": [], "metadatas": []}

    chunks = [
        ExternalPeekChunk(
            domain=md.get("domain", "?"),
            filename=md.get("filename", "?"),
            chunk_index=md.get("chunk_index", 0),
            text=doc,
        )
        for doc, md in zip(rows["documents"] or [], rows["metadatas"] or [], strict=False)
    ]
    return ExternalPeekResponse(source_id=source_id, chunks=chunks)


@router.delete("/builtin/{domain}/{filename}")
async def delete_builtin_file(domain: str, filename: str, request: Request) -> dict:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    from openexecutive.knowledge.review_store import ContentType, build_item_id
    from openexecutive.knowledge.store import ChromaDBStore

    store = _get_store(request)
    store.delete_documents(
        collection=ChromaDBStore.BUILTIN_COLLECTION,
        where={"source": str(path)},
    )
    path.unlink()
    _review_store().delete_item(
        build_item_id(ContentType.BUILTIN, domain, filename)
    )
    return {"deleted": filename}


# ---------------------------------------------------------------------------
# Failures (negative learnings) — separate ChromaDB collection (failure_cases)
# but managed via the same CRUD shape as builtin playbooks. Chunked smaller
# (400 words, 40 overlap) to match seed_failures so a re-index produces the
# same artifacts whether done at seed-time or via PUT.
# ---------------------------------------------------------------------------


def _resolve_failure_path(domain: str, filename: str) -> Path:
    return FAILURES_KNOWLEDGE_PATH / domain / filename


@router.get("/failures", response_model=BuiltinListResponse)
async def list_failure_files() -> BuiltinListResponse:
    files: list[BuiltinFileMeta] = []
    for domain in sorted(DOMAIN_MAP.keys()):
        domain_dir = FAILURES_KNOWLEDGE_PATH / domain
        if not domain_dir.exists():
            continue
        for f in sorted(domain_dir.glob("*.md")):
            files.append(
                BuiltinFileMeta(domain=domain, filename=f.name, size_bytes=f.stat().st_size)
            )
    return BuiltinListResponse(files=files)


@router.get("/failures/{domain}/{filename}", response_model=BuiltinFileContent)
async def get_failure_file(domain: str, filename: str) -> BuiltinFileContent:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_failure_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return BuiltinFileContent(
        domain=domain, filename=filename, content=path.read_text(encoding="utf-8")
    )


@router.post("/failures", response_model=BuiltinWriteResponse)
async def create_failure_file(body: BuiltinFileWrite, request: Request) -> BuiltinWriteResponse:
    _validate_domain(body.domain)
    _validate_filename(body.filename)
    path = _resolve_failure_path(body.domain, body.filename)
    if path.exists():
        raise HTTPException(status_code=409, detail="File already exists. Use PUT to update.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")

    from openexecutive.knowledge.loader import ingest_builtin_file
    from openexecutive.knowledge.store import ChromaDBStore

    chunks = await ingest_builtin_file(
        path,
        _get_store(request),
        collection=ChromaDBStore.FAILURES_COLLECTION,
        chunk_type="failure_case",
        chunk_size=400,
        overlap=40,
    )

    # Register, exactly as create_builtin_file does. Without this a
    # user-authored failure case study had no review row at all, so it could
    # never enter the withheld set — `retrieve_failures` applied a gate that
    # could not reach it, and no SME decision could block it.
    from openexecutive.knowledge.review_store import ContentType, build_item_id

    _review_store().register(
        item_id=build_item_id(ContentType.FAILURE, body.domain, body.filename),
        content_type=ContentType.FAILURE,
        domain=body.domain,
        filename=body.filename,
    )

    return BuiltinWriteResponse(domain=body.domain, filename=body.filename, chunks_indexed=chunks)


@router.put("/failures/{domain}/{filename}", response_model=BuiltinWriteResponse)
async def update_failure_file(
    domain: str, filename: str, body: BuiltinFileWrite, request: Request
) -> BuiltinWriteResponse:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_failure_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found. Use POST to create.")

    from openexecutive.knowledge.loader import ingest_builtin_file
    from openexecutive.knowledge.store import ChromaDBStore

    store = _get_store(request)
    store.delete_documents(
        collection=ChromaDBStore.FAILURES_COLLECTION,
        where={"source": str(path)},
    )
    path.write_text(body.content, encoding="utf-8")
    chunks = await ingest_builtin_file(
        path,
        store,
        collection=ChromaDBStore.FAILURES_COLLECTION,
        chunk_type="failure_case",
        chunk_size=400,
        overlap=40,
    )
    # Mirror update_builtin_file: register (a doc created before failure docs
    # were registered still has no row, and editing must create one) and mark
    # it needs_revision so an edit behaves the same for both content types.
    from openexecutive.knowledge.review_store import ContentType, build_item_id

    rs = _review_store()
    item_id = build_item_id(ContentType.FAILURE, domain, filename)
    rs.register(
        item_id=item_id,
        content_type=ContentType.FAILURE,
        domain=domain,
        filename=filename,
    )
    rs.touch_modified(item_id)

    return BuiltinWriteResponse(domain=domain, filename=filename, chunks_indexed=chunks)


@router.delete("/failures/{domain}/{filename}")
async def delete_failure_file(domain: str, filename: str, request: Request) -> dict:
    _validate_domain(domain)
    _validate_filename(filename)
    path = _resolve_failure_path(domain, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    from openexecutive.knowledge.store import ChromaDBStore

    store = _get_store(request)
    store.delete_documents(
        collection=ChromaDBStore.FAILURES_COLLECTION,
        where={"source": str(path)},
    )
    path.unlink()

    from openexecutive.knowledge.review_store import ContentType, build_item_id

    _review_store().delete_item(build_item_id(ContentType.FAILURE, domain, filename))
    return {"deleted": filename}


# ---------------------------------------------------------------------------
# Search — diagnostic "what would RAG retrieve for this question" endpoint.
# Returns raw structured chunks (text/metadata/distance) per collection so
# the UI can show exactly what each specialist would see. This is a read-only
# parallel of `retrieve()` and `retrieve_failures()` — it does NOT replace
# them or change the chat path.
# ---------------------------------------------------------------------------


_VALID_SOURCE_TYPES = {"builtin", "company", "failures", "external"}

# Max characters of chunk text returned per search hit. UI shows ~600 chars,
# leaving headroom for "…" truncation indicator and tail context.
_SEARCH_SNIPPET_LIMIT = 800

# When both 'builtin' (no source_id) and 'external' (with source_id) are requested
# from BUILTIN_COLLECTION, we over-fetch from a single query and partition by
# source_id. The window must be large enough that a dominant category doesn't
# starve the other. We pull (n_builtin + n_external) * OVERFETCH_MULTIPLIER rows.
_BUILTIN_OVERFETCH_MULTIPLIER = 5

# Per-bucket result count ceiling (caller-supplied n_* values are clamped here).
_MAX_RESULTS_PER_BUCKET = 25


class KnowledgeSearchRequest(BaseModel):
    query: str
    domain_filter: list[str] | None = None
    specialist: str | None = None
    n_builtin: int = 5
    n_company: int = 3
    n_failures: int = 3
    n_external: int = 5
    include: list[str] | None = None  # subset of _VALID_SOURCE_TYPES


class SearchHit(BaseModel):
    filename: str
    domain: str
    source: str | None = None
    source_id: str | None = None
    source_url: str | None = None
    license: str | None = None
    publisher: str | None = None
    chunk_index: int | None = None
    distance: float
    text: str


class KnowledgeSearchResponse(BaseModel):
    query: str
    effective_domains: list[str] | None
    specialists_that_would_see_this: list[str]
    builtin: list[SearchHit]
    company: list[SearchHit]
    failures: list[SearchHit]
    external: list[SearchHit]


def _hits_from_chroma(
    rows: list[dict[str, object]], snippet_limit: int = _SEARCH_SNIPPET_LIMIT
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for r in rows:
        md = r.get("metadata") or {}
        if not isinstance(md, dict):
            md = {}
        text = str(r.get("text") or "")
        if len(text) > snippet_limit:
            text = text[:snippet_limit] + "…"
        chunk_index_raw = md.get("chunk_index")
        chunk_index = chunk_index_raw if isinstance(chunk_index_raw, int) else None
        hits.append(
            SearchHit(
                filename=str(md.get("filename", "unknown")),
                domain=str(md.get("domain", "?")),
                source=md.get("source") if isinstance(md.get("source"), str) else None,
                source_id=md.get("source_id") if isinstance(md.get("source_id"), str) else None,
                source_url=md.get("source_url") if isinstance(md.get("source_url"), str) else None,
                license=md.get("license") if isinstance(md.get("license"), str) else None,
                publisher=md.get("publisher") if isinstance(md.get("publisher"), str) else None,
                chunk_index=chunk_index,
                distance=float(r.get("distance", 0.0) or 0.0),  # type: ignore[arg-type]
                text=text,
            )
        )
    return hits


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    body: KnowledgeSearchRequest, request: Request
) -> KnowledgeSearchResponse:
    """Diagnostic: show what RAG would surface for a given query.

    Mirrors the per-collection queries the chat path uses but returns raw
    chunks (text/distance/metadata) instead of the formatted markdown blob
    `retrieve()` produces. Intended for the Knowledge UI's Query mode and
    for tuning the knowledge base offline.
    """
    from openexecutive.knowledge.retriever import (
        DOMAIN_ALIASES,
        _default_review_store,
        _with_general,
    )
    from openexecutive.knowledge.review_store import ContentType
    from openexecutive.knowledge.store import ChromaDBStore

    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")

    include = set(body.include) if body.include else set(_VALID_SOURCE_TYPES)
    bad = include - _VALID_SOURCE_TYPES
    if bad:
        raise HTTPException(status_code=400, detail=f"Invalid include values: {sorted(bad)}")

    # UPLOAD_DOMAINS, not DOMAIN_MAP: `general` is a real, uploadable company
    # domain, and rejecting it here made this endpoint unable to introspect the
    # documents most likely to need it — the unclassified ones.
    if body.domain_filter:
        for d in body.domain_filter:
            if d not in UPLOAD_DOMAINS:
                raise HTTPException(status_code=400, detail=f"Unknown domain: {d}")

    if body.specialist and body.specialist not in DOMAIN_ALIASES:
        raise HTTPException(status_code=400, detail=f"Unknown specialist: {body.specialist}")

    effective_domains: list[str] | None = body.domain_filter
    if effective_domains is None and body.specialist:
        effective_domains = DOMAIN_ALIASES.get(body.specialist)

    # Reverse-map: which specialists would see at least one of these domains?
    if effective_domains:
        specialists_seeing = sorted(
            name
            for name, doms in DOMAIN_ALIASES.items()
            if any(d in doms for d in effective_domains)
        )
    else:
        specialists_seeing = sorted(DOMAIN_ALIASES.keys())

    store = _get_store(request)

    n_builtin = max(1, min(body.n_builtin, _MAX_RESULTS_PER_BUCKET))
    n_company = max(1, min(body.n_company, _MAX_RESULTS_PER_BUCKET))
    n_failures = max(1, min(body.n_failures, _MAX_RESULTS_PER_BUCKET))
    n_external = max(1, min(body.n_external, _MAX_RESULTS_PER_BUCKET))

    def _query_collection(
        collection: str, n: int, domains: list[str] | None = None
    ) -> list[dict[str, object]]:
        try:
            return store.query(
                query_text=body.query,
                collection=collection,
                domain_filter=effective_domains if domains is None else domains,
                n_results=n,
            )
        except Exception:
            return []

    # Mirror `retrieve()`'s review gate. Withheld = pending or rejected; the
    # chat path drops those before a specialist ever sees them, so a panel
    # that advertises itself as showing "what RAG would surface" must drop
    # them too — otherwise it reports knowledge the Executive cannot use.
    # Resolved once per request, via the retriever's own resolver so both
    # paths read the same database. Company docs are never registered in
    # `review_items`, so the company bucket is deliberately not filtered.
    #
    # Degrades to "nothing withheld" rather than failing, matching the
    # per-collection `except` above: this is a read-only diagnostic, and a
    # broken panel helps nobody. `retrieve()` is the enforcing gate and does
    # NOT degrade. The exists() check matters on its own — `sqlite3.connect`
    # creates the file, and a diagnostic read must not materialise a database.
    def _withheld_sets() -> tuple[set[tuple[str, str]], set[str]]:
        from openexecutive.memory.episodic import DB_PATH

        if not DB_PATH.exists():
            return set(), set()
        try:
            rs = _default_review_store()
            return (
                rs.get_withheld_keys(ContentType.BUILTIN)
                | rs.get_withheld_keys(ContentType.FAILURE),
                rs.get_withheld_source_ids(),
            )
        except Exception:
            # Say so. A silently degraded mirror is worse than a loud one: an
            # SME who rejects a doc, still sees it listed here, and has no
            # signal will conclude rejection is broken — while chat is in fact
            # withholding it correctly.
            logger.warning(
                "review state unreadable; /knowledge/search is showing "
                "unfiltered results this request",
                exc_info=True,
            )
            return set(), set()

    _withheld_builtin, _withheld_external = _withheld_sets()

    def _is_withheld(row: dict[str, object]) -> bool:
        md = row.get("metadata") or {}
        md_dict = md if isinstance(md, dict) else {}
        source_id = md_dict.get("source_id")
        if source_id:
            return source_id in _withheld_external
        return (md_dict.get("domain"), md_dict.get("filename")) in _withheld_builtin

    builtin_hits: list[SearchHit] = []
    external_hits: list[SearchHit] = []
    want_builtin = "builtin" in include
    want_external = "external" in include
    if want_builtin or want_external:
        # builtin + external share BUILTIN_COLLECTION — only metadata source_id
        # distinguishes them. Single over-fetched query, then partition.
        # The window must be wide enough that a dominant category (e.g. heavy
        # OER ingest) doesn't crowd out the requested count in the other.
        wanted_builtin = n_builtin if want_builtin else 0
        wanted_external = n_external if want_external else 0
        window = (wanted_builtin + wanted_external) * _BUILTIN_OVERFETCH_MULTIPLIER
        raw = [
            r
            for r in _query_collection(ChromaDBStore.BUILTIN_COLLECTION, window)
            if not _is_withheld(r)
        ]
        for r in raw:
            md = r.get("metadata") or {}
            md_dict = md if isinstance(md, dict) else {}
            if md_dict.get("source_id"):
                if want_external and len(external_hits) < wanted_external:
                    external_hits.extend(_hits_from_chroma([r]))
            else:
                if want_builtin and len(builtin_hits) < wanted_builtin:
                    builtin_hits.extend(_hits_from_chroma([r]))
            if (
                len(builtin_hits) >= wanted_builtin
                and len(external_hits) >= wanted_external
            ):
                break

    company_hits: list[SearchHit] = []
    if "company" in include:
        # Mirror `retrieve()` exactly: it widens the COMPANY filter with the
        # `general` catch-all. Without this, the panel an operator uses to
        # debug retrieval would report zero company hits for a specialist that
        # does retrieve them in chat — showing the very #114 symptom the
        # catch-all fixes.
        company_hits = _hits_from_chroma(
            _query_collection(
                ChromaDBStore.COMPANY_COLLECTION,
                n_company,
                domains=_with_general(effective_domains),
            )
        )

    failure_hits: list[SearchHit] = []
    if "failures" in include:
        failure_hits = _hits_from_chroma(
            [
                r
                for r in _query_collection(
                    ChromaDBStore.FAILURES_COLLECTION, n_failures
                )
                if not _is_withheld(r)
            ]
        )

    return KnowledgeSearchResponse(
        query=body.query,
        effective_domains=effective_domains,
        specialists_that_would_see_this=specialists_seeing,
        builtin=builtin_hits,
        company=company_hits,
        failures=failure_hits,
        external=external_hits,
    )
