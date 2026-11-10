"""Share a document by link: one read-only page anyone holding the link can
open, with no sign-in (the Documents page's **Share by link**).

A share is one row per document (``artifact_shares``, in the alerts store's
database, beside the drafted documents): the document's composite id (``artifact_records``), an
unguessable key (``secrets.token_urlsafe``, 192 bits) and who shared it.
The link is ``public_path(key)`` on the app's own address.

* **That one version.** A link opens the exact document it was made for. A
  revised version is a new document (``supersedes_id``) and needs its own
  share, so a link never starts showing text its sharer has not seen.
* **Who may share.** The person the document belongs to, and the principal
  for the team's documents (``may_share``); always only a document they can
  open. A link into another app (``format == "link"``) keeps that app's own
  sharing and is never shared here.
* **Stop is at once.** ``stop_share`` deletes the row; the next open of the
  link answers not found. Deleting the document stops it too. While the
  document is archived the link answers not found, and it works again on
  restore.
* **It ends with its sharer.** A link only opens while the person who shared
  it is still on the team and could still open and share the document
  themselves. The first open after that deletes it (``load_shared``), so it
  never comes back if they return, and sharing the document again makes a
  new link (``share``).
* **On the record.** Sharing and stopping each write an audit row
  (``document_shared`` / ``document_share_stopped``), private to the person
  the document belongs to (the team's: visible as the team's).
* **Nothing else leaks.** ``load_shared`` returns the one record; the public
  route turns it into the title, kind and text only, never who made it,
  which workflow, or any id.
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from openexecutive.alerts.store import _get_conn
from openexecutive.orchestrator.artifact_records import (
    ArtifactNotFound,
    ArtifactRecord,
    MalformedArtifactId,
    Viewer,
    load_artifact,
    parse_artifact_id,
)

logger = logging.getLogger(__name__)

# What a key looks like: token_urlsafe(24) is 32 URL-safe characters. The
# public route refuses anything else before touching the database.
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32}")
_KEY_BYTES = 24


class ShareRefused(ValueError):
    """The document can't be shared by link (a link into another app, or an
    archived one). The message is plain words for the page."""


def initialize_db() -> None:
    """Make the table. Only sharing does this; the reads (the list page, a
    public open) never write, and a missing table means no links yet."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS artifact_shares (
                artifact_id  TEXT PRIMARY KEY,
                share_key    TEXT NOT NULL UNIQUE,
                shared_by    INTEGER,
                created_at   TEXT NOT NULL
            )
            """
        )


def _no_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def public_path(key: str) -> str:
    """Where a share opens, relative to the app's own address."""
    return f"/shared/{key}"


def may_share(rec: ArtifactRecord, viewer: Viewer) -> bool:
    """Whether ``viewer`` (who can already open ``rec``) may share it: its
    owner, or the principal for one with no owner (the team's, or the
    principal's own drafts). Never a link into another app."""
    if rec.format == "link":
        return False
    if viewer.person_id is None:
        return False
    if rec.owner_person_id is None:
        return viewer.is_principal
    return rec.owner_person_id == viewer.person_id


def share_keys(artifact_ids: Iterable[str]) -> dict[str, str]:
    """``{artifact id: key}`` for those of ``artifact_ids`` that are shared."""
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return {}
    found: dict[str, str] = {}
    # SQLite caps bound parameters; the list page holds at most a few hundred.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        try:
            with _get_conn() as conn:
                rows = conn.execute(
                    "SELECT artifact_id, share_key FROM artifact_shares "
                    f"WHERE artifact_id IN ({', '.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _no_table(exc):
                return {}
            raise
        found.update({r["artifact_id"]: r["share_key"] for r in rows})
    return found


def share(composite_id: str, *, viewer: Viewer) -> str:
    """Share the document by link (or answer the link it already has) and
    return its key. ``ArtifactNotFound`` when ``viewer`` can't open it or
    may not share it, ``ShareRefused`` when it can't be shared."""
    rec = load_artifact(composite_id, viewer=viewer)
    if not may_share(rec, viewer):
        if rec.format == "link":
            raise ShareRefused("This one lives in another app. Share it from there.")
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    if rec.archived_at is not None:
        raise ShareRefused("Restore it first to share it.")
    initialize_db()
    with _get_conn() as conn:
        # Holds the write lock from the read on, so two shares at once (a
        # double click) can't both insert: the second waits, then answers
        # the first one's key.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT share_key, shared_by FROM artifact_shares WHERE artifact_id = ?", (rec.id,)
        ).fetchone()
        if row is not None and row["shared_by"] == viewer.person_id:
            return str(row["share_key"])
        # Someone else's link (a principal before this one): it no longer
        # counts, and this share is a new link.
        conn.execute("DELETE FROM artifact_shares WHERE artifact_id = ?", (rec.id,))
        key = secrets.token_urlsafe(_KEY_BYTES)
        conn.execute(
            "INSERT INTO artifact_shares (artifact_id, share_key, shared_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (rec.id, key, viewer.person_id, datetime.now(UTC).isoformat()),
        )
    _audit("document_shared", f"Shared a document by link: {rec.title}", rec, viewer)
    return key


def stop_share(composite_id: str, *, viewer: Viewer) -> bool:
    """Take the document's link down. True when there was one.
    ``ArtifactNotFound`` when ``viewer`` can't open it or may not share it."""
    rec = load_artifact(composite_id, viewer=viewer)
    if not may_share(rec, viewer):
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    stopped = forget(rec.id)
    if stopped:
        _audit("document_share_stopped", f"Stopped sharing a document by link: {rec.title}", rec, viewer)
    return stopped


def forget(artifact_id: str) -> bool:
    """Drop a document's link, whoever shared it (the document is being
    deleted). True when there was one."""
    try:
        with _get_conn() as conn:
            cur = conn.execute("DELETE FROM artifact_shares WHERE artifact_id = ?", (artifact_id,))
            return cur.rowcount > 0
    except sqlite3.OperationalError as exc:
        if _no_table(exc):
            return False
        raise


def load_shared(key: str) -> ArtifactRecord | None:
    """The document a link opens, or None: a key of the wrong shape, no
    such link, a document gone or archived, or a sharer who could no longer
    open or share it."""
    if not KEY_PATTERN.fullmatch(key or ""):
        return None
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT artifact_id, shared_by FROM artifact_shares WHERE share_key = ?", (key,)
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if _no_table(exc):
            return None
        raise
    if row is None or row["shared_by"] is None:
        return None
    artifact_id = str(row["artifact_id"])
    sharer = _sharer(int(row["shared_by"]))
    if sharer is None:
        # The People list couldn't be read just now: not this time, but
        # the link stays.
        return None
    if sharer is False:
        forget(artifact_id)
        return None
    try:
        parse_artifact_id(artifact_id)
        rec = load_artifact(artifact_id, viewer=sharer)
    except (ArtifactNotFound, MalformedArtifactId):
        # Gone, or the sharer can't open it any more: the link ends.
        forget(artifact_id)
        return None
    if not may_share(rec, sharer):
        forget(artifact_id)
        return None
    if rec.archived_at is not None:
        # Paused while archived; it opens again on restore.
        return None
    return rec


def _audit(event_type: str, summary: str, rec: ArtifactRecord, viewer: Viewer) -> None:
    """An audit row for a share or a stop, private to the document's owner
    when it has one. Never fails the share."""
    try:
        from openexecutive.audit import log_event

        log_event(
            event_type,
            summary[:200],
            actor="user",
            details={"artifact_id": rec.id, "by_person_id": viewer.person_id},
            private=rec.owner_person_id is not None,
            private_to_person=rec.owner_person_id,
        )
    except Exception:
        logger.warning("artifact shares: couldn't write the audit row", exc_info=True)


def _sharer(person_id: int) -> Viewer | Literal[False] | None:
    """The sharer as a viewer, from one read of the People list: False when
    they have left the team, None when the list can't be read right now (a
    busy database), which never ends a link."""
    from openexecutive.people.store import get_person

    try:
        person = get_person(person_id)
    except Exception:
        logger.warning("shares: couldn't read the People list", exc_info=True)
        return None
    if person is None or person.archived:
        return False
    return Viewer(person_id=person_id, is_principal=bool(person.is_principal))


__all__ = [
    "KEY_PATTERN",
    "ShareRefused",
    "forget",
    "initialize_db",
    "load_shared",
    "may_share",
    "public_path",
    "share",
    "share_keys",
    "stop_share",
]
