"""Incremental Google Drive folder → isolated collection sync.

Opt-in (``DRIVE_SYNC_ENABLED``). Reads the folders in ``DRIVE_SYNC_FOLDER_IDS``
(and their subfolders, to ``_MAX_DEPTH``) as a service account with the
``drive.readonly`` scope (``knowledge.drive_client``): only what is shared
with that account is visible, and that *is* the ACL. A file whose
``modifiedTime`` / ``md5Checksum`` changed is turned into text, written under
``<company>/docs/drive/`` and re-indexed into the DRIVE Chroma collection,
keyed by ``drive_file_id``. A file no longer listed is purged.

That collection is separate from COMPANY for the reason NOTION is: a shared
Drive folder is multi-writer, so its files are unvetted next to curated
uploads. The retriever labels them as such, ranks them below company docs,
and shows each chunk's file id and sync time so the Executive can open the
live file with its own Drive tools when the latest version matters.

Structure mirrors ``knowledge.notion_sync``: a network phase with no lock,
then a local write phase under ``_FIXTURE_OP_LOCK``; heartbeat bootstrap on
boot, one tick per run, chain the next.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from openexecutive.config import Settings, get_settings
from openexecutive.knowledge.drive_client import (
    DriveClient,
    DriveFileTooLarge,
    DriveItem,
    sanitize_drive_id,
    service_account_token_provider,
)
from openexecutive.knowledge.loader import extract_text_from_file, ingest_text_sync
from openexecutive.knowledge.notion_sync import infer_domain
from openexecutive.knowledge.store import ChromaDBStore
from openexecutive.memory.episodic import insert_scheduled_action

logger = logging.getLogger(__name__)

HEARTBEAT_KIND = "drive_sync_scan"
HEARTBEAT_CHANNEL = "__internal__"
HEARTBEAT_CHANNEL_REF = "drive_sync"
HEARTBEAT_INTENT = "Google Drive folder sync — incremental file ingest into isolated collection."

_MAX_FILE_CHARS = 200_000
_MAX_FILE_BYTES = 20 * 1024 * 1024
# A .docx / .xlsx is a zip: the download cap bounds only the compressed size,
# so the archive's declared uncompressed size is checked before parsing.
_MAX_UNZIPPED_BYTES = 100 * 1024 * 1024
# How long one file's text extraction may run before it is given up on. The
# worker thread cannot be killed, but the tick moves on and the file is
# recorded as unreadable, so it is not retried until it changes.
_EXTRACT_TIMEOUT_S = 120.0
_MAX_VISIBLE_ITEMS = 2000  # files + folders listed per tick, all folders together
_MAX_DEPTH = 4  # subfolder levels below a configured folder
_REQUEST_PAUSE_S = 0.2

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# Google-native files: the export format, and the suffix to read it as. A
# Sheet goes out as .xlsx, not CSV, because Drive's CSV export holds only the
# first sheet.
_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": (_XLSX, ".xlsx"),
}
# Stored files, downloaded and read with the knowledge loader's extractors
# (no model call: a scanned PDF with no text layer yields nothing).
_DOWNLOADS: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    _XLSX: ".xlsx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
}
_TEXT_SUFFIXES = {".txt", ".md", ".csv"}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FILE_ID_COMMENT = re.compile(r"<!--\s*drive_file_id:\s*([A-Za-z0-9_-]{1,128})\s*-->")


def is_supported(mime_type: str) -> bool:
    return mime_type in _EXPORTS or mime_type in _DOWNLOADS


def _state_path() -> Path:
    return get_settings().company_profile_path.parent / "drive_sync_state.json"


def _docs_dir() -> Path:
    path = get_settings().company_profile_path.parent / "docs" / "drive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state() -> dict[str, Any]:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    return data


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def reset_local_state(*, profile_path: Path | None = None) -> None:
    """Drop the file records so the next tick re-ingests. Call it whenever
    the Drive collection is wiped (fixture load/reset, client-slot rebuild),
    or leftover records skip every unchanged file as 'already current'."""
    path = (
        Path(profile_path).parent / "drive_sync_state.json"
        if profile_path is not None
        else _state_path()
    )
    path.unlink(missing_ok=True)


def _safe_name(name: str) -> str:
    cleaned = " ".join(name.replace("<!--", "").replace("-->", "").split())
    return cleaned[:200] or "Untitled"


def slugify(name: str, file_id: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")[:60] or "file"
    return f"drive-{file_id[:8].lower()}-{slug}.md"


def _safe_filename(name: str) -> str | None:
    candidate = Path(str(name)).name
    if candidate.startswith("drive-") and candidate.endswith(".md"):
        return candidate
    return None


def _record(state: dict[str, Any], file_id: str) -> dict[str, Any]:
    raw = state.get("files", {}).get(file_id)
    return raw if isinstance(raw, dict) else {}


def _is_current(recorded: dict[str, Any], item: DriveItem) -> bool:
    return bool(recorded) and (
        recorded.get("modified") == item.modified and recorded.get("md5", "") == item.md5
    )


async def _sleep() -> None:
    await asyncio.sleep(_REQUEST_PAUSE_S)


# ---------------------------------------------------------------------------
# Network phase
# ---------------------------------------------------------------------------


@dataclass
class _TickFetch:
    """Everything the network phase of a tick learned, ready to apply locally."""

    # Every file (not folder) seen in the synced folders, supported or not.
    visible_ids: set[str] = field(default_factory=set)
    # Why the listing is not the whole picture (reconcile must not run).
    incomplete: str | None = None
    # (item, text) per file fetched; "" when it had no readable text.
    fetched: list[tuple[DriveItem, str]] = field(default_factory=list)


async def _list_tree(
    client: DriveClient, roots: list[str], fetch: _TickFetch
) -> list[DriveItem]:
    """Every file under ``roots`` to ``_MAX_DEPTH`` levels, each once."""
    files: dict[str, DriveItem] = {}
    seen_folders: set[str] = set()
    queue: list[tuple[str, int]] = [(root, 0) for root in roots]
    listed = 0
    while queue:
        folder_id, depth = queue.pop(0)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        try:
            await _sleep()
            items, truncated = await client.list_folder(
                folder_id, max_items=_MAX_VISIBLE_ITEMS - listed + 1
            )
        except Exception as exc:
            logger.warning("drive_sync: listing folder %s failed: %s", folder_id, exc)
            fetch.incomplete = fetch.incomplete or f"folder {folder_id} could not be listed"
            continue
        listed += len(items)
        if truncated or listed > _MAX_VISIBLE_ITEMS:
            fetch.incomplete = fetch.incomplete or (
                f"more than {_MAX_VISIBLE_ITEMS} items across the synced folders"
            )
        for item in items:
            if item.is_folder:
                # Deeper subfolders are out of scope, not unseen: their files
                # were never synced, so reconcile may still run.
                if depth < _MAX_DEPTH:
                    queue.append((item.id, depth + 1))
                else:
                    logger.info(
                        "drive_sync: folder %s is deeper than %d levels — not synced",
                        item.id, _MAX_DEPTH,
                    )
            else:
                files.setdefault(item.id, item)
        if listed > _MAX_VISIBLE_ITEMS:
            break
    return list(files.values())


class _Unreadable(Exception):
    """A file that downloaded but cannot be read safely. Recorded with no
    chunks, like an oversize one, so it is not refetched every tick."""


def _check_archive(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise _Unreadable("not a valid Office archive") from exc
    if total > _MAX_UNZIPPED_BYTES:
        raise _Unreadable(f"unzips to {total} bytes")


def _extract(data: bytes, suffix: str) -> str:
    if suffix in _TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    if suffix in (".docx", ".xlsx"):
        _check_archive(data)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"file{suffix}"
        path.write_bytes(data)
        return extract_text_from_file(path)


async def _file_text(client: DriveClient, item: DriveItem) -> str:
    export = _EXPORTS.get(item.mime_type)
    if export is not None:
        export_mime, suffix = export
        data = await client.export(item.id, export_mime, max_bytes=_MAX_FILE_BYTES)
    else:
        suffix = _DOWNLOADS[item.mime_type]
        if item.size is not None and item.size > _MAX_FILE_BYTES:
            raise DriveFileTooLarge(item.id)
        data = await client.download(item.id, max_bytes=_MAX_FILE_BYTES)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_extract, data, suffix), timeout=_EXTRACT_TIMEOUT_S
        )
    except _Unreadable:
        raise
    except TimeoutError as exc:
        raise _Unreadable(f"text extraction took over {_EXTRACT_TIMEOUT_S:.0f}s") from exc
    except Exception as exc:
        raise _Unreadable(f"text extraction failed: {type(exc).__name__}") from exc


async def _fetch_tick(
    *,
    client: DriveClient,
    settings: Settings,
    state: dict[str, Any],
    reconcile_only: bool,
    stats: dict[str, int],
) -> _TickFetch:
    """List the synced folders and fetch the text of changed files. Reads
    ``state`` only to decide what is current; nothing is written here."""
    fetch = _TickFetch()
    items = await _list_tree(client, settings.drive_sync_folder_id_list, fetch)
    stats["seen"] = len(items)

    dirty: list[DriveItem] = []
    for item in items:
        fetch.visible_ids.add(item.id)
        if not is_supported(item.mime_type) or _is_current(_record(state, item.id), item):
            stats["skipped"] += 1
            continue
        dirty.append(item)
    if reconcile_only:
        return fetch

    cap = max(0, settings.drive_max_files_per_scan)
    dirty.sort(key=lambda i: i.modified, reverse=True)
    if len(dirty) > cap:
        stats["capped"] = len(dirty) - cap
        logger.warning(
            "drive_sync: %d file(s) changed, cap is %d — the rest retry next tick",
            len(dirty), cap,
        )
    for item in dirty[:cap]:
        try:
            await _sleep()
            text = await _file_text(client, item)
        except DriveFileTooLarge:
            logger.info("drive_sync: %s is over %d bytes — not synced", item.id, _MAX_FILE_BYTES)
            text = ""
        except _Unreadable as exc:
            logger.warning("drive_sync: %s is unreadable (%s) — not synced", item.id, exc)
            text = ""
        except Exception:
            stats["failed"] += 1
            logger.exception("drive_sync: failed to fetch file %s", item.id)
            continue
        fetch.fetched.append((item, text))
    return fetch


# ---------------------------------------------------------------------------
# Local write phase
# ---------------------------------------------------------------------------


def _build_file_index() -> dict[str, list[Path]]:
    """Map each synced file id to its on-disk file(s), in one directory pass."""
    index: dict[str, list[Path]] = {}
    for path in _docs_dir().glob("drive-*.md"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        found = _FILE_ID_COMMENT.search(head)
        if found:
            index.setdefault(found.group(1), []).append(path)
    return index


def purge_file(
    file_id: str,
    store: ChromaDBStore,
    state: dict[str, Any] | None = None,
    file_index: dict[str, list[Path]] | None = None,
) -> bool:
    """Remove one synced file's text file, chunks and state record."""
    fid = sanitize_drive_id(file_id)
    if not fid:
        logger.warning("drive_sync: refuse to purge unsafe file id %r", file_id)
        return False
    files = state.setdefault("files", {}) if state is not None else {}
    filename = _safe_filename(str(_record(state or {}, fid).get("filename") or ""))
    store.delete_documents(ChromaDBStore.DRIVE_COLLECTION, {"drive_file_id": fid})
    if filename:
        (_docs_dir() / filename).unlink(missing_ok=True)
    index = file_index if file_index is not None else _build_file_index()
    for path in index.get(fid, []):
        path.unlink(missing_ok=True)
    files.pop(fid, None)
    logger.info("drive_sync: purged file %s", fid)
    return True


def reconcile_missing_files(
    visible_ids: set[str], store: ChromaDBStore, state: dict[str, Any]
) -> int:
    """Purge files (and orphan text files) no longer in the synced folders.
    Blocking I/O: run it via ``asyncio.to_thread`` on the event loop."""
    file_index = _build_file_index()
    gone = {f for f in state.get("files", {}) if f not in visible_ids}
    # Orphans: a text file on disk for a file id with no state record.
    gone |= {f for f in file_index if f not in visible_ids and f not in state["files"]}
    return sum(purge_file(fid, store, state, file_index=file_index) for fid in sorted(gone))


def _ingest_file_sync(item: DriveItem, text: str, store: ChromaDBStore, synced_at: str) -> int:
    """Write the file's text under docs/drive/ and re-index its chunks.
    Empty text leaves no chunks (a file with nothing readable in it)."""
    name = _safe_name(item.name)
    filename = slugify(name, item.id)
    store.delete_documents(ChromaDBStore.DRIVE_COLLECTION, {"drive_file_id": item.id})
    for path in _build_file_index().get(item.id, []):
        path.unlink(missing_ok=True)
    if not text.strip():
        return 0
    header = f"<!-- drive_file_id: {item.id} -->\n\n# {name}\n\n"
    body = (header + text).strip()[:_MAX_FILE_CHARS]
    (_docs_dir() / filename).write_text(body + "\n", encoding="utf-8")
    return ingest_text_sync(
        body,
        store,
        source_name=f"drive/{filename}",
        domain=infer_domain(name),
        collection=ChromaDBStore.DRIVE_COLLECTION,
        extra_metadata={
            "drive_file_id": item.id,
            "type": "drive",
            "name": name,
            "url": item.link,
            "synced_at": synced_at,
        },
    )


def _note_reconcile_skip(state: dict[str, Any], cause: str) -> None:
    raw = state.get("reconcile_skips")
    skips = (raw if isinstance(raw, int) else 0) + 1
    state["reconcile_skips"] = skips
    logger.warning(
        "drive_sync: skipping reconciliation — %s (%d consecutive skip(s); files "
        "removed from the folders stay indexed until reconciliation runs)",
        cause, skips,
    )


async def _apply_tick(
    *,
    store: ChromaDBStore,
    fetch: _TickFetch,
    now: datetime,
    reconcile_only: bool,
    stats: dict[str, int],
) -> None:
    """Local write phase — the caller must hold ``_FIXTURE_OP_LOCK``."""
    # Reload rather than reuse the pre-fetch snapshot: a fixture load or reset
    # may have replaced the state file while the network phase ran.
    state = load_state()
    known = state["files"]
    if fetch.incomplete:
        _note_reconcile_skip(state, f"listing incomplete ({fetch.incomplete})")
    elif not fetch.visible_ids and known:
        _note_reconcile_skip(
            state,
            f"the folders listed 0 files while {len(known)} are on record "
            "(refusing a mass purge on a blank listing)",
        )
    else:
        stats["purged"] = await asyncio.to_thread(
            reconcile_missing_files, fetch.visible_ids, store, state
        )
        state["reconcile_skips"] = 0

    if not reconcile_only:
        synced_at = now.isoformat()
        for item, text in fetch.fetched:
            try:
                chunks = await asyncio.to_thread(_ingest_file_sync, item, text, store, synced_at)
            except Exception:
                stats["failed"] += 1
                logger.exception("drive_sync: failed to index file %s", item.id)
                continue
            name = _safe_name(item.name)
            state["files"][item.id] = {
                "modified": item.modified,
                "md5": item.md5,
                "name": name,
                "filename": slugify(name, item.id) if chunks else "",
                "url": item.link,
                "synced_at": synced_at,
            }
            stats["updated"] += 1
            logger.info("drive_sync: indexed %s (%d chunks)", item.id, chunks)

    state["last_run"] = now.isoformat()
    state.pop("last_error", None)
    save_state(state)


# One tick at a time per process: the scheduler heartbeat and a "Sync now"
# from the knowledge page share this, so a manual run can never interleave
# with a scheduled one. A caller that finds it held skips (``busy`` in the
# stats) rather than queueing behind a slow network phase.
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
        logger.exception("drive_sync: could not record the last error")


async def run_drive_sync(
    *,
    store: ChromaDBStore | None = None,
    client: DriveClient | None = None,
    now: datetime | None = None,
    reconcile_only: bool = False,
) -> dict[str, int]:
    """One sync tick. Returns counts: seen / updated / skipped / failed / purged / capped
    (plus ``busy`` when another tick is already running in this process)."""
    if _RUN_LOCK.locked():
        logger.info("drive_sync: a tick is already running — skipping")
        return {"busy": 1}
    global _last_finished_at
    async with _RUN_LOCK:
        try:
            return await _run_drive_sync_locked(
                store=store, client=client, now=now, reconcile_only=reconcile_only
            )
        except Exception:
            _record_error("The last sync failed unexpectedly. Check the server logs.")
            raise
        finally:
            _last_finished_at = datetime.now(UTC)


async def _run_drive_sync_locked(
    *,
    store: ChromaDBStore | None,
    client: DriveClient | None,
    now: datetime | None,
    reconcile_only: bool,
) -> dict[str, int]:
    settings = get_settings()
    stats = {"seen": 0, "updated": 0, "skipped": 0, "failed": 0, "purged": 0, "capped": 0}
    if not settings.drive_sync_enabled:
        return stats

    # Same locking as notion_sync.run_notion_sync: skip while a fixture load /
    # client rotation runs, fetch unlocked, lock only the local writes.
    from openexecutive.cli.fixture_loader import _FIXTURE_OP_LOCK
    from openexecutive.clients.slots import get_active_client

    if _FIXTURE_OP_LOCK.locked():
        logger.info("drive_sync: fixture/rotation in progress — skipping this tick")
        return stats
    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    generation = get_active_client(settings)
    http: httpx.AsyncClient | None = None
    if client is None:
        try:
            token = service_account_token_provider(str(settings.drive_sync_service_account_file))
        except Exception:
            logger.exception("drive_sync: cannot load DRIVE_SYNC_SERVICE_ACCOUNT_FILE")
            _record_error("Could not sign in to Google Drive. Check the Drive credentials.")
            stats["failed"] += 1
            return stats
        http = httpx.AsyncClient(timeout=60.0)
        client = DriveClient(http, token)
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
                    "drive_sync: active client changed while fetching — discarding this tick"
                )
                return stats
            await _apply_tick(
                store=store,
                fetch=fetch,
                now=now or datetime.now(UTC),
                reconcile_only=reconcile_only,
                stats=stats,
            )
    finally:
        if http is not None:
            await http.aclose()

    logger.info("drive_sync: %s", stats)
    return stats


def purge_all_synced(store: ChromaDBStore, state: dict[str, Any] | None = None) -> int:
    """Remove every locally synced Drive file (text files, chunks, state)."""
    current = state if state is not None else load_state()
    file_index = _build_file_index()
    purged = 0
    for fid in set(current["files"]) | set(file_index):
        purged += purge_file(str(fid), store, current, file_index=file_index)
    for path in _docs_dir().glob("drive-*.md"):
        path.unlink(missing_ok=True)
    store.delete_drive_docs()
    current["files"] = {}
    save_state(current)
    return purged


def list_synced_files() -> dict[str, Any]:
    """What the knowledge page shows for Drive: one row per file on record.

    Read from the sync state (not Chroma), so it costs one small JSON read.
    ``indexed`` is false for a file with no readable text. Entries written
    before ``synced_at`` was recorded per file fall back to the tick time.
    """
    state = load_state()
    last_run = state.get("last_run") if isinstance(state.get("last_run"), str) else None
    files: list[dict[str, Any]] = []
    for fid, rec in state["files"].items():
        safe = sanitize_drive_id(str(fid))
        if not safe or not isinstance(rec, dict):
            continue
        filename = rec.get("filename") if isinstance(rec.get("filename"), str) else ""
        files.append(
            {
                "id": safe,
                "name": str(rec.get("name") or filename or safe),
                "url": _https_or_none(rec.get("url")),
                "modified_at": rec.get("modified") if isinstance(rec.get("modified"), str) else None,
                "synced_at": rec.get("synced_at")
                if isinstance(rec.get("synced_at"), str)
                else last_run,
                "indexed": bool(filename),
            }
        )
    files.sort(key=lambda f: f["name"].lower())
    error = state.get("last_error")
    return {
        "last_run": last_run,
        "last_error": error if isinstance(error, str) else None,
        "files": files,
    }


def read_synced_file(file_id: str) -> dict[str, Any] | None:
    """The stored text of one synced file, or None when it is unknown or empty.

    The path comes from the state record for a sanitized id, never from the
    caller, and must be a ``drive-*.md`` name directly under ``docs/drive/``.
    """
    safe = sanitize_drive_id(file_id)
    if not safe or safe != file_id:
        return None
    rec = load_state()["files"].get(safe)
    if not isinstance(rec, dict):
        return None
    filename = Path(str(rec.get("filename") or "")).name
    if not (filename.startswith("drive-") and filename.endswith(".md")):
        return None
    path = _docs_dir() / filename
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = _FILE_ID_COMMENT.sub("", text, count=1).lstrip()
    return {
        "id": safe,
        "name": str(rec.get("name") or filename),
        "url": _https_or_none(rec.get("url")),
        "content": text,
    }


def _https_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


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


def _enqueue(run_at: datetime, db_path: Path | None) -> int | None:
    try:
        action_id = insert_scheduled_action(
            run_at=run_at.isoformat(),
            channel=HEARTBEAT_CHANNEL,
            channel_ref=HEARTBEAT_CHANNEL_REF,
            intent_text=HEARTBEAT_INTENT,
            kind=HEARTBEAT_KIND,
            db_path=db_path,
        )
    except Exception:
        logger.exception("drive_sync: failed to enqueue the heartbeat")
        return None
    logger.info("drive_sync: next scan at %s (id=%d)", run_at.isoformat(), action_id)
    return action_id


def bootstrap_drive_sync_scan(db_path: Path | None = None) -> int | None:
    if _heartbeat_pending(db_path):
        return None
    return _enqueue(datetime.now(UTC) + timedelta(minutes=1), db_path)


def enqueue_next_drive_sync_scan(
    *, after: datetime | None = None, db_path: Path | None = None
) -> int | None:
    base = (after or datetime.now(UTC)).astimezone(UTC)
    minutes = get_settings().drive_sync_interval_minutes
    return _enqueue(base + timedelta(minutes=minutes), db_path)
