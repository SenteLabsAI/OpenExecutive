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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from openexecutive.config import get_settings
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
) -> tuple[list[dict[str, Any]], bool]:
    """Return (pages, truncated). ``truncated`` means the safety cap hid more."""
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    truncated = False
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
                truncated = True
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
            break
        if len(pages) >= max_pages:
            truncated = True
            break
        await _sleep()
    else:
        truncated = True
        logger.warning("notion_sync: search pagination hit iteration cap")
    if data.get("has_more") and len(pages) >= max_pages:
        truncated = True
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
            list_index = 0
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


def purge_page(
    page_id: str,
    store: ChromaDBStore,
    state: dict[str, Any] | None = None,
) -> bool:
    """Remove one synced page's file, chunks, and state record."""
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
    for path in docs.glob("notion-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _PAGE_ID_COMMENT.search(head)
        if found and sanitize_notion_id(found.group(1)) == pid:
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
    """Purge pages (and orphan files) the integration no longer sees."""
    pages = state.setdefault("pages", {})
    if not isinstance(pages, dict):
        state["pages"] = {}
        pages = state["pages"]
    stale = [pid for pid in list(pages) if sanitize_notion_id(str(pid)) not in visible_ids]
    purged = 0
    for pid in stale:
        if purge_page(str(pid), store, state):
            purged += 1
    docs = _docs_dir()
    for path in docs.glob("notion-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _PAGE_ID_COMMENT.search(head)
        file_id = sanitize_notion_id(found.group(1)) if found else None
        if file_id and file_id not in visible_ids and purge_page(file_id, store, state):
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


async def run_notion_sync(
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
    if not settings.notion_sync_enabled:
        return stats
    api_key = settings.notion_api_key
    if not api_key:
        logger.warning("notion_sync: enabled but NOTION_API_KEY is empty — skipping")
        return stats

    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    state = load_state()
    watermark = state.get("watermark") if isinstance(state.get("watermark"), str) else None
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(headers=_headers(api_key), timeout=60.0)

    try:
        await asyncio.to_thread(_clear_legacy_company_notion, store)
        pages, truncated = await list_shared_pages(
            client, max_pages=_MAX_VISIBLE_PAGES
        )
        stats["seen"] = len(pages)
        if truncated:
            logger.warning(
                "notion_sync: visible page list hit safety cap %d — "
                "skipping reconciliation this tick so unseen pages are not purged",
                _MAX_VISIBLE_PAGES,
            )

        visible_ids: set[str] = set()
        dirty: list[dict[str, Any]] = []
        for page in pages:
            page_id = sanitize_notion_id(str(page.get("id") or ""))
            if not page_id:
                logger.warning(
                    "notion_sync: skipping page with unsafe id %r", page.get("id")
                )
                stats["failed"] += 1
                continue
            visible_ids.add(page_id)
            edited = str(page.get("last_edited_time") or "")
            recorded = _page_record(state, page_id)
            if recorded.get("last_edited") == edited:
                stats["skipped"] += 1
                continue
            dirty.append(page)

        known = state.get("pages") if isinstance(state.get("pages"), dict) else {}
        if truncated:
            pass
        elif not visible_ids and known:
            logger.warning(
                "notion_sync: search returned 0 pages while %d are on record — "
                "skipping reconciliation to avoid a mass purge on a blank listing",
                len(known),
            )
        else:
            stats["purged"] = reconcile_missing_pages(visible_ids, store, state)

        if not reconcile_only:
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
            unresolved_times = [
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
                    chunks = await ingest_page(page, "\n\n".join(lines), store)
                    filename = slugify(page_title(page), page_id)
                    state.setdefault("pages", {})[page_id] = {
                        "last_edited": edited,
                        "title": page_title(page),
                        "filename": filename,
                    }
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
        save_state(state)
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
    purged = 0
    for pid in ids:
        if purge_page(str(pid), store, current):
            purged += 1
    docs = _docs_dir()
    for path in docs.glob("notion-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        found = _PAGE_ID_COMMENT.search(head)
        file_id = sanitize_notion_id(found.group(1)) if found else None
        if file_id:
            if purge_page(file_id, store, current):
                purged += 1
        else:
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
