from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile

from openexecutive.api.models import (
    CompanyDocContent,
    DocumentUploadResponse,
    SyncedDocContent,
)

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md", ".txt"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# The upload is copied to its staging file this many bytes at a time, so the
# process never holds a whole (up to 50 MB) document in memory.
_COPY_CHUNK_BYTES = 1024 * 1024
_ALERT_EXCERPT_BYTES = 8000


async def _stage_upload(file: UploadFile, suffix: str) -> Path:
    """Copy the upload to a temp file in pieces and return its path.

    Raises 413 as soon as it passes ``MAX_UPLOAD_BYTES``, removing the
    partial copy."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            size = 0
            while chunk := await file.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large (max 50MB)")
                tmp.write(chunk)
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
    return tmp_path


def unreadable_document_error(name: str, exc: Exception) -> HTTPException:
    """422 for a document its parser could not read, in words a person can
    act on; 503 when every parser was busy and the file was never tried.
    Only the exception's type is logged: its message can quote the file."""
    from openexecutive.knowledge.isolated import IsolatedError, ParserBusy, WorkerStopped

    kind = exc.kind if isinstance(exc, IsolatedError) else type(exc).__name__
    logger.warning("documents: could not read %r (%s)", name, kind)
    if isinstance(exc, ParserBusy):
        return HTTPException(
            status_code=503,
            detail="Documents are being read right now. Try again in a minute.",
            headers={"Retry-After": "30"},
        )
    if isinstance(exc, WorkerStopped):
        reason = "it is too large or took too long to read"
    else:
        reason = "the file may be damaged, or not the type its name says"
    return HTTPException(status_code=422, detail=f"Could not read {name}: {reason}.")


def _read_head(path: Path, size: int) -> bytes:
    with path.open("rb") as f:
        return f.read(size)


@router.post("/documents", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile,
    # `Form(...)` (not a bare default) so FastAPI reads `domain` from the
    # multipart body the UI sends. A bare `domain: str = "general"` is parsed
    # as a query param, so the form field is dropped and every upload lands
    # under "general" — invisible to domain-filtered specialist retrieval.
    domain: str = Form("general"),
    request: Request = None,  # type: ignore[assignment]
) -> DocumentUploadResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Strip directory components to prevent path traversal (e.g. "../../etc/passwd.md")
    safe_filename = Path(file.filename).name
    if not safe_filename or safe_filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    ext = Path(safe_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    # Reject an unknown domain rather than indexing under it. Specialist
    # retrieval filters on these exact values, so a typo ("finanace") would
    # return 200 and then make the document permanently unretrievable — a
    # silent, delayed failure with no way to notice it from the API.
    from openexecutive.knowledge.loader import UPLOAD_DOMAINS

    if domain not in UPLOAD_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown domain: {domain}. Allowed: {', '.join(sorted(UPLOAD_DOMAINS))}",
        )

    tmp_path = await _stage_upload(file, ext)

    try:
        from openexecutive.config import get_settings
        from openexecutive.knowledge.isolated import IsolatedError
        from openexecutive.knowledge.loader import ingest_file
        from openexecutive.knowledge.store import ChromaDBStore

        settings = get_settings()
        store = (
            request.app.state.store
            if request and hasattr(request.app.state, "store")
            else ChromaDBStore(persist_directory=settings.vector_store_path)
        )

        # `source_name` — NOT tmp_path.name. The temp file is only a staging
        # buffer so the PDF/DOCX extractors have a real path to open; the
        # document's identity is the name the user uploaded it under. Deriving
        # identity from the random temp name instead gives every re-upload
        # fresh chunk ids (so the id-keyed upsert never collides and the
        # collection grows without bound) and stores a `filename` that the
        # DELETE endpoint below can never match.
        try:
            chunks_indexed = await ingest_file(
                path=tmp_path,
                store=store,
                domain=domain,
                collection=ChromaDBStore.COMPANY_COLLECTION,
                source_name=safe_filename,
                busy_raises=True,
            )
        except IsolatedError as exc:
            # A Word/Excel parse that failed, died or ran out of time, or any
            # file every parser was too busy to try (a PDF otherwise never
            # raises: it comes back empty, with a note).
            raise unreadable_document_error(safe_filename, exc) from exc

        company_docs_dir = settings.company_profile_path.parent / "docs"
        company_docs_dir.mkdir(parents=True, exist_ok=True)
        dest = company_docs_dir / safe_filename
        await asyncio.to_thread(shutil.copyfile, tmp_path, dest)

        # Fire the proactive-alerts pipeline. Body is a best-effort excerpt
        # for triage context; PDFs/docx won't decode cleanly and that's fine —
        # the triage prompt still sees source, title, and domain.
        try:
            from openexecutive.alerts.models import AlertEvent
            from openexecutive.alerts.pipeline import schedule_evaluation

            excerpt = ""
            if ext in {".md", ".txt"}:
                head = await asyncio.to_thread(_read_head, tmp_path, _ALERT_EXCERPT_BYTES)
                excerpt = head.decode("utf-8", errors="replace")
            else:
                excerpt = f"Newly ingested {ext} document: {safe_filename} (domain: {domain})"

            schedule_evaluation(
                AlertEvent(
                    source="document",
                    external_id=safe_filename,
                    title=safe_filename,
                    body=excerpt,
                )
            )
        except Exception:
            # Never let an alert failure 500 the upload.
            import logging

            logging.getLogger(__name__).exception(
                "Failed to schedule alert evaluation for document upload"
            )

    finally:
        tmp_path.unlink(missing_ok=True)

    return DocumentUploadResponse(
        filename=safe_filename,
        chunks_indexed=chunks_indexed,
        domain=domain,
        status="indexed",
    )


def _store(request: Request | None) -> Any:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore

    if request is not None and hasattr(request.app.state, "store"):
        return request.app.state.store
    return ChromaDBStore(persist_directory=get_settings().vector_store_path)


def _upload_domains(store: Any) -> dict[str, str]:
    """filename → domain for uploaded documents, from their chunks' metadata.

    The domain is only a label on the list; a store that cannot be read
    leaves the list untagged rather than failing it.
    """
    from openexecutive.knowledge.store import ChromaDBStore

    domains: dict[str, str] = {}
    try:
        rows = store.iter_chunk_metadata(ChromaDBStore.COMPANY_COLLECTION)
    except Exception:
        logger.exception("documents: could not read upload domains")
        return domains
    for _cid, md in rows:
        filename, domain = md.get("filename"), md.get("domain")
        if isinstance(filename, str) and isinstance(domain, str):
            domains.setdefault(filename, domain)
    return domains


@router.get("/documents")
async def list_documents(request: Request = None) -> dict:  # type: ignore[assignment]
    from openexecutive.config import get_settings
    from openexecutive.knowledge.loader import list_company_docs

    settings = get_settings()
    docs_dir = settings.company_profile_path.parent / "docs"
    docs = list_company_docs(docs_dir)
    domains = await asyncio.to_thread(_upload_domains, _store(request)) if docs else {}
    return {
        "documents": [
            {**doc, "source": "upload", "domain": domains.get(doc["filename"])} for doc in docs
        ]
    }


# ---------------------------------------------------------------------------
# Connected sources (Google Drive, Notion): read-only lists, a viewer, and a
# "Sync now" that runs one tick of the same sync the scheduler runs. Files
# from these sources are managed where they live — removing one from the
# shared folder / Notion integration is what drops it from the knowledge base.
#
# Declared before ``/documents/{filename}`` so "sources", "drive" and
# "notion" are never read as an uploaded file's name.
# ---------------------------------------------------------------------------

SourceId = Literal["drive", "notion"]
_SOURCE_LABELS: dict[str, str] = {"drive": "Google Drive", "notion": "Notion"}
# A manual sync this soon after the last tick is refused: the sync is
# incremental, so a second run finds nothing new and only spends API quota.
SYNC_COOLDOWN_S = 60
# Strong references to running manual syncs (the event loop holds tasks only
# weakly, so an unreferenced one can be collected mid-run).
_SYNC_TASKS: set[asyncio.Task[Any]] = set()


def _source_module(source: str) -> Any:
    if source == "drive":
        from openexecutive.knowledge import drive_sync

        return drive_sync
    if source == "notion":
        from openexecutive.knowledge import notion_sync

        return notion_sync
    raise HTTPException(status_code=404, detail="Unknown source")


def _source_enabled(source: str) -> bool:
    from openexecutive.config import get_settings

    settings = get_settings()
    if source == "drive":
        return bool(settings.drive_sync_enabled)
    return bool(settings.notion_sync_enabled and settings.notion_api_key)


def _source_interval(source: str) -> int:
    from openexecutive.config import get_settings

    settings = get_settings()
    if source == "drive":
        return int(settings.drive_sync_interval_minutes)
    return int(settings.notion_sync_interval_minutes)


def _list_source(source: str) -> dict[str, Any]:
    module = _source_module(source)
    listing: dict[str, Any] = (
        module.list_synced_files() if source == "drive" else module.list_synced_pages()
    )
    return listing


def _source_status(source: str) -> dict[str, Any]:
    listing = _list_source(source)
    return {
        "id": source,
        "label": _SOURCE_LABELS[source],
        "enabled": _source_enabled(source),
        "syncing": bool(_source_module(source).is_syncing()),
        "last_run": listing["last_run"],
        "last_error": listing["last_error"],
        "interval_minutes": _source_interval(source),
        "file_count": sum(1 for f in listing["files"] if f["indexed"]),
    }


@router.get("/documents/sources")
async def list_sources() -> dict:
    return {
        "sources": [
            await asyncio.to_thread(_source_status, source) for source in _SOURCE_LABELS
        ]
    }


@router.post("/documents/sources/{source}/sync", status_code=202)
async def sync_source(source: SourceId) -> dict:
    module = _source_module(source)
    if not _source_enabled(source):
        raise HTTPException(status_code=409, detail=f"{_SOURCE_LABELS[source]} is not connected")
    if module.is_syncing():
        raise HTTPException(status_code=409, detail="A sync is already running")
    # The cooldown runs from the latest of the last successful tick's start
    # and the end of the last tick in this process (failed ones included).
    last_run = (await asyncio.to_thread(_list_source, source))["last_run"]
    marks: list[datetime] = []
    if last_run:
        try:
            parsed = datetime.fromisoformat(last_run)
            marks.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
        except (TypeError, ValueError):
            pass
    if (finished := module.last_finished_at()) is not None:
        marks.append(finished)
    if marks:
        elapsed = (datetime.now(UTC) - max(marks)).total_seconds()
        if 0 <= elapsed < SYNC_COOLDOWN_S:
            raise HTTPException(
                status_code=429,
                detail="Synced less than a minute ago. Try again shortly.",
                headers={"Retry-After": str(int(SYNC_COOLDOWN_S - elapsed) + 1)},
            )
    runner = module.run_drive_sync if source == "drive" else module.run_notion_sync
    task = asyncio.create_task(_run_manual_sync(source, runner))
    _SYNC_TASKS.add(task)
    task.add_done_callback(_SYNC_TASKS.discard)
    return {"source": source, "status": "started"}


async def _run_manual_sync(source: str, runner: Any) -> None:
    try:
        stats = await runner()
        logger.info("documents: manual %s sync %s", source, stats)
    except Exception:
        # The sync module has already recorded a user-facing last_error.
        logger.exception("documents: manual %s sync failed", source)


@router.get("/documents/drive")
async def list_drive_documents() -> dict:
    listing = await asyncio.to_thread(_list_source, "drive")
    return {"enabled": _source_enabled("drive"), **listing}


@router.get("/documents/drive/{file_id}", response_model=SyncedDocContent)
async def get_drive_document(file_id: str) -> SyncedDocContent:
    from openexecutive.knowledge.drive_sync import read_synced_file

    doc = await asyncio.to_thread(read_synced_file, file_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return SyncedDocContent(**doc)


@router.get("/documents/notion")
async def list_notion_documents() -> dict:
    listing = await asyncio.to_thread(_list_source, "notion")
    return {"enabled": _source_enabled("notion"), **listing}


@router.get("/documents/notion/{page_id}", response_model=SyncedDocContent)
async def get_notion_document(page_id: str) -> SyncedDocContent:
    from openexecutive.knowledge.notion_sync import read_synced_page

    doc = await asyncio.to_thread(read_synced_page, page_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return SyncedDocContent(**doc)


@router.get("/documents/{filename}", response_model=CompanyDocContent)
async def get_document(
    filename: str,
    request: Request = None,  # type: ignore[assignment]
) -> CompanyDocContent:
    # Same sanitization as delete: reject anything that isn't a bare filename
    # so a crafted path can't escape the docs directory.
    safe = Path(filename).name
    if not safe or safe.startswith(".") or safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    from openexecutive.config import get_settings
    from openexecutive.knowledge.isolated import IsolatedError
    from openexecutive.knowledge.loader import extract_text_from_file_async

    settings = get_settings()
    docs_dir = settings.company_profile_path.parent / "docs"
    path = docs_dir / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    # Show the extracted text — exactly what gets chunked into the vector store
    # and retrieved by the Executive. Works uniformly across PDF/DOCX/MD/TXT; a
    # scanned PDF shows its converted text (knowledge/pdf_reader.py).
    try:
        content = await extract_text_from_file_async(path, busy_raises=True)
    except IsolatedError as exc:
        raise unreadable_document_error(safe, exc) from exc
    if not content.strip():
        content = "_No text could be read from this document._"
    return CompanyDocContent(filename=safe, content=content)


@router.delete("/documents/{filename}")
async def delete_document(
    filename: str,
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    safe = Path(filename).name
    if not safe or safe.startswith(".") or safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    from openexecutive.config import get_settings
    from openexecutive.knowledge.store import ChromaDBStore

    settings = get_settings()
    docs_dir = settings.company_profile_path.parent / "docs"
    path = docs_dir / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    store = (
        request.app.state.store
        if request and hasattr(request.app.state, "store")
        else ChromaDBStore(persist_directory=settings.vector_store_path)
    )
    store.delete_documents(
        collection=ChromaDBStore.COMPANY_COLLECTION,
        where={"filename": safe},
    )
    path.unlink()
    return {"deleted": safe}
