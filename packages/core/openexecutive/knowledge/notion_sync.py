"""Incremental Notion → company-docs sync.

Opt-in (`NOTION_SYNC_ENABLED`). Only pages shared with the Notion
internal integration are visible — that *is* the ACL. Changed pages
(last_edited_time after the stored watermark) are converted to Markdown,
written under ``<company>/docs/notion/``, and re-indexed into the
COMPANY Chroma collection keyed by ``notion_page_id``.

Heartbeat lifecycle matches ``monitoring.pipeline`` /
``watchlist_research_scan``: bootstrap on boot, run one tick, chain next.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from openexecutive.config import get_settings
from openexecutive.knowledge.loader import DOMAIN_MAP, ingest_text
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory.episodic import insert_scheduled_action

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "notion_sync_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "notion_sync"
HEARTBEAT_INTENT = "Notion company-docs sync — incremental page ingest."

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
_MAX_PAGE_CHARS = 200_000
_REQUEST_PAUSE_S = 0.35

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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
    return data


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
                if title.strip():
                    return title.strip()
    # Untitled / wiki pages sometimes only have a top-level title key.
    raw = page.get("title")
    if isinstance(raw, list):
        title = rich_text_to_plain(raw)
        if title.strip():
            return title.strip()
    return "Untitled"


def infer_domain(title: str, extra: str = "") -> str:
    hay = f"{title} {extra}".lower()
    for key, domain in DOMAIN_MAP.items():
        if key in hay:
            return domain
    return "general"


def slugify(title: str, page_id: str) -> str:
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:60] or "page"
    short = page_id.replace("-", "")[:8]
    return f"notion-{short}-{slug}.md"


def block_to_markdown(block: dict[str, Any]) -> str:
    btype = str(block.get("type") or "")
    body = block.get(btype) if isinstance(block.get(btype), dict) else {}
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
        return f"1. {text}" if text else ""
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
    if btype in {"paragraph", "callout", "toggle"}:
        return text
    return text


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
) -> dict[str, Any]:
    response = await client.request(method, url, json=json_body)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return {}
    return data


async def list_shared_pages(
    client: httpx.AsyncClient,
    *,
    max_pages: int,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(pages) < max_pages:
        body: dict[str, Any] = {
            "page_size": min(100, max_pages - len(pages)),
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        if cursor:
            body["start_cursor"] = cursor
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
        await _sleep()
    return pages


async def fetch_block_children(
    client: httpx.AsyncClient,
    block_id: str,
    *,
    depth: int = 0,
) -> list[str]:
    if depth > 8:
        return []
    lines: list[str] = []
    cursor: str | None = None
    while True:
        url = f"{NOTION_API}/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        data = await _request(client, "GET", url)
        for block in data.get("results") or []:
            if not isinstance(block, dict):
                continue
            line = block_to_markdown(block)
            if line:
                lines.append(line)
            if block.get("has_children") and block.get("type") not in {
                "child_page",
                "child_database",
            }:
                await _sleep()
                nested = await fetch_block_children(
                    client, str(block.get("id") or ""), depth=depth + 1
                )
                lines.extend(nested)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
        await _sleep()
    return lines


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(_REQUEST_PAUSE_S)


def _page_edited_after(page: dict[str, Any], watermark: str | None) -> bool:
    if not watermark:
        return True
    edited = str(page.get("last_edited_time") or "")
    return bool(edited and edited > watermark)


async def ingest_page(
    page: dict[str, Any],
    markdown: str,
    store: ChromaDBStore,
) -> int:
    page_id = str(page.get("id") or "")
    title = page_title(page)
    domain = infer_domain(title)
    filename = slugify(title, page_id)
    dest = _docs_dir() / filename
    header = f"# {title}\n\n<!-- notion_page_id: {page_id} -->\n\n"
    body = (header + markdown).strip()[:_MAX_PAGE_CHARS]
    dest.write_text(body + "\n", encoding="utf-8")

    store.delete_documents(
        ChromaDBStore.COMPANY_COLLECTION, {"notion_page_id": page_id}
    )
    return await ingest_text(
        body,
        store,
        source_name=f"notion/{filename}",
        domain=domain,
        extra_metadata={
            "notion_page_id": page_id,
            "type": "notion",
        },
    )


async def run_notion_sync(
    *,
    store: ChromaDBStore | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One sync tick. Returns counts: seen / updated / skipped / failed."""
    settings = get_settings()
    stats = {"seen": 0, "updated": 0, "skipped": 0, "failed": 0}
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
        client = httpx.AsyncClient(
            headers=_headers(api_key), timeout=60.0
        )

    newest = watermark
    try:
        pages = await list_shared_pages(
            client, max_pages=settings.notion_max_pages_per_scan
        )
        stats["seen"] = len(pages)
        for page in pages:
            page_id = str(page.get("id") or "")
            edited = str(page.get("last_edited_time") or "")
            if edited and (newest is None or edited > newest):
                newest = edited
            if not _page_edited_after(page, watermark):
                stats["skipped"] += 1
                continue
            try:
                await _sleep()
                lines = await fetch_block_children(client, page_id)
                chunks = await ingest_page(page, "\n\n".join(lines), store)
                stats["updated"] += 1
                logger.info(
                    "notion_sync: indexed %s (%d chunks)", page_title(page), chunks
                )
            except Exception:
                stats["failed"] += 1
                logger.exception("notion_sync: failed page %s", page_id)
        if newest:
            state["watermark"] = newest
            state["last_run"] = (now or datetime.now(UTC)).isoformat()
            save_state(state)
    finally:
        if own_client:
            await client.aclose()

    logger.info("notion_sync: %s", stats)
    return stats


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
