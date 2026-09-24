"""Chat + research tools: publish, find and reread the Executive's deliverables.

`draft_artifact` publishes a standalone deliverable — a memo, a one-page web
report, a Word document, a spreadsheet, or a link to something created in one
of the principal's connected apps — and surfaces it for review. Unlike
`create_alert`, it writes DIRECTLY to the alerts table (bypassing the triage
pipeline) so the authored content is never suppressed or rewritten, then
routes it to the principal so it lands in the `/today` "Needs you" queue.
Formats live in `artifact_formats.py`; every format is stored as text in
`alerts.body` (Word / Excel files are rendered at download time).

`list_artifacts` and `get_artifact` let the Executive find and reread its
own past work — drafted artifacts and workflow outputs alike — so it can
cite it, build on it, or revise it (`draft_artifact(supersedes=...)`, which
archives the prior version).

Each published artifact is also indexed into the recent-research knowledge
collection (metadata `type=artifact`), so ordinary retrieval surfaces the
Executive's earlier work without a tool call. Indexing is best-effort.

The artifact rides the existing alert -> /today -> ProposalCard path. The
`["artifact"]` topic tag is the UI discriminator (same convention as the
`external:*` tags). Registered in `_ALL_SKILL_TOOLS` / `_ALL_SKILL_HANDLERS`,
so all three tools are available in BOTH the chat tool loop and the
executive_research synthesis loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from openexecutive.orchestrator.artifact_formats import (
    ARTIFACT_FORMAT_NAMES,
    DEFAULT_FORMAT,
    ArtifactInputError,
    build_artifact,
    get_format,
)
from openexecutive.orchestrator.artifact_records import (
    ArtifactNotFound,
    MalformedArtifactId,
    list_artifacts,
    load_artifact,
    set_archived,
)

logger = logging.getLogger(__name__)

# Characters of an artifact indexed into knowledge. Retrieval returns chunks,
# so the head of a long document is what matters for recall.
_INDEX_CHARS = 20_000
_LIST_DEFAULT = 10
_LIST_MAX = 25
_LIST_SCAN = 200
_LIST_PREVIEW_CHARS = 160
_GET_DEFAULT_CHARS = 20_000
_GET_MAX_CHARS = 60_000


DRAFT_ARTIFACT_TOOL: dict[str, Any] = {
    "name": "draft_artifact",
    "description": (
        "Publish a finished deliverable: a memo, brief or teardown, a "
        "one-page web report, a Word document, a spreadsheet, or a link to "
        "something you created in one of the principal's connected apps. "
        "It lands in the principal's '/today' review queue and the "
        "Artifacts gallery with your rationale attached, and the result "
        "gives you its id and a link to share. It does NOT page or DM "
        "anyone. When the principal ASKS for a written deliverable, always "
        "publish it with this tool and reply with the link. When you draft "
        "one unprompted, reserve it for findings that clear a high interest "
        "bar — quiet is the right default. Use this (NOT create_alert) for a "
        "real deliverable, not a one-line operational signal.\n\n"
        "Pick `format`: 'markdown' (default, any prose document); 'html' (a "
        "styled, self-contained web page — inline CSS only; scripts and "
        "external resources are stripped or blocked); 'docx' (a Word file "
        "rendered from your Markdown `document`); 'xlsx' (a spreadsheet — "
        "pass `sheets`, with an optional summary in `document`); 'link' (the "
        "deliverable lives in the principal's own apps: first use "
        "search_tools to find a connected tool that can create it there — a "
        "spreadsheet, doc, page — create it with call_tool, then record it "
        "here with its https `url`, a `link_label`, and a short summary in "
        "`document`; if no connected tool can, use html, docx or xlsx "
        "instead).\n\n"
        "To revise an earlier artifact, read it with get_artifact, then "
        "publish the new version with `supersedes` set to its id; the old "
        "version is archived."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short document title — the card heading.",
            },
            "format": {
                "type": "string",
                "enum": list(ARTIFACT_FORMAT_NAMES),
                "description": "Output format. Default 'markdown'.",
            },
            "document": {
                "type": "string",
                "description": (
                    "The deliverable. Markdown for 'markdown' and 'docx' "
                    "(headings, lists and tables render), a full HTML page "
                    "or fragment for 'html', and a short Markdown summary "
                    "for 'xlsx' and 'link'. Required for markdown, html "
                    "and docx."
                ),
            },
            "sheets": {
                "type": "array",
                "description": (
                    "For 'xlsx' only: one entry per worksheet. Cells are "
                    "strings, numbers, booleans or null; text is always "
                    "written as text, never as a formula."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "Rows of cell values, in column order.",
                        },
                    },
                    "required": ["columns", "rows"],
                },
            },
            "url": {
                "type": "string",
                "description": "For 'link' only: the https URL of the item you created.",
            },
            "link_label": {
                "type": "string",
                "description": (
                    "For 'link' only: what the link opens, e.g. 'Google "
                    "Sheet', 'Notion page', 'Excel Online workbook'."
                ),
            },
            "why_interesting": {
                "type": "string",
                "description": (
                    "1-2 sentences: why this is worth the principal's "
                    "time. Shown as the 'Why this is worth your time' "
                    "block above the document."
                ),
            },
            "source_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional provenance URLs you verified. Appended as a "
                    "'Sources' footer (not added to html)."
                ),
            },
            "supersedes": {
                "type": "string",
                "description": (
                    "Optional id of the artifact this revises (e.g. "
                    "'alert:12' or 'run:ab12…'). The earlier version is "
                    "archived."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
                "description": "Attention weight in the queue. Default 'medium'.",
            },
        },
        "required": ["title", "why_interesting"],
    },
}


LIST_ARTIFACTS_TOOL: dict[str, Any] = {
    "name": "list_artifacts",
    "description": (
        "List deliverables you have already produced — drafted artifacts "
        "and workflow outputs — newest first, with id, title, format and a "
        "short preview. Use it before writing something that may already "
        "exist, or to find the artifact the principal is referring to."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional words to match against title and preview.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results (default {_LIST_DEFAULT}, max {_LIST_MAX}).",
            },
        },
    },
}


GET_ARTIFACT_TOOL: dict[str, Any] = {
    "name": "get_artifact",
    "description": (
        "Read one of your artifacts by id (from list_artifacts, a link, or "
        "the principal). Returns its content as text — Markdown for "
        "documents, tables for spreadsheets, the readable text of a web "
        "page, or the summary and URL of a link. Use it to cite, build on "
        "or revise earlier work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Artifact id, e.g. 'alert:12' or 'run:ab12…'.",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    f"Truncate the content to this many characters (default "
                    f"{_GET_DEFAULT_CHARS}, max {_GET_MAX_CHARS})."
                ),
            },
        },
        "required": ["id"],
    },
}


async def handle_draft_artifact(tool_input: dict[str, Any]) -> str:
    from openexecutive.alerts.models import AlertSeverity
    from openexecutive.alerts.store import insert_alert
    from openexecutive.audit import log_event as audit_log
    from openexecutive.people.store import find_principal_person

    title = str(tool_input.get("title", "")).strip()
    why_interesting = str(tool_input.get("why_interesting", "")).strip()
    if not title or not why_interesting:
        return _err("title and why_interesting are required")

    fmt_name = str(tool_input.get("format") or "").strip().lower() or DEFAULT_FORMAT
    try:
        built = build_artifact(fmt_name, tool_input)
    except ArtifactInputError as exc:
        return _err(str(exc))

    prior_id: str | None = None
    supersedes = str(tool_input.get("supersedes") or "").strip()
    if supersedes:
        try:
            prior_id = load_artifact(supersedes).id
        except (MalformedArtifactId, ArtifactNotFound) as exc:
            return _err(f"supersedes: {exc}")

    severity_raw = str(tool_input.get("severity") or "medium").strip().lower()
    try:
        severity = AlertSeverity(severity_raw)
    except ValueError:
        severity = AlertSeverity.MEDIUM

    principal_id: int | None = None
    try:
        principal = find_principal_person()
        principal_id = principal.id if principal else None
    except Exception:
        logger.exception("draft_artifact: principal lookup failed")

    try:
        alert_id = insert_alert(
            source="artifact",
            external_id=str(uuid.uuid4()),
            severity=severity.value,
            headline=title[:160],
            body=built.stored,
            suggested_action=why_interesting,
            topic_tags=["artifact"],
            routed_to_person_id=principal_id,
            artifact_format=fmt_name,
            artifact_url=built.url,
            artifact_link_label=built.link_label,
            supersedes_id=prior_id,
        )
    except Exception as exc:
        logger.exception("draft_artifact: insert failed")
        _audit(audit_log, False, f"draft_artifact FAILED: {title[:120]} — {exc}",
               {"error": str(exc)[:300]})
        return _err(f"insert failed: {exc}")

    artifact_id = f"alert:{alert_id}"
    if prior_id:
        _retire_superseded(prior_id)
        await unindex_artifact(prior_id)
    await index_artifact(artifact_id, title, fmt_name, built.stored)

    _audit(
        audit_log,
        True,
        f"Drafted artifact for review: {title[:160]}",
        {
            "alert_id": alert_id,
            "format": fmt_name,
            "severity": severity.value,
            "routed_to_person_id": principal_id,
            "body_chars": len(built.stored),
            "supersedes": prior_id,
        },
    )
    logger.info("draft_artifact: %s format=%s title=%r", artifact_id, fmt_name, title)
    result: dict[str, Any] = {
        "ok": True,
        "artifact_id": artifact_id,
        "alert_id": alert_id,
        "title": title,
        "format": fmt_name,
        "url": f"/artifacts/{artifact_id}",
    }
    if built.url:
        result["external_url"] = built.url
    if prior_id:
        result["superseded"] = prior_id
    return json.dumps(result)


async def handle_list_artifacts(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip().lower()
    limit = _clamp_int(tool_input.get("limit"), _LIST_DEFAULT, 1, _LIST_MAX)
    try:
        records = list_artifacts(_LIST_SCAN)
    except Exception as exc:
        logger.exception("list_artifacts failed")
        return _err(f"list failed: {exc}")

    words = query.split()
    items: list[dict[str, Any]] = []
    for rec in records:
        preview = (
            get_format(rec.format).text(rec.stored)[:_LIST_PREVIEW_CHARS]
            if rec.stored else ""
        )
        haystack = f"{rec.title} {rec.source_label} {preview}".lower()
        if words and not all(w in haystack for w in words):
            continue
        items.append({
            "id": rec.id,
            "title": rec.title,
            "format": rec.format,
            "source": rec.source_label,
            "created_at": rec.created_at,
            "preview": preview,
        })
        if len(items) >= limit:
            break
    return json.dumps({"artifacts": items, "count": len(items)})


async def handle_get_artifact(tool_input: dict[str, Any]) -> str:
    composite_id = str(tool_input.get("id") or "").strip()
    max_chars = _clamp_int(tool_input.get("max_chars"), _GET_DEFAULT_CHARS, 200, _GET_MAX_CHARS)
    try:
        rec = load_artifact(composite_id)
    except (MalformedArtifactId, ArtifactNotFound) as exc:
        return _err(str(exc))

    content = get_format(rec.format).display(rec.stored or "")
    truncated = len(content) > max_chars
    result: dict[str, Any] = {
        "id": rec.id,
        "title": rec.title,
        "format": rec.format,
        "source": rec.source_label,
        "created_at": rec.created_at,
        "archived": rec.archived_at is not None,
        "url": f"/artifacts/{rec.id}",
        "content": content[:max_chars],
        "truncated": truncated,
    }
    if rec.rationale:
        result["rationale"] = rec.rationale
    if rec.url:
        result["external_url"] = rec.url
        result["link_label"] = rec.link_label
    if rec.supersedes_id:
        result["supersedes"] = rec.supersedes_id
    return json.dumps(result)


# --------------------------------------------------------------------- #
# Knowledge indexing
# --------------------------------------------------------------------- #


def _knowledge_store() -> Any:
    """The vector store artifacts are indexed into (patched out in tests)."""
    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore

    return ChromaDBStore(persist_directory=get_settings().vector_store_path)


async def index_artifact(artifact_id: str, title: str, fmt_name: str, stored: str) -> None:
    """Index an artifact's text so retrieval can surface it. Never raises."""
    try:
        store = _knowledge_store()
        if store is None:
            return
        from openexecutive.knowledge.loader import ingest_text_sync
        from openexecutive.knowledge.store import ChromaDBStore

        text = get_format(fmt_name).display(stored)[:_INDEX_CHARS]
        await asyncio.to_thread(
            ingest_text_sync,
            f"{title}\n\n{text}",
            store,
            source_name=f"artifact_{artifact_id.replace(':', '_')}",
            collection=ChromaDBStore.RESEARCH_COLLECTION,
            extra_metadata={
                "type": "artifact",
                "artifact_id": artifact_id,
                "title": title[:160],
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    except Exception:
        logger.exception("draft_artifact: indexing %s failed", artifact_id)


async def unindex_artifact(artifact_id: str) -> None:
    """Drop an artifact's chunks from knowledge. Never raises."""
    try:
        store = _knowledge_store()
        if store is None:
            return
        from openexecutive.knowledge.store import ChromaDBStore

        await asyncio.to_thread(
            store.delete_documents,
            ChromaDBStore.RESEARCH_COLLECTION,
            {"artifact_id": artifact_id},
        )
    except Exception:
        logger.exception("draft_artifact: unindexing %s failed", artifact_id)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _retire_superseded(prior_id: str) -> None:
    """Archive the version a revision replaced and take it off `/today`.

    Archiving hides it from the gallery's default view; a draft still unread
    would otherwise keep its review card next to the new version, because
    the live queue keys on status, not `archived_at`.
    """
    from openexecutive.alerts.store import get_alert, set_status

    try:
        set_archived(prior_id, archived=True)
        kind, _, native_id = prior_id.partition(":")
        if kind == "alert":
            prior = get_alert(int(native_id))
            if prior is not None and prior.status == "unread":
                set_status(prior.id or 0, "read")
    except Exception:
        logger.exception("draft_artifact: retiring superseded %s failed", prior_id)


def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _audit(audit_log: Any, ok: bool, summary: str, details: dict[str, Any]) -> None:
    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": "draft_artifact", "ok": ok, **details},
    )


DRAFT_ARTIFACT_TOOLS: list[dict[str, Any]] = [
    DRAFT_ARTIFACT_TOOL,
    GET_ARTIFACT_TOOL,
    LIST_ARTIFACTS_TOOL,
]

DRAFT_ARTIFACT_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "draft_artifact": handle_draft_artifact,
    "get_artifact": handle_get_artifact,
    "list_artifacts": handle_list_artifacts,
}


__all__ = [
    "DRAFT_ARTIFACT_TOOL",
    "DRAFT_ARTIFACT_TOOLS",
    "DRAFT_ARTIFACT_TOOL_HANDLERS",
    "GET_ARTIFACT_TOOL",
    "LIST_ARTIFACTS_TOOL",
    "handle_draft_artifact",
    "handle_get_artifact",
    "handle_list_artifacts",
    "index_artifact",
    "unindex_artifact",
]
