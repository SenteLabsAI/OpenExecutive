from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))

PRIORITY_ORDER: dict[str, int] = {"high": 0, "normal": 1, "low": 2}

# Bound an explicit bulk id list well under SQLite's variable limit. Public
# because the API layer validates against the same number — a private copy
# there would silently drift from the limit the store enforces.
BULK_MAX_IDS = 500


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ContentType(StrEnum):
    BUILTIN = "builtin"
    EXTERNAL = "external"
    # Failure case studies ship under `knowledge/builtin/failures/<domain>/`
    # but need their OWN id namespace. Folding them into `builtin:<domain>:<f>`
    # put them in the namespace `POST /knowledge/builtin` writes to, and since
    # no file sits at `builtin/<domain>/<f>` that route's 409 guard could not
    # fire — an upload INSERT-OR-IGNOREd onto the shipped row and inherited
    # its `approved` + `trusted_default = 1`.
    FAILURE = "failure"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ReviewItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    item_id: str
    content_type: ContentType
    domain: str
    filename: str
    status: ReviewStatus = ReviewStatus.PENDING
    priority: Priority = Priority.NORMAL
    # True only for content that SHIPS with the product. Provenance cannot be
    # inferred from status/reviewed_at: a brand-new user upload is also
    # (pending, reviewed_at NULL), so curation keyed on that would silently
    # approve someone's document.
    trusted_default: bool = False
    reviewer_notes: str = ""
    reviewed_at: str | None = None
    registered_at: str
    last_modified_at: str


class Annotation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    annotation_id: str
    item_id: str
    domain: str
    correction: str
    is_active: bool = True
    created_at: str


@contextmanager
def _get_conn(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_item(row: sqlite3.Row) -> ReviewItem:
    return ReviewItem(
        item_id=row["item_id"],
        content_type=ContentType(row["content_type"]),
        domain=row["domain"],
        filename=row["filename"],
        status=ReviewStatus(row["status"]),
        priority=Priority(row["priority"]),
        trusted_default=bool(row["trusted_default"]),
        reviewer_notes=row["reviewer_notes"] or "",
        reviewed_at=row["reviewed_at"],
        registered_at=row["registered_at"],
        last_modified_at=row["last_modified_at"],
    )


def _row_to_annotation(row: sqlite3.Row) -> Annotation:
    return Annotation(
        annotation_id=row["annotation_id"],
        item_id=row["item_id"],
        domain=row["domain"],
        correction=row["correction"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def build_item_id(content_type: ContentType, domain: str, filename: str) -> str:
    """The one place an `item_id` is constructed.

    Three disjoint namespaces. `builtin:` is reachable by a user upload
    (`POST /knowledge/builtin` writes `knowledge/builtin/<domain>/<file>`), so
    nothing else may share it — that is what `failure:` exists for.
    """
    if content_type is ContentType.EXTERNAL:
        return f"external:{filename}"
    return f"{content_type.value}:{domain}:{filename}"


def _insert_trusted_defaults(
    conn: sqlite3.Connection,
    rows: Iterable[tuple[ContentType, str, str]],
) -> int:
    """INSERT OR IGNORE a batch of (content_type, domain, filename) as trusted defaults.

    Shared by both registration syncs so the "what does shipped content look
    like on arrival" decision lives in exactly one place. `INSERT OR IGNORE`
    means an existing SME decision is never overwritten. Returns new rows.
    """
    now = datetime.now(UTC).isoformat()
    new_count = 0
    for content_type, domain, filename in rows:
        item_id = build_item_id(content_type, domain, filename)
        result = conn.execute(
            "INSERT OR IGNORE INTO review_items "
            "(item_id, content_type, domain, filename, status, trusted_default, "
            " registered_at, last_modified_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (
                item_id,
                content_type.value,
                domain,
                filename,
                ReviewStatus.APPROVED.value,
                now,
                now,
            ),
        )
        new_count += result.rowcount
        if result.rowcount == 0:
            # The row already existed, so the INSERT was ignored and its
            # provenance may predate the column. Flag it — status is untouched,
            # so an SME decision on a shipped doc survives.
            conn.execute(
                "UPDATE review_items SET trusted_default = 1 "
                "WHERE item_id = ? AND trusted_default = 0",
                (item_id,),
            )
    return new_count


_FAILURE_NAMESPACE_KEY = "failure_namespace_v1"


def _migrate_failure_namespace(conn: sqlite3.Connection) -> int:
    """One-shot: re-key shipped failure docs out of the `builtin:` namespace.

    They used to register as `builtin:<domain>:<file>` — the namespace
    `POST /knowledge/builtin` writes into. Because no file sits at
    `knowledge/builtin/<domain>/<file>` for a failure doc, that route's
    `if path.exists(): 409` guard never fired, so an upload of the same name
    INSERT-OR-IGNOREd onto the shipped row and inherited its `approved` +
    `trusted_default = 1`: trusted, unqueued, and labelled "ships with the
    product" in the UI.

    Carries `status`, `reviewed_at`, `reviewer_notes`, `priority` and every
    annotation across, so an SME's decision on a failure case study survives
    the re-key. `review_annotations.item_id` is an FK with ON DELETE CASCADE
    but no ON UPDATE, and `_get_conn` turns foreign keys on, so the parent and
    child updates have to land in one transaction with the check deferred.

    Runs from `sync_builtin_registrations`, after the new-namespace rows are
    inserted: an INSERT OR IGNORE will already have created the `failure:` row
    as a trusted default, so this UPDATE would collide. Old rows are therefore
    merged onto the new id rather than renamed — the old row's decision wins,
    since it is the one a human made.
    """
    already = conn.execute(
        "SELECT 1 FROM review_meta WHERE key = ?", (_FAILURE_NAMESPACE_KEY,)
    ).fetchone()
    if already is not None:
        return 0

    from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH
    from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

    migrated = 0
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for rel in sorted(SHIPPED_BUILTIN_FILES):
        path = PurePosixPath(rel)
        if path.parts[0] != "failures":
            continue
        domain, filename = path.parent.name, path.name
        old_id = f"builtin:{domain}:{filename}"
        new_id = build_item_id(ContentType.FAILURE, domain, filename)

        old = conn.execute(
            "SELECT status, reviewed_at, reviewer_notes, priority "
            "FROM review_items WHERE item_id = ?",
            (old_id,),
        ).fetchone()
        if old is None:
            continue

        # A real file at knowledge/builtin/<domain>/<filename> means this row
        # is NOT (only) the shipped failure doc: it is a user upload that the
        # old bug merged onto the shipped row — one row governing two files.
        # Transplanting its decision onto the case study would both mis-apply
        # the SME's judgement AND delete the only row governing the upload,
        # silently un-suppressing content someone had rejected. Keep the row
        # for the upload it actually governs, untrusted and withheld, and let
        # the shipped doc keep its own fresh trusted-default row.
        if (BUILTIN_KNOWLEDGE_PATH / domain / filename).exists():
            now = datetime.now(UTC).isoformat()
            if old["reviewed_at"] is None:
                # Never actually decided — the `approved` came from the shipped
                # default this row inherited, not from a human. Queue it.
                conn.execute(
                    "UPDATE review_items SET trusted_default = 0, status = 'pending', "
                    "last_modified_at = ? WHERE item_id = ?",
                    (now, old_id),
                )
            else:
                # A human decided. Keep that decision — it was far more likely
                # about their own upload than about a case study they never
                # saw — and just strip the trust the collision conferred.
                conn.execute(
                    "UPDATE review_items SET trusted_default = 0, last_modified_at = ? "
                    "WHERE item_id = ?",
                    (now, old_id),
                )
            continue

        # Carry the human's decision onto the new row, then drop the old one.
        conn.execute(
            "UPDATE review_items SET status = ?, reviewed_at = ?, "
            "reviewer_notes = ?, priority = ? WHERE item_id = ?",
            (
                old["status"],
                old["reviewed_at"],
                old["reviewer_notes"],
                old["priority"],
                new_id,
            ),
        )
        conn.execute(
            "UPDATE review_annotations SET item_id = ? WHERE item_id = ?",
            (new_id, old_id),
        )
        conn.execute("DELETE FROM review_items WHERE item_id = ?", (old_id,))
        migrated += 1

    conn.execute(
        "INSERT OR IGNORE INTO review_meta (key, value) VALUES (?, ?)",
        (_FAILURE_NAMESPACE_KEY, datetime.now(UTC).isoformat()),
    )
    return migrated


_TRUSTED_DEFAULTS_BACKFILL_KEY = "trusted_defaults_backfill_v1"


def _backfill_trusted_defaults(conn: sqlite3.Connection) -> int:
    """One-shot: promote never-touched shipped rows to trusted defaults.

    Shipped content used to register as `pending`, so every pre-existing
    install carries a queue of docs nobody asked for. It now registers
    `approved` with a NULL `reviewed_at`; this brings older databases in line.

    Scoped to `trusted_default = 1`, which is why this runs AFTER the
    registration syncs have flagged provenance — a user's own upload awaiting
    its first review is also (pending, reviewed_at NULL) and must not be
    approved on their behalf.

    The guiding rule is **no shipped doc loses retrievability on upgrade**.
    Before this change `pending` was still retrieved, so every shipped doc was
    reachable; now `pending` is withheld, and any shipped row left behind here
    would silently vanish from the Executive's context. That is why notes and
    annotations do NOT hold a row back: they are supplementary, not decisions.
    An annotated doc is the worst case — `retrieve` injects annotations
    by DOMAIN regardless of item status, so withholding the doc would feed the
    Executive a correction to a document it can no longer see.

    `reviewed_at IS NULL` is the real exclusion, and it is enough: `set_status`
    always stamps it, so every explicit approve or reject is skipped, and a
    rejected row is not `pending` anyway.

    Guarded by a marker row so it runs exactly once: re-running would undo a
    later curation request. `BEGIN IMMEDIATE` + `INSERT OR IGNORE` make the
    check-then-write safe against two processes booting at the same time
    (two uvicorn workers, or the API racing a CLI command).
    """
    conn.execute("BEGIN IMMEDIATE")
    already = conn.execute(
        "SELECT 1 FROM review_meta WHERE key = ?", (_TRUSTED_DEFAULTS_BACKFILL_KEY,)
    ).fetchone()
    if already is not None:
        return 0

    result = conn.execute(
        "UPDATE review_items SET status = 'approved' "
        "WHERE trusted_default = 1 AND status = 'pending' AND reviewed_at IS NULL"
    )
    conn.execute(
        "INSERT OR IGNORE INTO review_meta (key, value) VALUES (?, ?)",
        (_TRUSTED_DEFAULTS_BACKFILL_KEY, datetime.now(UTC).isoformat()),
    )
    return result.rowcount


class ReviewStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path

    @staticmethod
    def initialize_db(db_path: Path = DB_PATH) -> None:
        with _get_conn(db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS review_items (
                    item_id          TEXT PRIMARY KEY,
                    content_type     TEXT NOT NULL,
                    domain           TEXT NOT NULL,
                    filename         TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'pending',
                    priority         TEXT NOT NULL DEFAULT 'normal',
                    reviewer_notes   TEXT DEFAULT '',
                    reviewed_at      TEXT,
                    trusted_default  INTEGER NOT NULL DEFAULT 0,
                    registered_at    TEXT NOT NULL,
                    last_modified_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_status
                    ON review_items(status);
                CREATE INDEX IF NOT EXISTS idx_review_domain
                    ON review_items(domain, status);

                CREATE TABLE IF NOT EXISTS review_annotations (
                    annotation_id TEXT PRIMARY KEY,
                    item_id       TEXT NOT NULL
                        REFERENCES review_items(item_id) ON DELETE CASCADE,
                    domain        TEXT NOT NULL,
                    correction    TEXT NOT NULL,
                    is_active     INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_annot_domain
                    ON review_annotations(domain, is_active);

                CREATE TABLE IF NOT EXISTS review_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            # Additive column for databases created before provenance was
            # tracked. Existing rows default to 0 (not shipped); the
            # registration syncs re-flag the shipped ones on the next boot.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_items)")}
            if "trusted_default" not in cols:
                conn.execute(
                    "ALTER TABLE review_items "
                    "ADD COLUMN trusted_default INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def backfill_trusted_defaults(db_path: Path = DB_PATH) -> int:
        """Run the one-shot legacy migration. Call AFTER the registration syncs.

        Ordering matters: the migration only touches rows the syncs have
        flagged `trusted_default = 1`, so running it first would promote
        nothing (or, before provenance existed, would have promoted user
        uploads awaiting their first review).
        """
        with _get_conn(db_path) as conn:
            return _backfill_trusted_defaults(conn)

    @staticmethod
    def sync_builtin_registrations(db_path: Path = DB_PATH) -> int:
        """Register the built-in knowledge docs that ship with the product.

        Registers as a **trusted default**: `approved` with a NULL `reviewed_at`.
        This content ships with the product and is curated upstream, so a new
        instance starts with an empty review queue rather than ~81 items nobody
        asked to adjudicate. `reviewed_at` stays NULL to keep "shipped default"
        distinguishable from "an SME actually signed off on this".

        Idempotent — safe to call on every startup. Returns new registrations.
        """
        from openexecutive.knowledge.shipped_manifest import SHIPPED_BUILTIN_FILES

        # Driven by the committed manifest, never by scanning the directory.
        # `POST /knowledge/builtin` writes a USER's document into this same
        # tree, so "a .md file exists here" does not mean "this ships with the
        # product" — scanning marked those uploads trusted and auto-approved
        # them into retrieval. Skills are absent from the manifest by
        # construction (they have separate management).
        # Entries under `failures/` register as FAILURE so they never occupy
        # the `builtin:` namespace a user upload can reach.
        rows = [
            (
                ContentType.FAILURE
                if PurePosixPath(rel).parts[0] == "failures"
                else ContentType.BUILTIN,
                PurePosixPath(rel).parent.name,
                PurePosixPath(rel).name,
            )
            for rel in sorted(SHIPPED_BUILTIN_FILES)
        ]
        with _get_conn(db_path) as conn:
            count = _insert_trusted_defaults(conn, rows)
            _migrate_failure_namespace(conn)
            return count

    @staticmethod
    def sync_external_registrations(
        ingested_source_ids: list[dict[str, Any]], db_path: Path = DB_PATH
    ) -> int:
        """Register all ingested OER sources. Returns new registration count.

        Trusted defaults for the same reason as `sync_builtin_registrations`:
        these sources come from the shipped manifest, not from the user.
        """
        rows = [
            (
                ContentType.EXTERNAL,
                src["domains"][0] if src.get("domains") else "general",
                src["id"],
            )
            for src in ingested_source_ids
        ]
        with _get_conn(db_path) as conn:
            return _insert_trusted_defaults(conn, rows)

    def register(
        self,
        item_id: str,
        content_type: ContentType,
        domain: str,
        filename: str,
    ) -> ReviewItem:
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO review_items "
                "(item_id, content_type, domain, filename, registered_at, last_modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, content_type.value, domain, filename, now, now),
            )
            row = conn.execute(
                "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return _row_to_item(row)

    def touch_modified(self, item_id: str) -> None:
        """Reset status on content edit: approved/rejected → needs_revision."""
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_items SET status = 'needs_revision', last_modified_at = ? "
                "WHERE item_id = ? AND status IN ('approved', 'rejected')",
                (now, item_id),
            )
            # Always bump last_modified_at even if no status change
            conn.execute(
                "UPDATE review_items SET last_modified_at = ? "
                "WHERE item_id = ? AND status NOT IN ('approved', 'rejected')",
                (now, item_id),
            )

    def delete_item(self, item_id: str) -> None:
        """Remove a review item and cascade-delete its annotations."""
        with _get_conn(self._db_path) as conn:
            conn.execute("DELETE FROM review_items WHERE item_id = ?", (item_id,))

    def update_notes(self, item_id: str, notes: str) -> ReviewItem:
        """Update reviewer notes without changing status or reviewed_at."""
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_items SET reviewer_notes = ?, last_modified_at = ? WHERE item_id = ?",
                (notes, now, item_id),
            )
            row = conn.execute(
                "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Review item not found: {item_id}")
        return _row_to_item(row)

    def set_status(
        self,
        item_id: str,
        status: ReviewStatus,
        notes: str = "",
    ) -> ReviewItem:
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_items SET status = ?, reviewer_notes = ?, "
                "reviewed_at = ?, last_modified_at = ? WHERE item_id = ?",
                (status.value, notes, now, now, item_id),
            )
            row = conn.execute(
                "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Review item not found: {item_id}")
        return _row_to_item(row)

    def set_priority(self, item_id: str, priority: Priority) -> ReviewItem:
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_items SET priority = ?, last_modified_at = ? WHERE item_id = ?",
                (priority.value, now, item_id),
            )
            row = conn.execute(
                "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Review item not found: {item_id}")
        return _row_to_item(row)

    def bulk_approve(
        self,
        domain: str | None = None,
        item_ids: list[str] | None = None,
    ) -> list[str]:
        """Approve many pending items at once. Returns the item_ids updated.

        Selects by ``domain`` and/or an explicit ``item_ids`` list (capped at
        ``BULK_MAX_IDS``). With neither selector, every pending item is
        approved — the long-standing "Approve all pending" behavior.
        """
        now = datetime.now(UTC).isoformat()
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if item_ids is not None:
            if not item_ids:
                return []
            if len(item_ids) > BULK_MAX_IDS:
                raise ValueError(
                    f"item_ids exceeds BULK_MAX_IDS ({len(item_ids)} > {BULK_MAX_IDS}); "
                    "silently dropping the tail would under-approve without telling "
                    "the caller"
                )
            clauses.append(f"item_id IN ({','.join('?' * len(item_ids))})")
            params.extend(item_ids)
        where = " AND ".join(clauses)
        with _get_conn(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")  # select + update under one write lock
            rows = conn.execute(
                f"SELECT item_id FROM review_items WHERE {where}", params
            ).fetchall()
            ids = [r["item_id"] for r in rows]
            if not ids:
                return []
            conn.execute(
                "UPDATE review_items SET status = 'approved', reviewed_at = ?, "
                f"last_modified_at = ? WHERE item_id IN ({','.join('?' * len(ids))})",
                (now, now, *ids),
            )
        return ids

    def _move_untouched(self, domain: str, *, from_status: str, to_status: str) -> int:
        """Flip status for a domain's never-reviewed items only.

        Two guards, both required. `trusted_default = 1` restricts this to
        content that SHIPS with the product — without it a user's own upload,
        which is also (pending, reviewed_at NULL), would be approved by a
        "stop curating" click it had nothing to do with. `reviewed_at IS NULL`
        then spares any shipped doc an SME has explicitly decided on.

        `reviewed_at` is left alone: moving an item between the queue and
        trusted-default is not itself a review decision.
        """
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            result = conn.execute(
                "UPDATE review_items SET status = ?, last_modified_at = ? "
                "WHERE trusted_default = 1 AND status = ? AND reviewed_at IS NULL "
                "  AND domain = ?",
                (to_status, now, from_status, domain),
            )
        return result.rowcount

    def queue_for_curation(self, domain: str) -> int:
        """Move one domain's trusted defaults into the review queue.

        Queued items are `pending`, and pending is withheld from retrieval, so
        this deliberately takes that domain's knowledge away from the Executive
        until the user works through it. Returns the number of items queued.
        """
        return self._move_untouched(
            domain,
            from_status=ReviewStatus.APPROVED.value,
            to_status=ReviewStatus.PENDING.value,
        )

    def stop_curation(self, domain: str) -> int:
        """Undo `queue_for_curation` — restore a domain's untouched defaults."""
        return self._move_untouched(
            domain,
            from_status=ReviewStatus.PENDING.value,
            to_status=ReviewStatus.APPROVED.value,
        )

    def count_trusted_defaults_by_domain(self) -> dict[str, int]:
        """Map domain → number of never-reviewed trusted defaults.

        Drives the "Curate N" affordance so the UI can state up front how much
        content a curation request would withhold.
        """
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT domain, COUNT(*) AS n FROM review_items "
                "WHERE trusted_default = 1 AND status = 'approved' "
                "  AND reviewed_at IS NULL GROUP BY domain"
            ).fetchall()
        return {row["domain"]: row["n"] for row in rows}

    def get_withheld_keys(self, content_type: ContentType) -> set[tuple[str, str]]:
        """(domain, filename) pairs that must not reach a specialist.

        `pending` *and* `rejected`. Pending means a human deliberately queued
        the item for curation, and the architecture contract is that the
        Executive never sees pending material — shipped content registers as
        an approved trusted default precisely so that gate can be real.

        Keyed on the PAIR, not the bare filename. Filename alone was ambiguous
        and user-reachable: `POST /knowledge/builtin` lets a caller choose any
        `(domain, filename)`, so uploading a file named after a shipped doc in
        some *other* domain silently withheld that shipped doc from every
        specialist. It also meant the design leaned on "all shipped basenames
        are unique", which only a test over the shipped tree ever checked.

        `needs_revision` is deliberately absent: `touch_modified` flips an
        item there on *any* content edit, so withholding it would make editing
        a file in the knowledge UI silently delete it from retrieval.
        """
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT domain, filename FROM review_items "
                "WHERE content_type = ? AND status IN ('pending', 'rejected')",
                (content_type.value,),
            ).fetchall()
        return {(row["domain"], row["filename"]) for row in rows}

    def get_withheld_source_ids(self) -> set[str]:
        """Withheld OER source ids. `source_id` is already unambiguous."""
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT filename FROM review_items "
                "WHERE content_type = ? AND status IN ('pending', 'rejected')",
                (ContentType.EXTERNAL.value,),
            ).fetchall()
        return {row["filename"] for row in rows}

    def get_priority_map(self, content_type: ContentType) -> dict[tuple[str, str], str]:
        """Map (domain, filename) → priority for approved items of a type.

        Same key as `get_withheld_keys`, and for the same reason: a bare
        filename is ambiguous across domains and user-choosable.
        """
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT domain, filename, priority FROM review_items "
                "WHERE content_type = ? AND status = 'approved'",
                (content_type.value,),
            ).fetchall()
        return {(row["domain"], row["filename"]): row["priority"] for row in rows}

    def list_items(
        self,
        status: ReviewStatus | None = None,
        domain: str | None = None,
        content_type: ContentType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReviewItem]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if content_type is not None:
            clauses.append("content_type = ?")
            params.append(content_type.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM review_items {where} "
                "ORDER BY registered_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def get_item(self, item_id: str) -> ReviewItem | None:
        with _get_conn(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM review_items GROUP BY status"
            ).fetchall()
        counts: dict[str, int] = {s.value: 0 for s in ReviewStatus}
        for row in rows:
            counts[row["status"]] = row["n"]
        counts["total"] = sum(counts.values())
        return counts

    def add_annotation(self, item_id: str, domain: str, correction: str) -> Annotation:
        ann_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "INSERT INTO review_annotations "
                "(annotation_id, item_id, domain, correction, is_active, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (ann_id, item_id, domain, correction, now),
            )
        return Annotation(
            annotation_id=ann_id,
            item_id=item_id,
            domain=domain,
            correction=correction,
            is_active=True,
            created_at=now,
        )

    def list_annotations(
        self,
        item_id: str | None = None,
        domains: list[str] | None = None,
        active_only: bool = True,
    ) -> list[Annotation]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("is_active = 1")
        if item_id is not None:
            clauses.append("item_id = ?")
            params.append(item_id)
        if domains is not None:
            placeholders = ",".join("?" * len(domains))
            clauses.append(f"domain IN ({placeholders})")
            params.extend(domains)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with _get_conn(self._db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM review_annotations {where} ORDER BY created_at ASC",
                params,
            ).fetchall()
        return [_row_to_annotation(r) for r in rows]

    def toggle_annotation(self, annotation_id: str, is_active: bool) -> None:
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_annotations SET is_active = ? WHERE annotation_id = ?",
                (1 if is_active else 0, annotation_id),
            )

    def update_annotation(self, annotation_id: str, correction: str) -> None:
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE review_annotations SET correction = ? WHERE annotation_id = ?",
                (correction, annotation_id),
            )

    def delete_annotation(self, annotation_id: str) -> None:
        with _get_conn(self._db_path) as conn:
            conn.execute(
                "DELETE FROM review_annotations WHERE annotation_id = ?", (annotation_id,)
            )
