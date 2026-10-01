"""Incremental Notion → isolated wiki-collection sync.

Opt-in (`NOTION_SYNC_ENABLED`). Only pages shared with the Notion
internal integration are visible — that *is* the ACL. Changed pages
(last_edited_time after the per-page record / watermark) are converted
to Markdown, written under ``<company>/docs/notion/``, and re-indexed
into the NOTION Chroma collection keyed by ``notion_page_id``.

That collection is separate from COMPANY: a Notion workspace is
multi-writer, so synced pages are unvetted relative to curated uploads.
The retriever labels them as such and ranks them below company docs.

Heartbeat lifecycle matches ``monitoring.pipeline`` /
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

HEARTBEAT_KIND = "notion_sync_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "notion_sync"
HEARTBEAT_INTENT = "Notion wiki sync — incremental page ingest into isolated collection."

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
_MAX_PAGE_CHARS = 200_000
_REQUEST_PAUSE_S = 0.35
MAX_BLOCK_CHILD_PAGES = 20  # 20 × 100-block pages per node — Notion page_size max
_MAX_VISIBLE_PAGES = 2000  # safety valve for reconcile listing, not a Notion API limit
_MAX_CHILD_REQUESTS_PER_PAGE = 80  # hard cap on /blocks/{id}/children calls per page
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_.~+=-]{1,1024}$")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UUID_HYPHEN = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_HEX = re.compile(r"(?i)^[0-9a-f]{32}$")
_PAGE_ID_COMMENT = re.compile(
    r"<!--\s*notion_page_id:\s*([0-9a-fA-F-]{32,36})\s*-->"
)
_SILENT_EMPTY_TYPES = {
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "toggle",
    "callout",
    "code",
    "divider",
}


def sanitize_notion_id(value: str) -> str | None:
    """Return a hyphenated lowercase Notion UUID, or None if unsafe."""
    raw = str(value or "").strip()
    if _UUID_HYPHEN.fullmatch(raw):
        return raw.lower()
    if _UUID_HEX.fullmatch(raw):
        h = raw.lower()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return None


def sanitize_cursor(value: str) -> str | None:
    raw = str(value or "").strip()
    if _CURSOR_RE.fullmatch(raw):
        return raw
    return None


def _state_path() -> Path:
    settings = get_settings()
    return settings.company_profile_path.parent / "notion_sync_state.json"


def _docs_dir() -> Path:
    settings = get_settings()
    path = settings.company_profile_path.parent / "docs" / "notion"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"watermark": None, "pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"watermark": None, "pages": {}}
    if not isinstance(data, dict):
        return {"watermark": None, "pages": {}}
    data.setdefault("watermark", None)
    data.setdefault("pages", {})
    if not isinstance(data["pages"], dict):
        data["pages"] = {}
    return data


def reset_local_state(*, profile_path: Path | None = None) -> None:
    """Drop the on-disk pages/watermark file so the next tick re-ingests.

    Call this whenever the Notion collection is wiped (fixture load/reset,
    client-slot rebuild) — otherwise leftover page records skip every
    still-shared page as 'already current'.
    """
    path = (
        Path(profile_path).parent / "notion_sync_state.json"
        if profile_path is not None
        else _state_path()
    )
    path.unlink(missing_ok=True)


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def rich_text_to_plain(rich: Any) -> str:
    if not isinstance(rich, list):
        return ""
    parts: list[str] = []
    for span in rich:
        if isinstance(span, dict):
            parts.append(str(span.get("plain_text") or ""))
    return "".join(parts)


def page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    if isinstance(props, dict):
        for value in props.values():
            if isinstance(value, dict) and value.get("type") == "title":
                title = rich_text_to_plain(value.get("title") or [])
                cleaned = _safe_title(title)
                if cleaned:
                    return cleaned
    raw = page.get("title")
    if isinstance(raw, list):
        cleaned = _safe_title(rich_text_to_plain(raw))
        if cleaned:
            return cleaned
    return "Untitled"


def _safe_title(title: str) -> str:
    return title.replace("<!--", "").replace("-->", "").strip()


def infer_domain(title: str, extra: str = "") -> str:
    hay = f"{title} {extra}".lower()
    for key, domain in DOMAIN_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", hay):
            return domain
    return "general"


def slugify(title: str, page_id: str) -> str:
    safe = sanitize_notion_id(page_id) or "invalid"
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:60] or "page"
    short = safe.replace("-", "")[:8]
    return f"notion-{short}-{slug}.md"


def block_to_markdown(block: dict[str, Any], list_index: int | None = None) -> str:
    btype = str(block.get("type") or "")
    raw_body = block.get(btype)
    body = raw_body if isinstance(raw_body, dict) else {}
    text = rich_text_to_plain(body.get("rich_text") or body.get("text") or [])
    if btype == "heading_1":
        return f"# {text}" if text else ""
    if btype == "heading_2":
        return f"## {text}" if text else ""
    if btype == "heading_3":
        return f"### {text}" if text else ""
    if btype == "bulleted_list_item":
        return f"- {text}" if text else ""
    if btype == "numbered_list_item":
        n = list_index if list_index and list_index > 0 else 1
        return f"{n}. {text}" if text else ""
    if btype == "to_do":
        mark = "x" if body.get("checked") else " "
        return f"- [{mark}] {text}" if text else ""
    if btype == "quote":
        return f"> {text}" if text else ""
    if btype == "code":
        lang = str(body.get("language") or "")
        return f"```{lang}\n{text}\n```" if text else ""
    if btype == "divider":
        return "---"
    if btype == "table_row":
        cells = body.get("cells") or []
        texts = [rich_text_to_plain(c) if isinstance(c, list) else "" for c in cells]
        return "| " + " | ".join(texts) + " |" if texts else ""
    if btype in {"paragraph", "callout", "toggle"}:
        return text
    return text


def table_to_markdown(row_blocks: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for row in row_blocks:
        raw_row = row.get("table_row")
        body = raw_row if isinstance(raw_row, dict) else {}
        cells = body.get("cells") or []
        rows.append(
            [rich_text_to_plain(c) if isinstance(c, list) else "" for c in cells]
        )
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def _fmt(r: list[str]) -> str:
        padded = r + [""] * (width - len(r))
        return "| " + " | ".join(padded) + " |"

    lines = [_fmt(rows[0])]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    lines.extend(_fmt(r) for r in rows[1:])
    return "\n".join(lines)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.request(method, url, json=json_body, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return {}
    return data


async def list_shared_pages(
    client: httpx.AsyncClient,
    *,
    max_pages: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (pages, truncated).

    ``truncated`` is ``None`` for a complete listing, else a short
    human-readable cause — callers must treat any non-None value as "more
    pages exist that this listing did not return".
    """
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated: str | None = None
    data: dict[str, Any] = {}
    max_iters = max(2, (max_pages // 100) + 2)
    for _ in range(max_iters):
        if len(pages) >= max_pages:
            break
        body: dict[str, Any] = {
            "page_size": min(100, max_pages - len(pages)),
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        if cursor:
            safe_cursor = sanitize_cursor(cursor)
            if not safe_cursor:
                logger.warning("notion_sync: dropping unsafe search cursor")
                truncated = "unsafe pagination cursor dropped"
                break
            body["start_cursor"] = safe_cursor
        data = await _request(client, "POST", f"{NOTION_API}/search", json_body=body)
        for item in data.get("results") or []:
            if isinstance(item, dict) and item.get("object") == "page":
                pages.append(item)
                if len(pages) >= max_pages:
                    break
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            # has_more with no cursor: the listing is incomplete but cannot
            # be continued. Mark it truncated so reconciliation is skipped —
            # otherwise still-shared pages beyond this point would be purged
            # as "missing".
            truncated = "Notion returned has_more with no next_cursor"
            break
        if len(pages) >= max_pages:
            truncated = f"listing hit the safety cap of {max_pages} pages"
            break
        await _sleep()
    else:
        truncated = "search pagination hit the iteration cap"
        logger.warning("notion_sync: search pagination hit iteration cap")
    if data.get("has_more") and len(pages) >= max_pages and truncated is None:
        truncated = f"listing hit the safety cap of {max_pages} pages"
    return pages, truncated


async def _list_child_blocks(
    client: httpx.AsyncClient,
    block_id: str,
    *,
    request_budget: list[int],
) -> list[dict[str, Any]]:
    safe_id = sanitize_notion_id(block_id)
    if not safe_id:
        logger.warning("notion_sync: refusing unsafe block id %r", block_id)
        return []
    blocks: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(MAX_BLOCK_CHILD_PAGES):
        if request_budget[0] <= 0:
            logger.warning("notion_sync: per-page request budget exhausted on %s", safe_id)
            return blocks
        request_budget[0] -= 1
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            safe_cursor = sanitize_cursor(str(cursor))
            if not safe_cursor:
                logger.warning("notion_sync: dropping unsafe start_cursor")
                break
            params["start_cursor"] = safe_cursor
        data = await _request(
            client,
            "GET",
            f"{NOTION_API}/blocks/{safe_id}/children",
            params=params,
        )
        for block in data.get("results") or []:
            if isinstance(block, dict):
                blocks.append(block)
        if not data.get("has_more"):
            return blocks
        cursor = data.get("next_cursor")
        if not cursor:
            return blocks
        await _sleep()
    logger.warning(
        "notion_sync: block children pagination hit cap %d on %s",
        MAX_BLOCK_CHILD_PAGES,
        safe_id,
    )
    return blocks


async def fetch_block_children(
    client: httpx.AsyncClient,
    block_id: str,
    *,
    depth: int = 0,
    request_budget: list[int] | None = None,
) -> list[str]:
    if depth > 8:
        return []
    if not sanitize_notion_id(block_id):
        logger.warning("notion_sync: refusing unsafe block id %r", block_id)
        return []
    budget = request_budget if request_budget is not None else [_MAX_CHILD_REQUESTS_PER_PAGE]
    if budget[0] <= 0:
        return []
    lines: list[str] = []
    dropped: set[str] = set()
    list_index = 0
    raw_blocks = await _list_child_blocks(client, block_id, request_budget=budget)
    for block in raw_blocks:
        btype = str(block.get("type") or "")
        if btype == "numbered_list_item":
            list_index += 1
            line = block_to_markdown(block, list_index=list_index)
        else:
            if btype == "table":
                child_id = str(block.get("id") or "")
                row_blocks: list[dict[str, Any]] = []
                if block.get("has_children") and sanitize_notion_id(child_id):
                    await _sleep()
                    row_blocks = await _list_child_blocks(
                        client, child_id, request_budget=budget
                    )
                line = table_to_markdown(row_blocks)
                if not line:
                    dropped.add("table")
            else:
                line = block_to_markdown(block)
            # Only a block that produces visible output ends a numbered list.
            # An empty paragraph between items (common Notion editing artifact)
            # must not restart the numbering at 1.
            if line:
                list_index = 0
        if line:
            lines.append(line)
        elif btype and btype not in _SILENT_EMPTY_TYPES:
            dropped.add(btype)
        if (
            block.get("has_children")
            and btype not in {"child_page", "child_database", "table"}
        ):
            child_id = str(block.get("id") or "")
            if sanitize_notion_id(child_id):
                await _sleep()
                lines.extend(
                    await fetch_block_children(
                        client, child_id, depth=depth + 1, request_budget=budget
                    )
                )
    if dropped:
        logger.info(
            "notion_sync: dropped block types %s on %s",
            sorted(dropped),
            block_id,
        )
    return lines


async def _sleep() -> None:
    await asyncio.sleep(_REQUEST_PAUSE_S)


def _page_edited_after(page: dict[str, Any], watermark: str | None) -> bool:
    if not watermark:
        return True
    edited = str(page.get("last_edited_time") or "")
    return bool(edited and edited > watermark)


def _page_record(state: dict[str, Any], page_id: str) -> dict[str, Any]:
    pages = state.get("pages")
    if not isinstance(pages, dict):
        return {}
    raw = pages.get(page_id)
    return raw if isinstance(raw, dict) else {}


def _ingest_page_sync(
    page: dict[str, Any],
    markdown: str,
    store: ChromaDBStore,
) -> int:
    page_id = sanitize_notion_id(str(page.get("id") or ""))
    if not page_id:
        raise ValueError(f"unsafe notion page id: {page.get('id')!r}")
    title = page_title(page)
    domain = infer_domain(title)
    filename = slugify(title, page_id)
    dest = _docs_dir() / filename
    header = f"<!-- notion_page_id: {page_id} -->\n\n# {title}\n\n"
    body = (header + markdown).strip()[:_MAX_PAGE_CHARS]
    dest.write_text(body + "\n", encoding="utf-8")
    _remove_stale_page_files(page_id, keep=dest)

    store.delete_documents(
        ChromaDBStore.NOTION_COLLECTION, {"notion_page_id": page_id}
    )
    store.delete_documents(
        ChromaDBStore.COMPANY_COLLECTION, {"notion_page_id": page_id}
    )
    return ingest_text_sync(
        body,
        store,
        source_name=f"notion/{filename}",
        domain=domain,
        collection=ChromaDBStore.NOTION_COLLECTION,
        extra_metadata={
            "notion_page_id": page_id,
            "type": "notion",
        },
    )


async def ingest_page(
    page: dict[str, Any],
    markdown: str,
    store: ChromaDBStore,
) -> int:
    return await asyncio.to_thread(_ingest_page_sync, page, markdown, store)


def _safe_filename(name: str) -> str | None:
    candidate = Path(str(name)).name
    if candidate.startswith("notion-") and candidate.endswith(".md"):
        return candidate
    return None


def _build_file_index() -> dict[str, list[Path]]:
    """One directory pass: map each synced page id to its on-disk file(s).

    Reading every file head is the unavoidable cost of orphan detection;
    building the index once per operation keeps ``purge_page`` /
    ``reconcile_missing_pages`` from re-scanning the directory per page.
    """
    index: dict[str, list[Path]] = {}
    for path in _docs_dir().glob("notion-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _PAGE_ID_COMMENT.search(head)
        file_id = sanitize_notion_id(found.group(1)) if found else None
        if file_id:
            index.setdefault(file_id, []).append(path)
    return index


def purge_page(
    page_id: str,
    store: ChromaDBStore,
    state: dict[str, Any] | None = None,
    file_index: dict[str, list[Path]] | None = None,
) -> bool:
    """Remove one synced page's file, chunks, and state record.

    ``file_index`` (from :func:`_build_file_index`) avoids a per-call
    directory scan; direct callers may omit it and one is built here.
    """
    pid = sanitize_notion_id(page_id)
    if not pid:
        logger.warning("notion_sync: refuse to purge unsafe page id %r", page_id)
        return False
    meta: dict[str, Any] = {}
    pages: dict[str, Any] | None = None
    if state is not None:
        raw_pages = state.setdefault("pages", {})
        if not isinstance(raw_pages, dict):
            state["pages"] = {}
            raw_pages = state["pages"]
        pages = raw_pages
        raw = pages.get(pid)
        if raw is None:
            raw = pages.get(page_id)
        if isinstance(raw, dict):
            meta = raw
    filename = _safe_filename(str(meta.get("filename") or ""))
    docs = _docs_dir()
    store.delete_documents(ChromaDBStore.NOTION_COLLECTION, {"notion_page_id": pid})
    store.delete_documents(ChromaDBStore.COMPANY_COLLECTION, {"notion_page_id": pid})
    if filename:
        (docs / filename).unlink(missing_ok=True)
    index = file_index if file_index is not None else _build_file_index()
    for path in index.get(pid, []):
        path.unlink(missing_ok=True)
    if pages is not None:
        pages.pop(pid, None)
        pages.pop(page_id, None)
    logger.info("notion_sync: purged page %s", pid)
    return True


def reconcile_missing_pages(
    visible_ids: set[str],
    store: ChromaDBStore,
    state: dict[str, Any],
) -> int:
    """Purge pages (and orphan files) the integration no longer sees.

    Blocking file and Chroma I/O throughout — callers on the event loop
    must run this via ``asyncio.to_thread`` (the sync tick does).
    """
    pages = state.setdefault("pages", {})
    if not isinstance(pages, dict):
        state["pages"] = {}
        pages = state["pages"]
    file_index = _build_file_index()
    stale = [pid for pid in list(pages) if sanitize_notion_id(str(pid)) not in visible_ids]
    purged = 0
    purged_ids: set[str] = set()
    for pid in stale:
        if purge_page(str(pid), store, state, file_index=file_index):
            purged += 1
            safe = sanitize_notion_id(str(pid))
            if safe:
                purged_ids.add(safe)
    # Orphan files: on disk with a page-id comment but absent from state.
    for file_id in list(file_index):
        if (
            file_id not in visible_ids
            and file_id not in purged_ids
            and purge_page(file_id, store, state, file_index=file_index)
        ):
            purged += 1
    return purged


def _remove_stale_page_files(page_id: str, keep: Path) -> None:
    keep_resolved = keep.resolve()
    for path in _docs_dir().glob("notion-*.md"):
        try:
            if path.resolve() == keep_resolved:
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _PAGE_ID_COMMENT.search(head)
        if found and sanitize_notion_id(found.group(1)) == page_id:
            path.unlink(missing_ok=True)


def _clear_legacy_company_notion(store: ChromaDBStore) -> None:
    store.delete_documents(ChromaDBStore.COMPANY_COLLECTION, {"type": "notion"})


@dataclass
class _TickFetch:
    """Everything the network phase of a tick learned, ready to apply locally."""

    visible_ids: set[str] = field(default_factory=set)
    truncated: str | None = None
    # (page, page_id, last_edited, markdown) per successfully fetched page.
    fetched: list[tuple[dict[str, Any], str, str, str]] = field(default_factory=list)
    # last_edited times of pages left unsynced (fetch failure / cap overflow),
    # which pin the watermark below them.
    unresolved_times: list[str] = field(default_factory=list)


def _note_reconcile_skip(state: dict[str, Any], cause: str) -> None:
    """Record and log one more consecutive tick without reconciliation.

    Reconciliation is what purges pages whose sharing was revoked, so a
    persistent skip streak means revoked content is lingering locally —
    surface the streak length instead of logging each skip in isolation.
    """
    raw = state.get("reconcile_skips")
    skips = (raw if isinstance(raw, int) else 0) + 1
    state["reconcile_skips"] = skips
    logger.warning(
        "notion_sync: skipping reconciliation — %s "
        "(%d consecutive skip(s); revoked pages are not purged until "
        "reconciliation runs)",
        cause,
        skips,
    )


async def _fetch_tick(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    state: dict[str, Any],
    reconcile_only: bool,
    stats: dict[str, int],
) -> _TickFetch:
    """Network phase: list visible pages and download dirty page content.

    Reads ``state`` only to decide which pages are already current; all
    state mutation happens later in :func:`_apply_tick`.
    """
    fetch = _TickFetch()
    pages, fetch.truncated = await list_shared_pages(
        client, max_pages=_MAX_VISIBLE_PAGES
    )
    stats["seen"] = len(pages)

    dirty: list[dict[str, Any]] = []
    for page in pages:
        page_id = sanitize_notion_id(str(page.get("id") or ""))
        if not page_id:
            logger.warning(
                "notion_sync: skipping page with unsafe id %r", page.get("id")
            )
            stats["failed"] += 1
            continue
        fetch.visible_ids.add(page_id)
        edited = str(page.get("last_edited_time") or "")
        recorded = _page_record(state, page_id)
        if recorded.get("last_edited") == edited:
            stats["skipped"] += 1
            continue
        dirty.append(page)

    if reconcile_only:
        return fetch

    ingest_cap = max(0, settings.notion_max_pages_per_scan)
    dirty.sort(
        key=lambda p: str(p.get("last_edited_time") or ""),
        reverse=True,
    )
    overflow = dirty[ingest_cap:]
    to_ingest = dirty[:ingest_cap]
    if overflow:
        stats["capped"] = len(overflow)
        logger.warning(
            "notion_sync: %d page(s) need ingest, cap is %d — "
            "overflow will retry next tick (watermark not advanced past them)",
            len(dirty),
            ingest_cap,
        )
    fetch.unresolved_times = [
        str(p.get("last_edited_time") or "")
        for p in overflow
        if p.get("last_edited_time")
    ]
    for page in to_ingest:
        page_id = sanitize_notion_id(str(page.get("id") or ""))
        if not page_id:
            stats["failed"] += 1
            continue
        edited = str(page.get("last_edited_time") or "")
        try:
            await _sleep()
            lines = await fetch_block_children(client, page_id)
        except Exception:
            stats["failed"] += 1
            if edited:
                fetch.unresolved_times.append(edited)
            logger.exception("notion_sync: failed to fetch page %s", page_id)
            continue
        fetch.fetched.append((page, page_id, edited, "\n\n".join(lines)))
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
    # and saving a stale snapshot would resurrect purged page records.
    state = load_state()
    watermark = state.get("watermark") if isinstance(state.get("watermark"), str) else None
    await asyncio.to_thread(_clear_legacy_company_notion, store)

    known = state.get("pages") if isinstance(state.get("pages"), dict) else {}
    if fetch.truncated:
        _note_reconcile_skip(
            state,
            f"page listing incomplete ({fetch.truncated}), "
            "so unseen pages must not be purged as missing",
        )
    elif not fetch.visible_ids and known:
        _note_reconcile_skip(
            state,
            f"search returned 0 pages while {len(known)} are on record "
            "(refusing a mass purge on a blank listing)",
        )
    else:
        # File-head scanning plus Chroma deletes — keep it off the event
        # loop so a large or bulk-unshared wiki cannot stall the API.
        stats["purged"] = await asyncio.to_thread(
            reconcile_missing_pages, fetch.visible_ids, store, state
        )
        state["reconcile_skips"] = 0

    if not reconcile_only:
        synced_at = (now or datetime.now(UTC)).isoformat()
        unresolved_times = list(fetch.unresolved_times)
        for page, page_id, edited, markdown in fetch.fetched:
            try:
                chunks = await ingest_page(page, markdown, store)
                filename = slugify(page_title(page), page_id)
                record: dict[str, Any] = {
                    "last_edited": edited,
                    "title": page_title(page),
                    "filename": filename,
                    "synced_at": synced_at,
                }
                url = page.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    record["url"] = url
                state.setdefault("pages", {})[page_id] = record
                stats["updated"] += 1
                logger.info(
                    "notion_sync: indexed %s (%d chunks)", page_title(page), chunks
                )
            except Exception:
                stats["failed"] += 1
                if edited:
                    unresolved_times.append(edited)
                logger.exception("notion_sync: failed page %s", page_id)

        if unresolved_times:
            # Leave watermark strictly below the earliest unresolved edit
            # so those pages stay eligible. Successfully recorded pages
            # are skipped via the pages dict, not the watermark.
            logger.info(
                "notion_sync: watermark held at %s (%d unresolved page(s))",
                watermark,
                len(unresolved_times),
            )
        else:
            recorded_times = [
                str(rec.get("last_edited") or "")
                for rec in (state.get("pages") or {}).values()
                if isinstance(rec, dict) and rec.get("last_edited")
            ]
            if recorded_times:
                candidate = max(recorded_times)
                if watermark is None or candidate > watermark:
                    state["watermark"] = candidate

    state["last_run"] = (now or datetime.now(UTC)).isoformat()
    state.pop("last_error", None)
    save_state(state)


# One tick at a time per process — see the matching lock in drive_sync.
_RUN_LOCK = asyncio.Lock()


# When the last tick in this process ended, failed or not. ``last_run`` is
# stamped only on success and with the tick's start time, so a manual-sync
# cooldown keyed on it alone lets a failing or slow source be re-run
# back-to-back.
_last_finished_at: datetime | None = None


def is_syncing() -> bool:
    return _RUN_LOCK.locked()


def last_finished_at() -> datetime | None:
    return _last_finished_at


def _record_error(message: str) -> None:
    """Keep a short, user-facing reason the last tick failed (never a trace)."""
    try:
        state = load_state()
        state["last_error"] = message
        save_state(state)
    except Exception:
        logger.exception("notion_sync: could not record the last error")


async def run_notion_sync(
    *,
    store: ChromaDBStore | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    reconcile_only: bool = False,
) -> dict[str, int]:
    """One sync tick. Returns counts: seen / updated / skipped / failed / purged / capped
    (plus ``busy`` when another tick is already running in this process)."""
    if _RUN_LOCK.locked():
        logger.info("notion_sync: a tick is already running — skipping")
        return {"busy": 1}
    global _last_finished_at
    async with _RUN_LOCK:
        try:
            return await _run_notion_sync_locked(
                store=store, client=client, now=now, reconcile_only=reconcile_only
            )
        except Exception:
            _record_error("The last sync failed unexpectedly. Check the server logs.")
            raise
        finally:
            _last_finished_at = datetime.now(UTC)


async def _run_notion_sync_locked(
    *,
    store: ChromaDBStore | None,
    client: httpx.AsyncClient | None,
    now: datetime | None,
    reconcile_only: bool,
) -> dict[str, int]:
    settings = get_settings()
    stats = {
        "seen": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "purged": 0,
        "capped": 0,
    }
    if not settings.notion_sync_enabled:
        return stats
    api_key = settings.notion_api_key
    if not api_key:
        logger.warning("notion_sync: enabled but NOTION_API_KEY is empty — skipping")
        return stats

    # A sync tick writes the active client's docs, vector collections, and
    # sync-state file, so its writes must not interleave with an in-process
    # fixture load / client rotation — those swap that state wholesale and a
    # mid-rotation tick would write one client's wiki into another's
    # collection. Holding the destructive-op lock across the whole tick
    # would let slow Notion I/O block /fixtures/* and /clients/* (and, via
    # the rotation pause marker, the entire scheduler), so instead: skip
    # the tick when a destructive op is already running, fetch from Notion
    # unlocked, and take the lock only for the local write phase. A CLI
    # invocation is a separate process this lock cannot see — that
    # cross-process race is a known, accepted limitation.
    from openexecutive.cli.fixture_loader import _FIXTURE_OP_LOCK
    from openexecutive.clients.slots import get_active_client

    if _FIXTURE_OP_LOCK.locked():
        logger.info(
            "notion_sync: fixture/rotation operation in progress — "
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
            settings=settings,
            state=load_state(),
            reconcile_only=reconcile_only,
            stats=stats,
        )
        async with _FIXTURE_OP_LOCK:
            if get_active_client(settings) != generation:
                logger.warning(
                    "notion_sync: active client changed while fetching — "
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

    logger.info("notion_sync: %s", stats)
    return stats


def purge_all_synced(store: ChromaDBStore, state: dict[str, Any] | None = None) -> int:
    """Remove every locally synced Notion page (files + chunks + state)."""
    current = state if state is not None else load_state()
    raw_pages = current.get("pages")
    pages = raw_pages if isinstance(raw_pages, dict) else {}
    ids = list(pages)
    file_index = _build_file_index()
    purged = 0
    purged_ids: set[str] = set()
    for pid in ids:
        if purge_page(str(pid), store, current, file_index=file_index):
            purged += 1
            safe = sanitize_notion_id(str(pid))
            if safe:
                purged_ids.add(safe)
    for file_id in file_index:
        if file_id not in purged_ids and purge_page(
            file_id, store, current, file_index=file_index
        ):
            purged += 1
    indexed_paths = {p for paths in file_index.values() for p in paths}
    for path in _docs_dir().glob("notion-*.md"):
        # Files with no readable page-id comment are junk — remove them.
        if path not in indexed_paths:
            path.unlink(missing_ok=True)
            purged += 1
    store.delete_notion_docs()
    current["pages"] = {}
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


def bootstrap_notion_sync_scan(db_path: Path | None = None) -> int | None:
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
        logger.info("notion_sync.bootstrap: heartbeat at %s (id=%d)", run_at.isoformat(), action_id)
        return action_id
    except Exception:
        logger.exception("notion_sync.bootstrap: failed to enqueue heartbeat")
        return None


def enqueue_next_notion_sync_scan(
    *,
    after: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    settings = get_settings()
    base = (after or datetime.now(UTC)).astimezone(UTC)
    run_at = base + timedelta(minutes=settings.notion_sync_interval_minutes)
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
        logger.info("notion_sync.enqueue_next: next scan at %s (id=%d)", run_at.isoformat(), action_id)
        return action_id
    except Exception:
        logger.exception("notion_sync.enqueue_next: insert failed")
        return None


def list_synced_pages() -> dict[str, Any]:
    """What the knowledge page shows for Notion: one row per page on record.

    Mirrors ``drive_sync.list_synced_files``. Pages recorded before
    ``synced_at`` / ``url`` were kept per page fall back to the tick time and
    no link.
    """
    state = load_state()
    last_run = state.get("last_run") if isinstance(state.get("last_run"), str) else None
    raw_pages = state.get("pages")
    pages: list[dict[str, Any]] = []
    for pid, rec in (raw_pages if isinstance(raw_pages, dict) else {}).items():
        safe = sanitize_notion_id(str(pid))
        if not safe or not isinstance(rec, dict):
            continue
        filename = _safe_filename(str(rec.get("filename") or ""))
        url = rec.get("url")
        pages.append(
            {
                "id": safe,
                "name": str(rec.get("title") or filename or safe),
                "url": url if isinstance(url, str) and url.startswith("https://") else None,
                "modified_at": rec.get("last_edited")
                if isinstance(rec.get("last_edited"), str)
                else None,
                "synced_at": rec.get("synced_at")
                if isinstance(rec.get("synced_at"), str)
                else last_run,
                "indexed": filename is not None,
            }
        )
    pages.sort(key=lambda p: p["name"].lower())
    error = state.get("last_error")
    return {
        "last_run": last_run,
        "last_error": error if isinstance(error, str) else None,
        "files": pages,
    }


def read_synced_page(page_id: str) -> dict[str, Any] | None:
    """The stored Markdown of one synced page, or None when it is unknown.

    The path comes from the state record for a sanitized id, never from the
    caller, and must be a ``notion-*.md`` name directly under ``docs/notion/``.
    """
    safe = sanitize_notion_id(page_id)
    if not safe:
        return None
    raw_pages = load_state().get("pages")
    rec = raw_pages.get(safe) if isinstance(raw_pages, dict) else None
    if not isinstance(rec, dict):
        return None
    filename = _safe_filename(str(rec.get("filename") or ""))
    if filename is None:
        return None
    try:
        text = (_docs_dir() / filename).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    url = rec.get("url")
    return {
        "id": safe,
        "name": str(rec.get("title") or filename),
        "url": url if isinstance(url, str) and url.startswith("https://") else None,
        "content": _PAGE_ID_COMMENT.sub("", text, count=1).lstrip(),
    }
