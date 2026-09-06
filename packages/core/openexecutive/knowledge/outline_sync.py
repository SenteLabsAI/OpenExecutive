"""Incremental Outline → isolated wiki-collection sync.

Opt-in (`OUTLINE_SYNC_ENABLED`). Unlike Notion, Outline has no per-document
"share with integration" ACL — an API key sees everything its owning
user/service account can see. The entire access boundary is therefore
`OUTLINE_COLLECTION_IDS`: only documents whose `collectionId` is in that
configured allowlist are ever ingested, checked again per-document even
though each listing call is already server-side filtered by collectionId.

Changed documents (`updatedAt` after the per-document record / watermark)
are fetched via `documents.info`, which returns the body as Markdown
natively — unlike Notion, there is no block-tree walk or Markdown
conversion step. Markdown is written under ``<company>/docs/outline/``,
and re-indexed into the OUTLINE Chroma collection keyed by
``outline_document_id``.

That collection is separate from COMPANY: an Outline collection can be
multi-writer, so synced documents are unvetted relative to curated
uploads. The retriever labels them as such and ranks them alongside the
synced Notion wiki, below curated company documents.

Heartbeat lifecycle matches ``notion_sync`` / ``monitoring.pipeline`` /
``watchlist_research_scan``: bootstrap on boot, run one tick, chain next.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from openexecutive.config import Settings, get_settings
from openexecutive.knowledge.loader import DOMAIN_MAP, ingest_text_sync
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory.episodic import insert_scheduled_action

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "outline_sync_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "outline_sync"
HEARTBEAT_INTENT = "Outline wiki sync — incremental document ingest into isolated collection."

_MAX_DOC_CHARS = 200_000
_REQUEST_PAUSE_S = 0.35
_MAX_VISIBLE_DOCS = 2000  # safety valve for a scoped listing, not an Outline API limit
_LIST_PAGE_SIZE = 100

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UUID_HYPHEN = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_HEX = re.compile(r"(?i)^[0-9a-f]{32}$")
_DOC_ID_COMMENT = re.compile(
    r"<!--\s*outline_document_id:\s*([0-9a-fA-F-]{32,36})\s*-->"
)
# Outline's markdown renders a person mention as `@[Display Name](mention://...)`.
# The URI is an internal workspace/user id — noise for retrieval — so this is
# rewritten to plain `@Display Name` before ingest.
_MENTION_LINK = re.compile(r"@\[([^\]]+)\]\(mention://[^)]*\)")


def sanitize_outline_id(value: str) -> str | None:
    """Return a hyphenated lowercase Outline UUID, or None if unsafe."""
    raw = str(value or "").strip()
    if _UUID_HYPHEN.fullmatch(raw):
        return raw.lower()
    if _UUID_HEX.fullmatch(raw):
        h = raw.lower()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return None


def sanitize_markdown_mentions(text: str) -> str:
    """Replace Outline's ``@[Name](mention://...)`` links with plain ``@Name``."""
    return _MENTION_LINK.sub(r"@\1", text)


def _allowed_collection_ids(settings: Settings) -> set[str]:
    ids: set[str] = set()
    for raw in settings.outline_collection_ids:
        safe = sanitize_outline_id(raw)
        if safe:
            ids.add(safe)
    return ids


def _state_path() -> Path:
    settings = get_settings()
    return settings.company_profile_path.parent / "outline_sync_state.json"


def _docs_dir() -> Path:
    settings = get_settings()
    path = settings.company_profile_path.parent / "docs" / "outline"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"watermark": None, "documents": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"watermark": None, "documents": {}}
    if not isinstance(data, dict):
        return {"watermark": None, "documents": {}}
    data.setdefault("watermark", None)
    data.setdefault("documents", {})
    if not isinstance(data["documents"], dict):
        data["documents"] = {}
    return data


def reset_local_state(*, profile_path: Path | None = None) -> None:
    """Drop the on-disk documents/watermark file so the next tick re-ingests.

    Call this whenever the Outline collection is wiped (fixture load/reset,
    client-slot rebuild) — otherwise leftover document records skip every
    still-in-scope document as 'already current'.
    """
    path = (
        Path(profile_path).parent / "outline_sync_state.json"
        if profile_path is not None
        else _state_path()
    )
    path.unlink(missing_ok=True)


def reset_synced_state(store: ChromaDBStore, *, profile_path: Path | None = None) -> None:
    """Combine the two calls every vector-store wipe must make together:
    drop synced chunks from Chroma AND drop the local watermark/state file.
    Forgetting either half leaves either stale chunks or a stale watermark
    that skips re-ingesting content the wipe just removed.
    """
    store.delete_outline_docs()
    reset_local_state(profile_path=profile_path)


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _safe_title(title: str) -> str:
    # Best-effort strip of HTML-comment delimiters (a single pass — a
    # nested "<<!-- ... ---->>" title can still reconstruct a marker-shaped
    # string). Id-recovery stays correct anyway because the genuine
    # `outline_document_id` header is always written first (_ingest_doc_sync)
    # and every reader (_build_file_index, _remove_stale_doc_files) uses
    # re.search, which returns the FIRST match — that ordering, not this
    # strip, is the actual invariant a forged title marker can't beat.
    cleaned = title.replace("<!--", "").replace("-->", "").strip()
    return cleaned or "Untitled"


def infer_domain(title: str, extra: str = "") -> str:
    hay = f"{title} {extra}".lower()
    for key, domain in DOMAIN_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", hay):
            return domain
    return "general"


def slugify(title: str, doc_id: str) -> str:
    safe = sanitize_outline_id(doc_id) or "invalid"
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:60] or "document"
    short = safe.replace("-", "")[:8]
    return f"outline-{short}-{slug}.md"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _request(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Outline's API is uniformly POST/JSON-RPC style — no GET+query-param
    # endpoints the way Notion's block-children pagination uses.
    response = await client.post(url, json=json_body or {})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return {}
    return data


async def list_documents_in_scope(
    client: httpx.AsyncClient,
    base: str,
    *,
    collection_ids: set[str],
    max_docs: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (documents, truncated) across every configured collection.

    ``truncated`` is ``None`` for a complete listing, else a short
    human-readable cause — callers must treat any non-None value as "more
    documents exist that this listing did not return".

    Each returned document's ``collectionId`` is checked against
    ``collection_ids`` even though the request itself is already
    server-side filtered by collectionId: that filter is the ENTIRE access
    boundary for this integration (Outline has no per-document
    share-to-integration ACL the way Notion does), so it must never be
    trusted alone.
    """
    documents: list[dict[str, Any]] = []
    truncated: str | None = None
    # A page where every row is filtered out by the allowlist check below
    # grows `offset` without ever growing `documents` — the `remaining <= 0`
    # exit alone would then never trip, looping forever against a server
    # that returns full pages of out-of-scope rows (exactly the "don't
    # trust the filter alone" scenario this function exists to guard
    # against). Bound total requests per collection the same way Notion's
    # search pagination does, and mark the listing truncated if the cap is
    # hit without a natural exit.
    max_iters = max(2, (max_docs // _LIST_PAGE_SIZE) + 2)
    for collection_id in sorted(collection_ids):
        offset = 0
        for _ in range(max_iters):
            remaining = max_docs - len(documents)
            if remaining <= 0:
                truncated = f"listing hit the safety cap of {max_docs} document(s)"
                break
            limit = min(_LIST_PAGE_SIZE, remaining)
            data = await _request(
                client,
                f"{base}/documents.list",
                json_body={
                    "collectionId": collection_id,
                    "limit": limit,
                    "offset": offset,
                    "sort": "updatedAt",
                    "direction": "DESC",
                },
            )
            rows = data.get("data")
            if not isinstance(rows, list):
                truncated = (
                    "documents.list returned an unexpected response shape "
                    f"for collection {collection_id}"
                )
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                doc_id = sanitize_outline_id(str(item.get("id") or ""))
                if not doc_id:
                    logger.warning(
                        "outline_sync: skipping document with unsafe id %r",
                        item.get("id"),
                    )
                    continue
                if item.get("collectionId") != collection_id:
                    logger.warning(
                        "outline_sync: dropping document %s — collectionId "
                        "%r is outside the configured allowlist",
                        doc_id,
                        item.get("collectionId"),
                    )
                    continue
                documents.append(item)
            if len(rows) < limit:
                # A short page is treated as the last one rather than
                # reading Outline's `pagination` envelope (e.g. a `total`
                # or `hasMore` field) — if a server ever returns a short
                # page that ISN'T actually the last one, this misses the
                # remainder without setting `truncated`. Self-healing: a
                # missed document just stays "not yet seen" and is picked
                # up (or, if genuinely gone, reconciled away) on a later
                # tick, so this degrades quietly rather than unsafely.
                break
            offset += len(rows)
            await _sleep()
        else:
            truncated = (
                f"listing pagination for collection {collection_id} hit "
                "the iteration cap"
            )
            logger.warning(
                "outline_sync: listing pagination hit iteration cap for "
                "collection %s",
                collection_id,
            )
        if truncated is not None:
            break
    return documents, truncated


async def fetch_document_body(
    client: httpx.AsyncClient,
    base: str,
    doc_id: str,
    *,
    collection_ids: set[str],
) -> str:
    """Fetch one document's Markdown body via ``documents.info``.

    Raises on anything other than a well-formed, in-scope document —
    never silently returns "" for a malformed response shape (that would
    read as a genuinely empty document and let the caller overwrite real,
    previously-synced content with a near-empty stub) or for a document
    that moved out of the collection allowlist, or was replaced by a
    different id, in the window between the listing call and this fetch.
    The listing call already checks collectionId per document, but that
    window (several seconds per document at the default request pacing)
    is real, so the access boundary is re-checked here too rather than
    trusted from a single point.
    """
    safe_id = sanitize_outline_id(doc_id)
    if not safe_id:
        raise ValueError(f"unsafe outline document id: {doc_id!r}")
    data = await _request(client, f"{base}/documents.info", json_body={"id": safe_id})
    doc = data.get("data")
    if not isinstance(doc, dict) or not isinstance(doc.get("text"), str):
        raise ValueError(
            f"documents.info returned an unexpected response shape for {safe_id}"
        )
    returned_id = sanitize_outline_id(str(doc.get("id") or ""))
    if returned_id != safe_id:
        raise ValueError(
            f"documents.info returned id {doc.get('id')!r}, expected {safe_id}"
        )
    if doc.get("collectionId") not in collection_ids:
        raise ValueError(
            f"document {safe_id} is no longer in an allowed collection "
            f"(collectionId={doc.get('collectionId')!r})"
        )
    return doc["text"]


async def _sleep() -> None:
    await asyncio.sleep(_REQUEST_PAUSE_S)


def _doc_edited_after(doc: dict[str, Any], watermark: str | None) -> bool:
    if not watermark:
        return True
    edited = str(doc.get("updatedAt") or "")
    return bool(edited and edited > watermark)


def _doc_record(state: dict[str, Any], doc_id: str) -> dict[str, Any]:
    documents = state.get("documents")
    if not isinstance(documents, dict):
        return {}
    raw = documents.get(doc_id)
    return raw if isinstance(raw, dict) else {}


def _ingest_doc_sync(
    doc: dict[str, Any],
    markdown: str,
    store: ChromaDBStore,
) -> int:
    doc_id = sanitize_outline_id(str(doc.get("id") or ""))
    if not doc_id:
        raise ValueError(f"unsafe outline document id: {doc.get('id')!r}")
    title = _safe_title(str(doc.get("title") or ""))
    domain = infer_domain(title)
    filename = slugify(title, doc_id)
    dest = _docs_dir() / filename
    header = f"<!-- outline_document_id: {doc_id} -->\n\n# {title}\n\n"
    body_text = sanitize_markdown_mentions(markdown)
    body = (header + body_text).strip()[:_MAX_DOC_CHARS]
    dest.write_text(body + "\n", encoding="utf-8")
    _remove_stale_doc_files(doc_id, keep=dest)

    # Clear this document's own prior chunks before re-ingesting (a shrunk
    # edit must not leave orphaned trailing chunks from the previous
    # version). No COMPANY_COLLECTION cleanup here — Outline sync has
    # always ingested only into OUTLINE_COLLECTION, unlike Notion sync
    # which carries a pre-isolation-bug cleanup for legacy deployments.
    store.delete_documents(
        ChromaDBStore.OUTLINE_COLLECTION, {"outline_document_id": doc_id}
    )
    return ingest_text_sync(
        body,
        store,
        source_name=f"outline/{filename}",
        domain=domain,
        collection=ChromaDBStore.OUTLINE_COLLECTION,
        extra_metadata={
            "outline_document_id": doc_id,
            "type": "outline",
        },
    )


async def ingest_document(
    doc: dict[str, Any],
    markdown: str,
    store: ChromaDBStore,
) -> int:
    return await asyncio.to_thread(_ingest_doc_sync, doc, markdown, store)


def _safe_filename(name: str) -> str | None:
    candidate = Path(str(name)).name
    if candidate.startswith("outline-") and candidate.endswith(".md"):
        return candidate
    return None


def _build_file_index() -> dict[str, list[Path]]:
    """One directory pass: map each synced document id to its on-disk file(s).

    Reading every file head is the unavoidable cost of orphan detection;
    building the index once per operation keeps ``purge_document`` /
    ``reconcile_missing_documents`` from re-scanning the directory per document.
    """
    index: dict[str, list[Path]] = {}
    for path in _docs_dir().glob("outline-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _DOC_ID_COMMENT.search(head)
        file_id = sanitize_outline_id(found.group(1)) if found else None
        if file_id:
            index.setdefault(file_id, []).append(path)
    return index


def purge_document(
    doc_id: str,
    store: ChromaDBStore,
    state: dict[str, Any] | None = None,
    file_index: dict[str, list[Path]] | None = None,
) -> bool:
    """Remove one synced document's file, chunks, and state record.

    ``file_index`` (from :func:`_build_file_index`) avoids a per-call
    directory scan; direct callers may omit it and one is built here.
    """
    did = sanitize_outline_id(doc_id)
    if not did:
        logger.warning("outline_sync: refuse to purge unsafe document id %r", doc_id)
        return False
    meta: dict[str, Any] = {}
    documents: dict[str, Any] | None = None
    if state is not None:
        raw_documents = state.setdefault("documents", {})
        if not isinstance(raw_documents, dict):
            state["documents"] = {}
            raw_documents = state["documents"]
        documents = raw_documents
        raw = documents.get(did)
        if raw is None:
            raw = documents.get(doc_id)
        if isinstance(raw, dict):
            meta = raw
    filename = _safe_filename(str(meta.get("filename") or ""))
    docs = _docs_dir()
    store.delete_documents(ChromaDBStore.OUTLINE_COLLECTION, {"outline_document_id": did})
    if filename:
        (docs / filename).unlink(missing_ok=True)
    index = file_index if file_index is not None else _build_file_index()
    for path in index.get(did, []):
        path.unlink(missing_ok=True)
    if documents is not None:
        documents.pop(did, None)
        documents.pop(doc_id, None)
    logger.info("outline_sync: purged document %s", did)
    return True


def reconcile_missing_documents(
    visible_ids: set[str],
    store: ChromaDBStore,
    state: dict[str, Any],
) -> int:
    """Purge documents (and orphan files) no longer visible in scope.

    A document counts as "no longer visible" when it's absent from the
    current scoped listing, archived, deleted, or moved outside the
    configured collection allowlist — the caller (`_fetch_tick`) excludes
    all of those from ``visible_ids`` before this runs.

    Blocking file and Chroma I/O throughout — callers on the event loop
    must run this via ``asyncio.to_thread`` (the sync tick does).
    """
    documents = state.setdefault("documents", {})
    if not isinstance(documents, dict):
        state["documents"] = {}
        documents = state["documents"]
    file_index = _build_file_index()
    stale = [
        did for did in list(documents) if sanitize_outline_id(str(did)) not in visible_ids
    ]
    purged = 0
    purged_ids: set[str] = set()
    for did in stale:
        if purge_document(str(did), store, state, file_index=file_index):
            purged += 1
            safe = sanitize_outline_id(str(did))
            if safe:
                purged_ids.add(safe)
    # Orphan files: on disk with a document-id comment but absent from state.
    for file_id in list(file_index):
        if (
            file_id not in visible_ids
            and file_id not in purged_ids
            and purge_document(file_id, store, state, file_index=file_index)
        ):
            purged += 1
    return purged


def _remove_stale_doc_files(doc_id: str, keep: Path) -> None:
    # A title edit changes slugify()'s output, so a re-synced document can
    # land at a new filename — without this sweep the old file (same
    # outline_document_id, stale name) would linger on disk forever.
    keep_resolved = keep.resolve()
    for path in _docs_dir().glob("outline-*.md"):
        try:
            if path.resolve() == keep_resolved:
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _DOC_ID_COMMENT.search(head)
        if found and sanitize_outline_id(found.group(1)) == doc_id:
            path.unlink(missing_ok=True)


@dataclass
class _TickFetch:
    """Everything the network phase of a tick learned, ready to apply locally."""

    visible_ids: set[str] = field(default_factory=set)
    truncated: str | None = None
    # (doc, doc_id, updated_at, markdown) per successfully fetched document.
    fetched: list[tuple[dict[str, Any], str, str, str]] = field(default_factory=list)
    # updatedAt times of documents left unsynced (fetch failure / cap overflow),
    # which pin the watermark below them.
    unresolved_times: list[str] = field(default_factory=list)


_BLANK_LISTING_TRUST_AFTER_SKIPS = 3


def _note_reconcile_skip(state: dict[str, Any], cause: str) -> int:
    """Record and log one more consecutive tick without reconciliation.

    Returns the new streak length so callers can decide whether a
    persistent condition should change how it's handled.
    """
    raw = state.get("reconcile_skips")
    skips = (raw if isinstance(raw, int) else 0) + 1
    state["reconcile_skips"] = skips
    logger.warning(
        "outline_sync: skipping reconciliation — %s "
        "(%d consecutive skip(s); revoked/archived documents are not "
        "purged until reconciliation runs)",
        cause,
        skips,
    )
    return skips


async def _reconcile_or_skip(
    *,
    store: ChromaDBStore,
    fetch: _TickFetch,
    state: dict[str, Any],
    stats: dict[str, int],
) -> None:
    """Decide whether this tick's listing is trustworthy enough to reconcile.

    A truncated/malformed listing tells us nothing reliable about what's
    actually visible, so it always skips (no escalation — a broken
    response must never be trusted just because it repeats). A
    well-formed but EMPTY listing while documents are on record is
    different: it's presumed transient once, but the same condition
    persisting across several ticks is far more likely to reflect a real,
    legitimate change (the allowlist was narrowed, or every allowlisted
    collection is now empty) than a recurring glitch — without this
    escalation that state is otherwise stuck forever, since reconciliation
    is the only thing that purges revoked documents.
    """
    known = state.get("documents") if isinstance(state.get("documents"), dict) else {}

    if fetch.truncated:
        _note_reconcile_skip(
            state,
            f"document listing incomplete ({fetch.truncated}), so unseen "
            "documents must not be purged as missing",
        )
        # `blank_listing_skips` must count ONLY an uninterrupted run of
        # well-formed empty listings — a truncated/malformed tick tells us
        # nothing reliable and must never pre-charge that escalation (a
        # deployment that hits the _MAX_VISIBLE_DOCS safety cap on every
        # tick, e.g. any wiki with >2000 in-scope documents, would
        # otherwise arrive truncated-but-primed, and the very first
        # genuinely empty listing — one tick, not three — would trigger a
        # full mass purge). Reset it here, separately from the general
        # `reconcile_skips` counter above.
        state["blank_listing_skips"] = 0
        return

    if stats.get("seen", 0) == 0 and known:
        # Checks `stats["seen"]` (every document that passed the
        # collectionId allowlist check during listing), not
        # `fetch.visible_ids` — an archived/deleted document IS counted in
        # `seen` but deliberately excluded from `visible_ids`, and that
        # exclusion must still be allowed to purge it below.
        cause = (
            f"listing returned 0 documents while {len(known)} are on record "
            "(refusing a mass purge on a blank listing)"
        )
        _note_reconcile_skip(state, cause)
        raw_blank_skips = state.get("blank_listing_skips")
        blank_skips = (raw_blank_skips if isinstance(raw_blank_skips, int) else 0) + 1
        state["blank_listing_skips"] = blank_skips
        if blank_skips < _BLANK_LISTING_TRUST_AFTER_SKIPS:
            return
        logger.warning(
            "outline_sync: blank listing persisted for %d consecutive "
            "well-formed ticks — trusting it and reconciling so a "
            "legitimate revocation isn't stuck forever",
            blank_skips,
        )

    # File-head scanning plus Chroma deletes — keep it off the event loop
    # so a large or bulk-rescoped wiki cannot stall the API.
    stats["purged"] = await asyncio.to_thread(
        reconcile_missing_documents, fetch.visible_ids, store, state
    )
    state["reconcile_skips"] = 0
    state["blank_listing_skips"] = 0


async def _fetch_tick(
    *,
    client: httpx.AsyncClient,
    base: str,
    settings: Settings,
    collection_ids: set[str],
    state: dict[str, Any],
    reconcile_only: bool,
    stats: dict[str, int],
) -> _TickFetch:
    """Network phase: list in-scope documents and download dirty document bodies.

    Reads ``state`` only to decide which documents are already current; all
    state mutation happens later in :func:`_apply_tick`.
    """
    fetch = _TickFetch()
    documents, fetch.truncated = await list_documents_in_scope(
        client, base, collection_ids=collection_ids, max_docs=_MAX_VISIBLE_DOCS
    )
    stats["seen"] = len(documents)

    dirty: list[dict[str, Any]] = []
    for doc in documents:
        doc_id = sanitize_outline_id(str(doc.get("id") or ""))
        if not doc_id:
            stats["failed"] += 1
            continue
        if doc.get("archivedAt") or doc.get("deletedAt"):
            # Excluded from visible_ids so reconciliation purges it below,
            # exactly like a document the listing no longer returns at all.
            continue
        fetch.visible_ids.add(doc_id)
        edited = str(doc.get("updatedAt") or "")
        recorded = _doc_record(state, doc_id)
        if recorded.get("last_edited") == edited:
            stats["skipped"] += 1
            continue
        dirty.append(doc)

    if reconcile_only:
        return fetch

    ingest_cap = max(0, settings.outline_max_docs_per_scan)
    dirty.sort(
        key=lambda d: str(d.get("updatedAt") or ""),
        reverse=True,
    )
    overflow = dirty[ingest_cap:]
    to_ingest = dirty[:ingest_cap]
    if overflow:
        stats["capped"] = len(overflow)
        logger.warning(
            "outline_sync: %d document(s) need ingest, cap is %d — "
            "overflow will retry next tick (watermark not advanced past them)",
            len(dirty),
            ingest_cap,
        )
    fetch.unresolved_times = [
        str(d.get("updatedAt") or "")
        for d in overflow
        if d.get("updatedAt")
    ]
    for doc in to_ingest:
        doc_id = sanitize_outline_id(str(doc.get("id") or ""))
        if not doc_id:
            stats["failed"] += 1
            continue
        edited = str(doc.get("updatedAt") or "")
        try:
            await _sleep()
            body = await fetch_document_body(
                client, base, doc_id, collection_ids=collection_ids
            )
        except Exception:
            stats["failed"] += 1
            if edited:
                fetch.unresolved_times.append(edited)
            logger.exception("outline_sync: failed to fetch document %s", doc_id)
            continue
        fetch.fetched.append((doc, doc_id, edited, body))
    return fetch


async def _apply_tick(
    *,
    store: ChromaDBStore,
    fetch: _TickFetch,
    now: datetime | None,
    reconcile_only: bool,
    stats: dict[str, int],
) -> None:
    """Local write phase — the caller must hold ``_FIXTURE_OP_LOCK``.

    Nothing here touches the network: it is bounded file, state, and
    Chroma work, so the lock hold stays short even for a large wiki.
    """
    # Reload instead of reusing the pre-fetch snapshot: a fixture load or
    # reset may have replaced the state file while the network phase ran,
    # and saving a stale snapshot would resurrect purged document records.
    state = load_state()
    watermark = state.get("watermark") if isinstance(state.get("watermark"), str) else None

    await _reconcile_or_skip(store=store, fetch=fetch, state=state, stats=stats)

    if not reconcile_only:
        unresolved_times = list(fetch.unresolved_times)
        for doc, doc_id, edited, markdown in fetch.fetched:
            try:
                chunks = await ingest_document(doc, markdown, store)
                title = _safe_title(str(doc.get("title") or ""))
                filename = slugify(title, doc_id)
                state.setdefault("documents", {})[doc_id] = {
                    "last_edited": edited,
                    "title": title,
                    "filename": filename,
                }
                stats["updated"] += 1
                logger.info(
                    "outline_sync: indexed %s (%d chunks)", title, chunks
                )
            except Exception:
                stats["failed"] += 1
                if edited:
                    unresolved_times.append(edited)
                logger.exception("outline_sync: failed document %s", doc_id)

        if unresolved_times:
            # Leave watermark strictly below the earliest unresolved edit
            # so those documents stay eligible. Successfully recorded
            # documents are skipped via the documents dict, not the watermark.
            logger.info(
                "outline_sync: watermark held at %s (%d unresolved document(s))",
                watermark,
                len(unresolved_times),
            )
        else:
            recorded_times = [
                str(rec.get("last_edited") or "")
                for rec in (state.get("documents") or {}).values()
                if isinstance(rec, dict) and rec.get("last_edited")
            ]
            if recorded_times:
                candidate = max(recorded_times)
                if watermark is None or candidate > watermark:
                    state["watermark"] = candidate

    state["last_run"] = (now or datetime.now(UTC)).isoformat()
    save_state(state)


async def run_outline_sync(
    *,
    store: ChromaDBStore | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    reconcile_only: bool = False,
) -> dict[str, int]:
    """One sync tick. Returns counts: seen / updated / skipped / failed / purged / capped."""
    settings = get_settings()
    stats = {
        "seen": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "purged": 0,
        "capped": 0,
    }
    if not settings.outline_sync_enabled:
        return stats
    api_key = settings.outline_api_key
    base_url = settings.outline_base_url
    collection_ids = _allowed_collection_ids(settings)
    if not api_key or not base_url or not collection_ids:
        logger.warning(
            "outline_sync: enabled but OUTLINE_API_KEY/OUTLINE_BASE_URL/"
            "OUTLINE_COLLECTION_IDS is incomplete — skipping"
        )
        return stats
    base = base_url.rstrip("/")
    if not base.startswith("https://"):
        logger.warning(
            "outline_sync: OUTLINE_BASE_URL does not start with https:// — "
            "the API key is sent in cleartext. Use https:// unless this is "
            "a trusted internal network."
        )

    # Same concurrency posture as notion_sync: a tick must not interleave
    # with a fixture load / client-slot rotation (those swap the active
    # client's docs/collections/state file wholesale). Skip when the lock
    # is held, fetch from Outline unlocked, and take the lock only for the
    # bounded local write phase — see notion_sync.py for the full rationale
    # (this shape survived an adversarial security review there).
    from openexecutive.cli.fixture_loader import _FIXTURE_OP_LOCK
    from openexecutive.clients.slots import get_active_client

    if _FIXTURE_OP_LOCK.locked():
        logger.info(
            "outline_sync: fixture/rotation operation in progress — "
            "skipping this tick, will retry on the next interval"
        )
        return stats

    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    generation = get_active_client(settings)
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(headers=_headers(api_key), timeout=60.0)
    try:
        fetch = await _fetch_tick(
            client=client,
            base=base,
            settings=settings,
            collection_ids=collection_ids,
            state=load_state(),
            reconcile_only=reconcile_only,
            stats=stats,
        )
        async with _FIXTURE_OP_LOCK:
            if get_active_client(settings) != generation:
                logger.warning(
                    "outline_sync: active client changed while fetching — "
                    "discarding this tick's results"
                )
                return stats
            await _apply_tick(
                store=store,
                fetch=fetch,
                now=now,
                reconcile_only=reconcile_only,
                stats=stats,
            )
    finally:
        if own_client:
            await client.aclose()

    logger.info("outline_sync: %s", stats)
    return stats


def purge_all_synced(store: ChromaDBStore, state: dict[str, Any] | None = None) -> int:
    """Remove every locally synced Outline document (files + chunks + state)."""
    current = state if state is not None else load_state()
    raw_documents = current.get("documents")
    documents = raw_documents if isinstance(raw_documents, dict) else {}
    ids = list(documents)
    file_index = _build_file_index()
    purged = 0
    purged_ids: set[str] = set()
    for did in ids:
        if purge_document(str(did), store, current, file_index=file_index):
            purged += 1
            safe = sanitize_outline_id(str(did))
            if safe:
                purged_ids.add(safe)
    for file_id in file_index:
        if file_id not in purged_ids and purge_document(
            file_id, store, current, file_index=file_index
        ):
            purged += 1
    indexed_paths = {p for paths in file_index.values() for p in paths}
    for path in _docs_dir().glob("outline-*.md"):
        # Files with no readable document-id comment are junk — remove them.
        if path not in indexed_paths:
            path.unlink(missing_ok=True)
            purged += 1
    store.delete_outline_docs()
    current["documents"] = {}
    current["watermark"] = None
    save_state(current)
    return purged


def _heartbeat_pending(db_path: Path | None = None) -> bool:
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE kind = ? AND status IN ('pending', 'running') LIMIT 1",
            (HEARTBEAT_KIND,),
        ).fetchone()
    return row is not None


def bootstrap_outline_sync_scan(db_path: Path | None = None) -> int | None:
    if _heartbeat_pending(db_path):
        return None
    run_at = datetime.now(UTC) + timedelta(minutes=1)
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
        logger.info(
            "outline_sync.bootstrap: heartbeat at %s (id=%d)",
            run_at.isoformat(),
            action_id,
        )
        return action_id
    except Exception:
        logger.exception("outline_sync.bootstrap: failed to enqueue heartbeat")
        return None


def enqueue_next_outline_sync_scan(
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    settings = get_settings()
    base = (after or datetime.now(UTC)).astimezone(UTC)
    run_at = base + timedelta(minutes=settings.outline_sync_interval_minutes)
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
        logger.info(
            "outline_sync.enqueue_next: next scan at %s (id=%d)",
            run_at.isoformat(),
            action_id,
        )
        return action_id
    except Exception:
        logger.exception("outline_sync.enqueue_next: insert failed")
        return None
