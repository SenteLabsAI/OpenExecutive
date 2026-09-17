from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Holds strong references to background tasks so GC cannot cancel them mid-flight.
_background_tasks: set[asyncio.Task[None]] = set()


class Decision(BaseModel):
    id: int | None = None
    timestamp: str
    domain: str
    summary: str
    rationale: str = ""
    outcome: str = ""
    tags: str = ""
    # Department slug owning this decision. Empty string for legacy/unscoped
    # rows. Added in the Departments feature (Phase 1) via additive ALTER.
    department: str = ""
    # Conversation session that produced this decision. Empty string for rows
    # created before session scoping (Phase 5) or from global contexts (CLI,
    # scheduler). Added via additive ALTER — see initialize_db migration below.
    session_id: str = ""


class Initiative(BaseModel):
    id: int | None = None
    title: str
    status: str
    created_at: str
    updated_at: str
    summary: str = ""
    department: str = ""


class Advice(BaseModel):
    id: int | None = None
    timestamp: str
    domain: str
    query_summary: str
    advice_summary: str
    department: str = ""
    # Conversation session that produced this advice. See Decision.session_id.
    session_id: str = ""


class ScheduledAction(BaseModel):
    id: int | None = None
    created_at: str
    run_at: str
    channel: str
    channel_ref: str
    intent_text: str
    originating_session_id: str | None = None
    status: str = "pending"
    attempts: int = 0
    last_error: str = ""
    department: str = ""
    # Phase 4: authority-gate metadata. `kind` lets the runner and gate
    # distinguish user-facing follow-ups ("ad_hoc") from internal cadences
    # ("dept_cadence") and awaiting-human pauses ("awaiting_human").
    # The nudge engine adds two more: "nudge_scan" (internal heartbeat that
    # bypasses outbound dispatch) and "proactive_nudge" (per-channel nudge
    # emitted by a scan, dispatched via the normal ad-hoc path).
    # Shift 3 adds "principal_brief_morning" and "principal_brief_eod" —
    # recurring rows that run the morning_brief / end_of_day_digest
    # workflow and DM the artifact to the principal. Seeded once at
    # scheduler startup via seed_principal_briefs(); each fire chains
    # the next occurrence in _run_principal_brief().
    kind: str = "ad_hoc"
    assigned_to_person_id: int | None = None
    awaiting_response_since: datetime | None = None
    # Per-scope dedup key for proactive nudges. Format:
    # "nudge:stalled:{run_id}", "nudge:commitment:{action_id}",
    # "nudge:initiative:{id}". NULL for every non-nudge row.
    scope_key: str | None = None
    # Authority scope the approver must hold when this dept-scoped action
    # reaches the gate. The scheduler runner parses this into an
    # AuthorityScope and passes it to gate_action; None falls back to
    # WILDCARD (i.e. routes to the principal). Persisted so callers can
    # pin per-action routing (e.g. fixture-staged proposals; the
    # Executive's schedule_followup tool when the LLM specifies scope).
    required_scope: str | None = None


class OutboundContext(BaseModel):
    """A linkage record connecting an outbound DM oe sent to a person back to
    the conversation that triggered it.

    When the Executive DMs a rostered person mid-conversation (e.g. the
    principal says "DM Alex about the pizza thing"), the send is otherwise
    fire-and-forget: the recipient's reply lands in a fresh per-person session
    with no history, so oe has no idea what they're replying about. This row
    carries the outbound text + the originating session id so the inbound
    handler can hydrate the reply turn with the backstory.

    Lifecycle: created ``open`` at send time, flipped to ``consumed`` (one-shot)
    the first time a reply from that recipient is matched within the recency
    window. See ``insert_outbound_context`` / ``find_open_outbound_context`` /
    ``mark_outbound_context_consumed``.
    """

    id: int | None = None
    created_at: str
    # Outbound channel string — matches the inbound lookup key. One of
    # _VALID_OUTBOUND_CHANNELS ("discord_dm", "telegram", "slack_dm").
    channel: str
    # Recipient's channel-specific id (discord snowflake, telegram chat id,
    # slack user id) — the rendezvous key with the inbound reply.
    channel_ref: str
    # Resolved Person.id of the recipient, when known. Nullable.
    recipient_person_id: int | None = None
    # The (bounded) DM body oe sent.
    outbound_text: str
    # The principal session that triggered the send, so the inbound handler can
    # pull a backstory excerpt via load_messages(). Nullable for safety.
    originating_session_id: str | None = None
    # Platform message id of the outbound DM, best-effort. Stored for a future
    # exact-match tier; matching does not depend on it. Nullable.
    outbound_message_id: str | None = None
    consumed_at: str | None = None
    status: str = "open"


DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))


@contextmanager
def _get_conn(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path = DB_PATH) -> None:
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                domain TEXT NOT NULL,
                summary TEXT NOT NULL,
                rationale TEXT DEFAULT '',
                outcome TEXT DEFAULT '',
                tags TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS initiatives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                summary TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS advice_given (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                domain TEXT NOT NULL,
                query_summary TEXT NOT NULL,
                advice_summary TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id        TEXT PRIMARY KEY,
                title             TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                caller_person_id  INTEGER
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                action_chips TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS scheduled_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                run_at TEXT NOT NULL,
                channel TEXT NOT NULL,
                channel_ref TEXT NOT NULL,
                intent_text TEXT NOT NULL,
                originating_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_due
                ON scheduled_actions(status, run_at);
            CREATE TABLE IF NOT EXISTS voice_personas (
                slug TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # Additive migration: the Departments feature (Phase 1) tags every
        # episodic row with the department slug that owns it. Pre-existing
        # databases predate the column; PRAGMA-guarded ALTERs make second-boot
        # a no-op (mirroring `audit/logger.py:155`). Concurrent workers booting
        # at the same moment can race the ALTER — SQLite raises
        # OperationalError("duplicate column name") on the loser; treat as success.
        for table in ("decisions", "initiatives", "advice_given", "scheduled_actions"):
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if "department" not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN department TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

        # Phase 5 additive migration: tag decisions and advice with the
        # conversation session that produced them so format_for_prompt() can
        # scope context to the current thread instead of returning data from
        # every conversation globally. initiatives stay unscoped (company-wide).
        for table in ("decisions", "advice_given"):
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if "session_id" not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

        # Additive migration: persist per-assistant-message action chips (the
        # tool-action pills) so reopening a saved session shows them instead of
        # bare prose. Nullable JSON TEXT; legacy rows stay NULL.
        _cm_existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(chat_messages)")
        }
        if "action_chips" not in _cm_existing:
            try:
                conn.execute("ALTER TABLE chat_messages ADD COLUMN action_chips TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # Phase 4 additive columns for scheduled_actions only.
        _sa_existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(scheduled_actions)")
        }
        for col, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'ad_hoc'"),
            ("assigned_to_person_id", "INTEGER"),
            ("awaiting_response_since", "TEXT"),
            # Nudge-engine dedup key. Nullable so existing/non-nudge rows are
            # unaffected; partial index below keeps lookups O(log n).
            ("scope_key", "TEXT"),
            # Authority scope the action's approver must hold. Read by the
            # scheduler runner at gate time so dept-scoped proposals can
            # route to a non-principal approver (e.g. vendor_onboarding
            # → the Product head). Nullable; None falls back to WILDCARD.
            ("required_scope", "TEXT"),
        ):
            if col not in _sa_existing:
                try:
                    conn.execute(
                        f"ALTER TABLE scheduled_actions ADD COLUMN {col} {ddl}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

        # Partial index on the nudge-dedup key. Only nudge rows ever set
        # scope_key, so a partial index keeps it small even with millions of
        # legacy rows.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_scope_key "
            "ON scheduled_actions(scope_key, created_at DESC) "
            "WHERE scope_key IS NOT NULL"
        )

        # Multi-user scoping: tag each session with the Person who started it
        # so the Recent-chats sidebar can filter by caller. Nullable for
        # legacy rows created before this column existed — those are hidden
        # from list_sessions() (NULL = ? is never true) but still reachable
        # by direct session_id URL.
        _sessions_existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        if "caller_person_id" not in _sessions_existing:
            try:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN caller_person_id INTEGER"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

        # Trust-ledger tables (first-climb autonomy, Build 1+2).
        # `decision_instances` is the correlation unit: links a proposal, its
        # human resolution (approve/edit/reject), and any subsequent reversal.
        # `decision_class_state` holds the per-class mode (propose /
        # auto_execute) written by the evaluator job (Build 3).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decision_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_class TEXT NOT NULL,
                created_at TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                originating_session_id TEXT,
                proposed_payload_json TEXT NOT NULL,
                idempotency_key TEXT,
                gate_mode TEXT NOT NULL DEFAULT 'propose',
                approver_person_id INTEGER,
                confidence REAL,
                status TEXT NOT NULL DEFAULT 'proposed',
                resolved_at TEXT,
                resolver_person_id INTEGER,
                final_payload_json TEXT,
                external_event_id TEXT,
                reversal_reason TEXT,
                severity TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_di_class_created
                ON decision_instances(decision_class, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_di_idem
                ON decision_instances(idempotency_key)
                WHERE idempotency_key IS NOT NULL;

            CREATE TABLE IF NOT EXISTS decision_class_state (
                decision_class TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'propose',
                updated_at TEXT NOT NULL,
                last_eval_json TEXT,
                breaker_tripped_at TEXT
            );

            -- Links an outbound DM oe sent back to the conversation that
            -- triggered it, so the recipient's reply can be hydrated with the
            -- backstory instead of landing in a context-free per-person
            -- session. One-shot consumed on first matched reply. See the
            -- OutboundContext model + insert/find/consume helpers below.
            CREATE TABLE IF NOT EXISTS outbound_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                channel TEXT NOT NULL,
                channel_ref TEXT NOT NULL,
                recipient_person_id INTEGER,
                outbound_text TEXT NOT NULL,
                originating_session_id TEXT,
                outbound_message_id TEXT,
                consumed_at TEXT,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_outbound_ctx_lookup
                ON outbound_context(channel, channel_ref, status, created_at DESC);

            -- One-shot data migrations. Schema changes above are idempotent
            -- DDL and need no bookkeeping; this table is for sweeps that must
            -- run exactly once per DB (e.g. cancelling rows a removed feature
            -- left behind). A migration inserts its name when it has applied.
            CREATE TABLE IF NOT EXISTS app_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)


def _mirror_to_dept_honcho(
    *,
    department: str,
    kind: str,
    body: str,
    person_id: int | None,
) -> None:
    """Best-effort mirror of an episodic row into the dept peer's Honcho timeline.

    Skipped when ``department`` is empty or the literal ``"general"`` —
    those rows have no dept peer to land in. SQL is the system of record;
    a Honcho failure (no loop, disabled, network error) never affects
    the episodic INSERT that just committed. The wrapper itself audits
    every outcome via the peer_memory event type.

    Type ``kind`` must be one of the literals accepted by
    ``honcho_client.NoteKind`` — typing at the boundary keeps mypy
    honest at call sites.
    """
    if not department or department == "general":
        return
    # Local import keeps the episodic module importable in environments
    # where the honcho extra isn't installed (no settings.honcho_enabled
    # would short-circuit anyway, but defensive isolation matters here
    # because episodic is one of the lowest-level modules in the stack).
    from openexecutive.memory.honcho_client import NoteKind, append_department_note

    # Typed cast at the boundary so mypy sees a NoteKind, not a free str.
    note_kind: NoteKind = kind  # type: ignore[assignment]
    append_department_note(
        department_slug=department,
        kind=note_kind,
        body=body,
        person_id=person_id,
    )


def store_decision(
    domain: str,
    summary: str,
    rationale: str = "",
    tags: str = "",
    department: str = "",
    session_id: str = "",
    person_id: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(UTC)
    dedup_window_start = (now - timedelta(days=7)).isoformat()
    summary_prefix = summary[:80]
    with _get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id, rationale FROM decisions"
            " WHERE domain = ? AND timestamp > ? AND substr(summary, 1, 80) = ? AND session_id = ?",
            (domain, dedup_window_start, summary_prefix, session_id),
        ).fetchone()
        if existing:
            # Don't lose a newly-provided rationale when the prior row had none.
            if rationale and not existing["rationale"]:
                conn.execute(
                    "UPDATE decisions SET rationale = ? WHERE id = ?",
                    (rationale, existing["id"]),
                )
            logger.debug("Skipping duplicate decision in domain=%s (matches row %d)", domain, existing["id"])
            # Do NOT mirror on a dedup hit — the dept peer already has
            # the prior note from when the original row landed.
            return
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, rationale, tags, department, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(), domain, summary, rationale, tags, department, session_id),
        )
    # Mirror only after the SQL commit lands. Body is concise + structured
    # so Honcho's representation extractor can pick out summary/rationale
    # cleanly later.
    body_parts = [f"Decision ({domain}): {summary}"]
    if rationale:
        body_parts.append(f"Rationale: {rationale}")
    _mirror_to_dept_honcho(
        department=department,
        kind="decision",
        body="\n".join(body_parts),
        person_id=person_id,
    )


def store_initiative(
    title: str,
    status: str,
    summary: str = "",
    department: str = "",
    person_id: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(UTC).isoformat()
    is_insert = False
    is_status_change = False
    with _get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id, status FROM initiatives WHERE title = ?", (title,)
        ).fetchone()
        if existing:
            is_status_change = existing["status"] != status
            # Only overwrite an existing department when the caller actually
            # passed a non-empty value — preserves the original tag if the
            # update path is invoked without department context.
            if department:
                conn.execute(
                    "UPDATE initiatives SET status = ?, updated_at = ?, summary = ?, department = ? WHERE id = ?",
                    (status, now, summary, department, existing["id"]),
                )
            else:
                conn.execute(
                    "UPDATE initiatives SET status = ?, updated_at = ?, summary = ? WHERE id = ?",
                    (status, now, summary, existing["id"]),
                )
        else:
            is_insert = True
            conn.execute(
                "INSERT INTO initiatives (title, status, created_at, updated_at, summary, department) VALUES (?, ?, ?, ?, ?, ?)",
                (title, status, now, now, summary, department),
            )
    # Mirror only on a new initiative OR a real status transition.
    # Idempotent upserts (same title + same status) don't fire — that
    # would spam the dept peer with redundant notes on every routine
    # extraction pass.
    if is_insert or is_status_change:
        body = f"Initiative '{title}' → status={status}"
        if summary:
            body = f"{body}\n{summary}"
        _mirror_to_dept_honcho(
            department=department,
            kind="initiative_update",
            body=body,
            person_id=person_id,
        )


def store_advice(
    domain: str,
    query_summary: str,
    advice_summary: str,
    department: str = "",
    session_id: str = "",
    person_id: int | None = None,
    db_path: Path = DB_PATH,
) -> None:
    now = datetime.now(UTC)
    dedup_window_start = (now - timedelta(days=7)).isoformat()
    advice_prefix = advice_summary[:80]
    with _get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM advice_given"
            " WHERE domain = ? AND timestamp > ? AND substr(advice_summary, 1, 80) = ? AND session_id = ?",
            (domain, dedup_window_start, advice_prefix, session_id),
        ).fetchone()
        if existing:
            logger.debug("Skipping duplicate advice in domain=%s (matches row %d)", domain, existing["id"])
            return
        conn.execute(
            "INSERT INTO advice_given (timestamp, domain, query_summary, advice_summary, department, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(), domain, query_summary, advice_summary, department, session_id),
        )
    body = f"Query: {query_summary}\nAdvice ({domain}): {advice_summary}"
    _mirror_to_dept_honcho(
        department=department,
        kind="advice",
        body=body,
        person_id=person_id,
    )


def get_recent_advice(
    limit: int = 5,
    db_path: Path | None = None,
    session_id: str = "",
) -> list[Advice]:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM advice_given WHERE session_id = ?"
                " ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM advice_given ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
    return [Advice(**dict(row)) for row in rows]


def get_recent_decisions(
    limit: int = 10,
    db_path: Path | None = None,
    session_id: str = "",
) -> list[Decision]:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE session_id = ?"
                " ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
    return [Decision(**dict(row)) for row in rows]


def get_active_initiatives(db_path: Path = DB_PATH) -> list[Initiative]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM initiatives WHERE status != 'completed' ORDER BY updated_at DESC"
        ).fetchall()
    return [Initiative(**dict(row)) for row in rows]


def get_recent_initiatives(
    limit: int = 10,
    db_path: Path | None = None,
) -> list[Initiative]:
    """Most recently kicked-off initiatives, newest first.

    Resolves `db_path` lazily (mirrors `get_recent_decisions`/`get_recent_advice`)
    so callers and tests pick up a monkeypatched/live `DB_PATH`, unlike
    `list_initiatives`'s eager default. Ordered by `created_at` DESC — the
    activity rail surfaces these as "kicked off initiative" events.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "SELECT * FROM initiatives ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [Initiative(**dict(row)) for row in rows]


def list_decisions(db_path: Path = DB_PATH) -> list[Decision]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY timestamp DESC"
        ).fetchall()
    return [Decision(**dict(row)) for row in rows]


def list_initiatives(db_path: Path = DB_PATH) -> list[Initiative]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM initiatives ORDER BY updated_at DESC"
        ).fetchall()
    return [Initiative(**dict(row)) for row in rows]


def list_advice(db_path: Path = DB_PATH) -> list[Advice]:
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM advice_given ORDER BY timestamp DESC"
        ).fetchall()
    return [Advice(**dict(row)) for row in rows]


def get_decision(decision_id: int, db_path: Path = DB_PATH) -> Decision | None:
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    return Decision(**dict(row)) if row else None


def get_initiative(initiative_id: int, db_path: Path = DB_PATH) -> Initiative | None:
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM initiatives WHERE id = ?", (initiative_id,)
        ).fetchone()
    return Initiative(**dict(row)) if row else None


def get_advice(advice_id: int, db_path: Path = DB_PATH) -> Advice | None:
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM advice_given WHERE id = ?", (advice_id,)
        ).fetchone()
    return Advice(**dict(row)) if row else None


def update_decision(
    decision_id: int,
    *,
    domain: str | None = None,
    summary: str | None = None,
    rationale: str | None = None,
    outcome: str | None = None,
    tags: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    fields: list[tuple[str, str]] = []
    if domain is not None:
        fields.append(("domain", domain))
    if summary is not None:
        fields.append(("summary", summary))
    if rationale is not None:
        fields.append(("rationale", rationale))
    if outcome is not None:
        fields.append(("outcome", outcome))
    if tags is not None:
        fields.append(("tags", tags))
    if not fields:
        return get_decision(decision_id, db_path) is not None
    set_clause = ", ".join(f"{name} = ?" for name, _ in fields)
    values = [value for _, value in fields] + [decision_id]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE decisions SET {set_clause} WHERE id = ?", values
        )
        return cursor.rowcount > 0


def update_initiative(
    initiative_id: int,
    *,
    title: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    fields: list[tuple[str, str]] = []
    if title is not None:
        fields.append(("title", title))
    if status is not None:
        fields.append(("status", status))
    if summary is not None:
        fields.append(("summary", summary))
    if not fields:
        return get_initiative(initiative_id, db_path) is not None
    fields.append(("updated_at", datetime.now(UTC).isoformat()))
    set_clause = ", ".join(f"{name} = ?" for name, _ in fields)
    values = [value for _, value in fields] + [initiative_id]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE initiatives SET {set_clause} WHERE id = ?", values
        )
        return cursor.rowcount > 0


def update_advice(
    advice_id: int,
    *,
    domain: str | None = None,
    query_summary: str | None = None,
    advice_summary: str | None = None,
    db_path: Path = DB_PATH,
) -> bool:
    fields: list[tuple[str, str]] = []
    if domain is not None:
        fields.append(("domain", domain))
    if query_summary is not None:
        fields.append(("query_summary", query_summary))
    if advice_summary is not None:
        fields.append(("advice_summary", advice_summary))
    if not fields:
        return get_advice(advice_id, db_path) is not None
    set_clause = ", ".join(f"{name} = ?" for name, _ in fields)
    values = [value for _, value in fields] + [advice_id]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE advice_given SET {set_clause} WHERE id = ?", values
        )
        return cursor.rowcount > 0


def delete_decision(decision_id: int, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
        return cursor.rowcount > 0


def delete_initiative(initiative_id: int, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM initiatives WHERE id = ?", (initiative_id,))
        return cursor.rowcount > 0


def delete_advice(advice_id: int, db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM advice_given WHERE id = ?", (advice_id,))
        return cursor.rowcount > 0


# "__internal__" is reserved for system-generated actions (department
# cadences, workflow steps) that bypass outbound message dispatch.
_VALID_SCHEDULED_CHANNELS = {
    "email", "telegram", "slack_dm", "discord_dm", "__internal__",
}
_MAX_INTENT_CHARS = 2000

# Channels that participate in outbound→inbound DM context linkage. The 1:1 DM
# channels plus email — email replies arrive in a fresh per-thread session, so
# without a linkage a reply to mail the Executive sent during some other session
# (e.g. web chat) loses the originating context, exactly like the DM channels.
# "__internal__" is excluded — it never reaches a human.
_VALID_OUTBOUND_CHANNELS = {"discord_dm", "telegram", "slack_dm", "email"}
# Outbound DM bodies can be long; keep enough to reconstruct intent while
# bounding storage + the prompt-injection surface re-injected into the reply
# turn.
_MAX_OUTBOUND_CONTEXT_CHARS = 2000


def _resolve_db_path(db_path: Path | None) -> Path:
    """Return the caller's path or the current module-level DB_PATH.

    Reading DB_PATH dynamically (not via default-arg binding) lets tests
    monkeypatch `openexecutive.memory.episodic.DB_PATH` and have it actually
    take effect — default arguments capture the value at def time.
    """
    return db_path if db_path is not None else DB_PATH


_VALID_INSERT_STATUSES = {"pending", "done"}


def insert_scheduled_action(
    *,
    run_at: str,
    channel: str,
    channel_ref: str,
    intent_text: str,
    originating_session_id: str | None = None,
    department: str = "",
    kind: str = "ad_hoc",
    assigned_to_person_id: int | None = None,
    awaiting_response_since: datetime | None = None,
    scope_key: str | None = None,
    required_scope: str | None = None,
    status: str = "pending",
    db_path: Path | None = None,
) -> int:
    if channel not in _VALID_SCHEDULED_CHANNELS:
        raise ValueError(f"Unknown channel {channel!r}")
    # Only `pending` (the normal queue path) and `done` (a completed action
    # being recorded for the activity feed, e.g. a direct send) are allowed
    # at insert time. `running`/`failed`/`cancelled` are transitions managed
    # by the scheduler runner, never an initial state.
    if status not in _VALID_INSERT_STATUSES:
        raise ValueError(f"insert status {status!r} not in {sorted(_VALID_INSERT_STATUSES)}")
    # Bound intent_text length — both a storage DoS guard and a prompt-injection
    # surface-area reduction (it is re-injected into the Executive synthetic
    # turn at fire time).
    if len(intent_text) > _MAX_INTENT_CHARS:
        intent_text = intent_text[:_MAX_INTENT_CHARS]
    now = datetime.now(UTC).isoformat()
    awaiting_str = awaiting_response_since.isoformat() if awaiting_response_since else None
    with _get_conn(_resolve_db_path(db_path)) as conn:
        cursor = conn.execute(
            "INSERT INTO scheduled_actions "
            "(created_at, run_at, channel, channel_ref, intent_text, originating_session_id, "
            "department, kind, assigned_to_person_id, awaiting_response_since, scope_key, "
            "required_scope, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now, run_at, channel, channel_ref, intent_text, originating_session_id,
                department, kind, assigned_to_person_id, awaiting_str, scope_key,
                required_scope, status,
            ),
        )
        return int(cursor.lastrowid or 0)


def insert_outbound_context(
    *,
    channel: str,
    channel_ref: str,
    outbound_text: str,
    originating_session_id: str | None = None,
    recipient_person_id: int | None = None,
    outbound_message_id: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Record an outbound DM so a later reply from this recipient can recover
    the originating conversation's context. Returns the new row id.

    ``outbound_text`` is bounded to ``_MAX_OUTBOUND_CONTEXT_CHARS``. The row is
    created ``open``; the first matched reply consumes it.
    """
    if channel not in _VALID_OUTBOUND_CHANNELS:
        raise ValueError(
            f"Unknown outbound channel {channel!r} (expected one of "
            f"{sorted(_VALID_OUTBOUND_CHANNELS)})"
        )
    if len(outbound_text) > _MAX_OUTBOUND_CONTEXT_CHARS:
        outbound_text = outbound_text[:_MAX_OUTBOUND_CONTEXT_CHARS]
    now = datetime.now(UTC).isoformat()
    with _get_conn(_resolve_db_path(db_path)) as conn:
        cursor = conn.execute(
            "INSERT INTO outbound_context "
            "(created_at, channel, channel_ref, recipient_person_id, "
            "outbound_text, originating_session_id, outbound_message_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (
                now, channel, channel_ref, recipient_person_id,
                outbound_text, originating_session_id, outbound_message_id,
            ),
        )
        return int(cursor.lastrowid or 0)


def find_open_outbound_context(
    *,
    channel: str,
    channel_ref: str,
    within: timedelta,
    db_path: Path | None = None,
) -> OutboundContext | None:
    """Return the most-recent ``open`` outbound linkage for this recipient
    within ``within`` of now, or None.

    Most-recent-wins: if oe sent several DMs to the same person, the latest
    open one is assumed to be what they're replying to. Older ones age out of
    the window. Returns None (not an error) when the DB file doesn't exist yet.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None
    cutoff = (datetime.now(UTC) - within).isoformat()
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT * FROM outbound_context "
            "WHERE channel = ? AND channel_ref = ? AND status = 'open' "
            "  AND created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (channel, channel_ref, cutoff),
        ).fetchone()
    return OutboundContext(**dict(row)) if row else None


def mark_outbound_context_consumed(
    context_id: int, db_path: Path | None = None
) -> bool:
    """Flip an outbound linkage to ``consumed`` (one-shot).

    The ``AND status = 'open'`` guard makes this race-safe: two near-
    simultaneous replies can't both consume the same row — only the writer
    whose UPDATE affects a row (``rowcount > 0``) wins and should inject.
    """
    with _get_conn(_resolve_db_path(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE outbound_context "
            "SET status = 'consumed', consumed_at = ? "
            "WHERE id = ? AND status = 'open'",
            (datetime.now(UTC).isoformat(), context_id),
        )
        return cursor.rowcount > 0


def scope_key_in_use(scope_key: str, db_path: Path | None = None) -> bool:
    """Return True if a live scheduled action already exists for ``scope_key``.

    "Live" = any ``pending`` / ``running`` / ``done`` row (so a one-shot that has
    already fired still counts — re-enqueuing would duplicate it); ``cancelled``
    and ``failed`` rows are excluded so a fresh attempt can replace them. This is
    a single indexed lookup (``idx_scheduled_scope_key``) across ALL such rows —
    use it for idempotency instead of scanning ``list_scheduled_actions`` (which
    is capped at 100 and omits the ``running`` status).
    """
    if not scope_key:
        return False
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE scope_key = ? AND status IN ('pending', 'running', 'done') "
            "LIMIT 1",
            (scope_key,),
        ).fetchone()
    return row is not None


def count_nudges_for_scope(scope_key: str, db_path: Path | None = None) -> int:
    """Delivered (``done``) nudges ever emitted for ``scope_key``.

    Backs the per-scope cap in the nudge engine so the same stalled item is
    not chased forever; ``cancelled`` / ``failed`` rows never count.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions "
            "WHERE scope_key = ? AND status = 'done' AND kind = 'proactive_nudge'",
            (scope_key,),
        ).fetchone()
    return int(row["n"]) if row else 0


def recent_nudge_for_scope(
    scope_key: str,
    since: datetime,
    db_path: Path | None = None,
) -> bool:
    """Return True if a nudge for `scope_key` should be considered live.

    "Live" means either:
      - Any row in ``pending`` or ``running`` — undelivered, regardless
        of age. This catches the deferred-nudge case: a scan that
        scheduled a nudge with ``run_at`` far in the future has an open
        commitment for that scope and a follow-up scan must NOT emit
        a duplicate just because ``created_at`` is older than the
        cooldown.
      - A ``done`` row created on or after ``since`` — within the
        per-source cooldown.

    ``cancelled`` and ``failed`` rows are intentionally excluded so a
    cancelled or permanently failed nudge can be replaced by a fresh
    attempt.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return False
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduled_actions "
            "WHERE scope_key = ? AND ("
            "    status IN ('pending', 'running') "
            "    OR (status = 'done' AND created_at >= ?) "
            "  ) "
            "LIMIT 1",
            (scope_key, since.isoformat()),
        ).fetchone()
    return row is not None


def count_pending_for_channel_ref(
    channel: str, channel_ref: str, db_path: Path | None = None
) -> int:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions "
            "WHERE channel = ? AND channel_ref = ? AND status = 'pending'",
            (channel, channel_ref),
        ).fetchone()
    return int(row["n"]) if row else 0


def count_pending_global(db_path: Path | None = None) -> int:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions WHERE status = 'pending'"
        ).fetchone()
    return int(row["n"]) if row else 0


def count_activity_by_day(
    days: int, db_path: Path | None = None
) -> list[tuple[str, int]]:
    """Daily counts of self-initiated Executive activity for the Pulse heatmap.

    Same source set as `today._build_activity`: the union of recorded
    decisions, advice given, *fired* scheduled actions (status='done'),
    completed workflow runs, initiatives, resolved gated decisions, and raised
    alerts. Internal-channel rows and the `nudge_scan` heartbeat are excluded —
    they have no user-visible side effect, so they would inflate the
    "heartbeat" without representing anything the user did or saw.

    Buckets by calendar day (UTC) via `substr(...,1,10)`. Decisions/advice
    bucket by their `timestamp` (when they happened); fired actions bucket by
    `run_at` (when they fired), NOT `created_at` (when they were queued) — a
    cadence queued the night before should count on the day it actually fires,
    so the "Beats today" tile stays truthful. (This is the schema-light version
    of the fix `_build_activity` defers: there is no `done_at` column, but for a
    done action `run_at` is the closest persisted proxy for fire time.)
    Workflow runs bucket by `updated_at` (completion), initiatives by
    `created_at`, resolved decisions by `resolved_at`, alerts by `created_at`.
    Returns `[(YYYY-MM-DD, count), ...]` ascending, covering only the last
    `days` days (cutoff = today − (days−1), inclusive). Days with zero activity
    are simply absent — the caller fills the dense grid.

    The `workflow_runs` and `alerts` tables are created by other stores (not
    `initialize_db`), so each UNION arm is included only when its table exists
    — a DB initialised by `episodic` alone still counts the sources it owns
    without raising "no such table".
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    cutoff = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
    # (table, SQL fragment) in display order. Fragment day-expression already
    # filtered to the window via `>= ?`; one cutoff param is bound per arm.
    arms = [
        ("decisions",
         "SELECT substr(timestamp, 1, 10) AS day FROM decisions"
         " WHERE substr(timestamp, 1, 10) >= ?"),
        ("advice_given",
         "SELECT substr(timestamp, 1, 10) AS day FROM advice_given"
         " WHERE substr(timestamp, 1, 10) >= ?"),
        ("scheduled_actions",
         "SELECT substr(run_at, 1, 10) AS day FROM scheduled_actions"
         " WHERE status = 'done' AND channel != '__internal__'"
         " AND kind != 'nudge_scan' AND substr(run_at, 1, 10) >= ?"),
        ("workflow_runs",
         "SELECT substr(updated_at, 1, 10) AS day FROM workflow_runs"
         " WHERE status = 'done' AND substr(updated_at, 1, 10) >= ?"),
        ("initiatives",
         "SELECT substr(created_at, 1, 10) AS day FROM initiatives"
         " WHERE substr(created_at, 1, 10) >= ?"),
        ("decision_instances",
         "SELECT substr(resolved_at, 1, 10) AS day FROM decision_instances"
         " WHERE resolved_at IS NOT NULL AND substr(resolved_at, 1, 10) >= ?"),
        # source != 'decision_scheduling' mirrors `_build_activity`'s exclusion
        # (decision_ledger.DECISION_ALERT_SOURCE) so the heatmap matches the
        # feed: a resolved gated booking is counted once (decision_instances
        # arm), not twice (its companion alert would otherwise also count).
        ("alerts",
         "SELECT substr(created_at, 1, 10) AS day FROM alerts"
         " WHERE source != 'decision_scheduling'"
         " AND substr(created_at, 1, 10) >= ?"),
    ]
    with _get_conn(resolved) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        included = [frag for table, frag in arms if table in present]
        if not included:
            return []
        # Safe to interpolate: every fragment is a static literal from `arms`
        # above (no user input); only the cutoff is parameterised, one per arm.
        union = " UNION ALL ".join(included)
        rows = conn.execute(
            f"SELECT day, COUNT(*) AS n FROM ({union}) GROUP BY day ORDER BY day",
            [cutoff] * len(included),
        ).fetchall()
    return [(str(row["day"]), int(row["n"])) for row in rows]


def recent_sends_for_channel_ref(
    channel: str, channel_ref: str, since_iso: str, db_path: Path | None = None
) -> list[tuple[str, str]]:
    """Return ``(created_at, intent_text)`` for completed sends to a recipient.

    Reads the ``status='done'`` rows that ``_record_send_to_activity`` writes for
    every direct send (and that the scheduler marks for delivered followups), so
    it doubles as an outbound-delivery log for the anti-spam guard
    (``orchestrator.outbound_guard``). Newest first. Only rows with
    ``created_at >= since_iso`` (an ISO-8601 string) are returned, so the caller
    bounds the scan to its rate/dedup window. Returns ``[]`` when the DB does not
    yet exist.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "SELECT created_at, intent_text FROM scheduled_actions "
            "WHERE channel = ? AND channel_ref = ? AND status = 'done' "
            "AND created_at >= ? ORDER BY created_at DESC",
            (channel, channel_ref, since_iso),
        ).fetchall()
    return [(str(r["created_at"]), str(r["intent_text"])) for r in rows]


def requeue_orphaned_running(db_path: Path | None = None) -> int:
    """Flip any 'running' rows back to 'pending'.

    Call on scheduler startup. Rows are claimed via UPDATE…RETURNING into the
    'running' state and only flipped to 'done' or 'failed' after dispatch
    completes. A crash mid-dispatch would otherwise leave them stuck forever.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return 0
    with _get_conn(resolved) as conn:
        cursor = conn.execute(
            "UPDATE scheduled_actions SET status = 'pending' WHERE status = 'running'"
        )
        return int(cursor.rowcount)


def claim_due_actions(
    now: datetime, db_path: Path | None = None, limit: int = 20
) -> list[ScheduledAction]:
    """Claim pending actions whose run_at has passed.

    Uses UPDATE … RETURNING to flip claimed rows to 'running' in the same
    statement so the same row cannot be re-claimed by a subsequent tick.
    Within one process this is race-free. For multi-process safety SQLite
    serialises writers, so concurrent claimers will see SQLITE_BUSY rather
    than double-claiming — but assume single-worker for now.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "UPDATE scheduled_actions "
            "SET status = 'running', attempts = attempts + 1 "
            "WHERE id IN ("
            "  SELECT id FROM scheduled_actions "
            "  WHERE status = 'pending' AND run_at <= ? "
            "  ORDER BY run_at LIMIT ?"
            ") "
            "RETURNING *",
            (now.isoformat(), limit),
        ).fetchall()
    return [ScheduledAction(**dict(row)) for row in rows]


def reschedule_action(
    action_id: int, new_run_at: datetime, db_path: Path | None = None
) -> bool:
    """Set a new run_at and return the action to 'pending'.

    Called by the authority gate when an approver is outside their
    availability window — the action is deferred to ``new_run_at``
    rather than discarded or sent immediately.

    We undo the `attempts` increment that `claim_due_actions` applied when it
    claimed this row. Without the decrement, repeated deferrals would exhaust
    `max_attempts` before the action ever dispatched, causing permanent failure
    on the first real dispatch attempt.  `MAX(0, attempts-1)` is safe even if
    `attempts` is somehow already 0.
    """
    with _get_conn(_resolve_db_path(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE scheduled_actions "
            "SET status = 'pending', run_at = ?, attempts = MAX(0, attempts - 1) "
            "WHERE id = ? AND status = 'running'",
            (new_run_at.isoformat(), action_id),
        )
        return cursor.rowcount > 0


def mark_action_done(action_id: int, db_path: Path | None = None) -> bool:
    with _get_conn(_resolve_db_path(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE scheduled_actions SET status = 'done', last_error = '' WHERE id = ?",
            (action_id,),
        )
        return cursor.rowcount > 0


def mark_action_failed_or_retry(
    action_id: int,
    error: str,
    *,
    max_attempts: int = 3,
    db_path: Path | None = None,
) -> str:
    """Either reschedule with backoff (pending) or give up (failed). Returns new status."""
    with _get_conn(_resolve_db_path(db_path)) as conn:
        row = conn.execute(
            "SELECT attempts FROM scheduled_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempts"])
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE scheduled_actions SET status = 'failed', last_error = ? WHERE id = ?",
                (error[:500], action_id),
            )
            return "failed"
        # Backoff: 30s, 5m, 30m for attempts 1, 2, 3.
        backoff_seconds = {1: 30, 2: 300, 3: 1800}.get(attempts, 1800)
        new_run_at = (datetime.now(UTC) + timedelta(seconds=backoff_seconds)).isoformat()
        conn.execute(
            "UPDATE scheduled_actions "
            "SET status = 'pending', run_at = ?, last_error = ? WHERE id = ?",
            (new_run_at, error[:500], action_id),
        )
        return "pending"


def cancel_scheduled_action(action_id: int, db_path: Path | None = None) -> str:
    """Soft-cancel a pending action. Returns the resulting status or 'not_cancellable'."""
    with _get_conn(_resolve_db_path(db_path)) as conn:
        row = conn.execute(
            "SELECT status FROM scheduled_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return "not_found"
        current = row["status"]
        if current != "pending":
            return "not_cancellable"
        conn.execute(
            "UPDATE scheduled_actions SET status = 'cancelled' WHERE id = ?",
            (action_id,),
        )
        return "cancelled"


# Intent-text shapes of the ad-hoc reminders the removed talent /
# staff-onboarding workflows scheduled on the principal's real DM channel.
# Nothing creates these any more, but rows pending from before the removal
# would keep firing (for weeks, in the outreach case) about candidates whose
# records are unreachable. They were `kind="ad_hoc"` with `department=''` on
# a live channel, so the scheduler's `__internal__` drain never sees them.
# Each pattern is the full generated template up to its first free-text
# field — deliberately NOT a bare prefix like "Outreach reminder %", which
# would also catch a reminder the Executive phrased that way for the
# principal's own work. (`talent.reminders`, `workflows.candidate_outreach`,
# `interview_coordination`, `reference_check`, `new_hire_onboarding`,
# `talent.offers` at the pre-removal commit.)
_ORPHANED_TALENT_REMINDER_PATTERNS: tuple[str, ...] = (
    "Outreach reminder %/% (%) for the % search (%). Send the principal %",
    "Interview coordination for % on the % search (%). Send the principal %",
    "Reference checks (%) for % on the % search (%). Send the principal %",
    "Onboarding check-in (day %) for %, % at %. DM the principal %",
    "Offer expiry reminder: the offer to % for the % role (offer %) %. DM the principal %",
)
_TALENT_REMINDER_SWEEP = "2026-09-cancel-talent-reminders"


def cancel_orphaned_talent_reminders(db_path: Path | None = None) -> int:
    """One-shot sweep: cancel reminders left by the removed talent and
    staff-onboarding features. Returns the number of rows cancelled.

    Bounded by ``app_migrations``: the marker row is claimed FIRST with
    ``INSERT OR IGNORE`` inside the same transaction as the sweep, so of two
    processes booting against one DB exactly one does the work and the other
    is a clean no-op (no ``IntegrityError``). Every later call is a no-op even
    if a matching row appears afterwards. ``running`` rows are included: the
    scheduler's ``requeue_orphaned_running`` runs AFTER this sweep at boot and
    would otherwise resurrect a crash-orphaned row as ``pending``. Delete this
    function (and its callers) in the release after next, once every install
    has booted on it once.
    """
    with _get_conn(_resolve_db_path(db_path)) as conn:
        claimed = conn.execute(
            "INSERT OR IGNORE INTO app_migrations (name, applied_at) VALUES (?, ?)",
            (_TALENT_REMINDER_SWEEP, datetime.now(UTC).isoformat()),
        )
        if claimed.rowcount == 0:
            return 0
        shape = " OR ".join(
            "intent_text LIKE ?" for _ in _ORPHANED_TALENT_REMINDER_PATTERNS
        )
        cur = conn.execute(
            "UPDATE scheduled_actions "
            "SET status = 'cancelled', "
            "    awaiting_response_since = NULL, "
            "    last_error = 'cancelled: talent/staff-onboarding feature removed' "
            "WHERE status IN ('pending', 'running') "
            "  AND kind = 'ad_hoc' AND department = '' "
            f"  AND ({shape})",
            _ORPHANED_TALENT_REMINDER_PATTERNS,
        )
        # Delivered talent reminders still marked as awaiting a reply would
        # keep feeding the nudge engine ("chase the open commitment …") about
        # a dead candidate. Clear the flag; leave the rows as history.
        conn.execute(
            "UPDATE scheduled_actions SET awaiting_response_since = NULL "
            "WHERE awaiting_response_since IS NOT NULL AND status = 'done' "
            "  AND kind = 'ad_hoc' AND department = '' "
            f"  AND ({shape})",
            _ORPHANED_TALENT_REMINDER_PATTERNS,
        )
        return int(cur.rowcount)


def get_scheduled_action(
    action_id: int, db_path: Path | None = None
) -> ScheduledAction | None:
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None
    with _get_conn(resolved) as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_actions WHERE id = ?", (action_id,)
        ).fetchone()
    return ScheduledAction(**dict(row)) if row else None


def list_scheduled_actions(
    *,
    status: str | None = None,
    limit: int = 100,
    order: str = "asc",
    db_path: Path | None = None,
) -> list[ScheduledAction]:
    """List scheduled actions, optionally filtered by status.

    `order` sorts by `run_at`: "asc" (default) puts the soonest-due pending
    rows first — the right default for the upcoming queue; "desc" puts the
    most-recent rows first, which is what a *terminal*-status view (done /
    failed / cancelled) wants so recent history isn't pushed past `limit` by
    old rows. The direction is mapped to a SQL literal from a fixed set — the
    raw `order` string never reaches the query.
    """
    direction = "DESC" if order == "desc" else "ASC"
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    with _get_conn(resolved) as conn:
        if status is None:
            rows = conn.execute(
                f"SELECT * FROM scheduled_actions ORDER BY run_at {direction} LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM scheduled_actions WHERE status = ? ORDER BY run_at {direction} LIMIT ?",
                (status, limit),
            ).fetchall()
    return [ScheduledAction(**dict(row)) for row in rows]


# Scheduled-action kinds/channels that are internal plumbing, not user-facing
# commitments — excluded from the briefing's "In flight" list (and mirrored by
# the activity feed's own filter in api/routes/today.py:_build_activity).
_INTERNAL_ACTION_CHANNEL = "__internal__"
_INTERNAL_ACTION_KINDS = frozenset({"nudge_scan", "alert_review_scan"})


def list_pending_scheduled_actions(
    *,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[ScheduledAction]:
    """User-facing pending scheduled actions, soonest first.

    These are the Executive's in-flight commitments — follow-ups it will run,
    nudges it will send — that the briefing surfaces so the principal can see
    "what's about to happen" without digging into the Scheduled Actions page.
    Excludes internal plumbing (the ``__internal__`` channel used by cadences /
    research scans / brief generators, and the ``nudge_scan`` heartbeat), the
    same set the activity feed hides.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return []
    placeholders = ",".join("?" * len(_INTERNAL_ACTION_KINDS))
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_actions "
            "WHERE status = 'pending' "
            "  AND channel != ? "
            f"  AND kind NOT IN ({placeholders}) "
            "ORDER BY run_at LIMIT ?",
            (_INTERNAL_ACTION_CHANNEL, *_INTERNAL_ACTION_KINDS, limit),
        ).fetchall()
    return [ScheduledAction(**dict(row)) for row in rows]


def list_awaiting_replies_by_person(
    *,
    db_path: Path | None = None,
) -> dict[int, tuple[int, str | None]]:
    """Per-person count of open commitments where we're awaiting their reply.

    An "open commitment" is a scheduled_action with a non-null
    ``awaiting_response_since`` that hasn't been answered — the same set the
    proactive-nudge engine chases (see scheduler/nudge_engine.py): rows in
    ``pending``/``done`` excluding the nudge follow-ups themselves. Returns
    ``{person_id: (count, oldest_awaiting_response_since_iso)}``. The oldest
    timestamp is a lexicographic MIN, which is correct because every value is
    a UTC ``isoformat()`` string written by insert_scheduled_action.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return {}
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "SELECT assigned_to_person_id AS pid, COUNT(*) AS cnt, "
            "MIN(awaiting_response_since) AS oldest "
            "FROM scheduled_actions "
            "WHERE awaiting_response_since IS NOT NULL "
            "  AND assigned_to_person_id IS NOT NULL "
            "  AND status IN ('pending', 'done') "
            "  AND kind != 'proactive_nudge' "
            "GROUP BY assigned_to_person_id"
        ).fetchall()
    return {int(r["pid"]): (int(r["cnt"]), r["oldest"]) for r in rows}


def last_contact_at_by_person(
    *,
    db_path: Path | None = None,
) -> dict[int, str]:
    """Per-person timestamp of the most recent outbound we actually sent.

    Keyed off ``status='done'`` scheduled_actions assigned to the person, so a
    still-pending action does not count as contact. Uses ``created_at`` (queue
    time) — there is no ``done_at`` column; for ad-hoc follow-ups the two are
    minutes apart, the same approximation the activity feed makes. Returns
    ``{person_id: last_created_at_iso}``.
    """
    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return {}
    with _get_conn(resolved) as conn:
        rows = conn.execute(
            "SELECT assigned_to_person_id AS pid, MAX(created_at) AS last "
            "FROM scheduled_actions "
            "WHERE assigned_to_person_id IS NOT NULL "
            "  AND status = 'done' "
            "GROUP BY assigned_to_person_id"
        ).fetchall()
    return {int(r["pid"]): r["last"] for r in rows}


def format_for_prompt(
    db_path: Path = DB_PATH,
    max_chars: int = 2500,
    session_id: str = "",
) -> str:
    """Render recent decisions, active initiatives, and recent advice for prompt injection.

    When `session_id` is non-empty, decisions and advice are scoped to that
    session only — used by Discord/Telegram/Slack/email thread handlers so
    each conversation sees its own extracted context rather than a global mix
    from unrelated conversations. Initiatives are always global (company-wide).

    Output is bounded by `max_chars`. When over budget, oldest advice is dropped
    first, then oldest decisions. Initiatives are always kept.
    """
    decisions = get_recent_decisions(limit=5, db_path=db_path, session_id=session_id)
    initiatives = get_active_initiatives(db_path=db_path)
    advice_items = get_recent_advice(limit=2, db_path=db_path, session_id=session_id)

    if not decisions and not initiatives and not advice_items:
        return ""

    decision_lines: list[str] = []
    for d in decisions:
        date = d.timestamp[:10]
        line = f"- {date} [{d.domain}]: {d.summary}"
        if d.outcome:
            line += f" (Outcome: {d.outcome})"
        decision_lines.append(line)

    # Defensive cap on rendered initiatives. Even with extraction-side
    # dedup, an undisciplined run can sprawl — keep the prompt bounded
    # and let the overflow show as a single "+N more" line so the model
    # knows there's context it isn't seeing.
    _MAX_INITIATIVES_RENDERED = 10
    initiative_lines: list[str] = [
        f"- {i.title} [{i.status}]: {i.summary}"
        for i in initiatives[:_MAX_INITIATIVES_RENDERED]
    ]
    if len(initiatives) > _MAX_INITIATIVES_RENDERED:
        initiative_lines.append(
            f"- (+{len(initiatives) - _MAX_INITIATIVES_RENDERED} more active initiatives)"
        )

    advice_lines: list[str] = [
        f"- [{a.domain}]: {a.advice_summary}" for a in advice_items
    ]

    def _render(d_lines: list[str], i_lines: list[str], a_lines: list[str]) -> str:
        parts: list[str] = []
        if d_lines:
            parts.append("Recent decisions:")
            parts.extend(d_lines)
        if i_lines:
            parts.append("\nActive initiatives:")
            parts.extend(i_lines)
        if a_lines:
            parts.append("\nRecent strategic advice:")
            parts.extend(a_lines)
        return "\n".join(parts)

    # Drop oldest advice first, then oldest decisions; initiatives are always kept.
    rendered = _render(decision_lines, initiative_lines, advice_lines)
    while len(rendered) > max_chars and advice_lines:
        advice_lines.pop()
        rendered = _render(decision_lines, initiative_lines, advice_lines)
    while len(rendered) > max_chars and decision_lines:
        decision_lines.pop()
        rendered = _render(decision_lines, initiative_lines, advice_lines)

    return rendered


_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "store_memories",
    "description": (
        "Extract and store decisions, initiatives, and strategic advice from this conversation turn. "
        "High bar: most turns produce empty arrays for all three categories. "
        "Only fill an array when a future session would clearly suffer without that item. "
        "When in doubt, leave the array empty. "
        "Source attribution: only extract items the USER originated in the USER QUESTION block. "
        "Prescriptive prose in the EXECUTIVE RESPONSE — including confident 'we will / we should / "
        "the team will X' framing — is a proposal, not a commitment, and is never extracted unless "
        "the USER explicitly endorses it in their own text. "
        "STRUCTURAL RULE: every item MUST include a `user_commitment_quote` — verbatim text "
        "copied from the USER QUESTION block that is a declarative commitment (not a question). "
        "If you cannot locate such a quote in the USER QUESTION text, do NOT include the item. "
        "Quotes are validated against the user text after extraction; fabricated or paraphrased "
        "quotes will cause the item to be dropped."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "description": (
                    "Concrete decisions the user explicitly committed to. Speculation, options still under "
                    "consideration, and 'we should probably' are NOT decisions — leave empty in those cases. "
                    "Most turns produce no decisions. The commitment language must come from the USER block; "
                    "the executive proposing 'we should X' or 'the team will Y' is a proposal, not a decision."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "enum": ["strategy", "finance", "hr", "legal", "operations", "marketing", "product", "board", "general"]},
                        "summary": {"type": "string", "description": "One sentence: what was decided."},
                        "rationale": {"type": "string", "description": "Why this decision was made. Leave empty if not stated."},
                        "user_commitment_quote": {
                            "type": "string",
                            "description": (
                                "Verbatim text from the USER QUESTION block that constitutes the user's "
                                "commitment to this decision. Must be a declarative statement "
                                "(e.g. 'we're going with X', 'let's kill Y', 'decided: Z'), NOT a question "
                                "or a hypothetical. Copy the user's exact wording — paraphrases will be "
                                "rejected by the post-extraction validator."
                            ),
                        },
                    },
                    "required": ["domain", "summary", "user_commitment_quote"],
                },
            },
            "initiatives": {
                "type": "array",
                "description": (
                    "Named, ongoing projects the user is actively running or just kicked off. "
                    "Generic activities like 'improve marketing' are NOT initiatives — there must be a "
                    "concrete project name or scope. Most turns produce no initiatives. "
                    "If the conversation touches an initiative listed under EXISTING ACTIVE INITIATIVES "
                    "in the user turn, use that exact existing title verbatim — do NOT rephrase or "
                    "create a variant. Only invent a new title for a genuinely new project that does "
                    "not already exist in that list. "
                    "The initiative must come from the USER block; the executive proposing 'we should "
                    "kick off project X' is a recommendation, not an initiative."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short title for the initiative (3-6 words). REUSE an existing title from the list when applicable."},
                        "status": {"type": "string", "enum": ["active", "paused", "completed", "planned"]},
                        "summary": {"type": "string", "description": "One sentence describing the initiative and its goal."},
                        "user_commitment_quote": {
                            "type": "string",
                            "description": (
                                "Verbatim text from the USER QUESTION block where the user names "
                                "or kicks off this initiative. Must be a declarative statement, "
                                "not a question. Copy the user's exact wording — paraphrases will "
                                "be rejected by the post-extraction validator."
                            ),
                        },
                    },
                    "required": ["title", "status", "summary", "user_commitment_quote"],
                },
            },
            "advice": {
                "type": "array",
                "description": (
                    "Company-specific guidance tied to a named situation at THIS company. "
                    "Universal frameworks, general principles, and anything that would still make sense for a "
                    "random other company (e.g. 'cash flow is king', 'validate before building') are NEVER advice. "
                    "Only store advice that would lose meaning if the company name were removed. "
                    "Most turns produce no advice — leave empty unless the guidance is unmistakably "
                    "tied to this company's specifics. "
                    "Store only advice the USER explicitly asked about or endorsed; the executive's "
                    "own recommendations in the EXECUTIVE RESPONSE are proposals, not durable advice "
                    "to persist across sessions."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "enum": ["strategy", "finance", "hr", "legal", "operations", "marketing", "product", "board", "general"]},
                        "query_summary": {"type": "string", "description": "What the user was asking about (one sentence)."},
                        "advice_summary": {"type": "string", "description": "The core advice given (one to two sentences)."},
                        "user_commitment_quote": {
                            "type": "string",
                            "description": (
                                "Verbatim text from the USER QUESTION block where the user names "
                                "this specific company situation as one worth durable advice — "
                                "e.g. 'we're navigating X with Y constraint'. Must be a declarative "
                                "statement, not a question. Copy the user's exact wording — "
                                "paraphrases will be rejected by the post-extraction validator."
                            ),
                        },
                    },
                    "required": ["domain", "query_summary", "advice_summary", "user_commitment_quote"],
                },
            },
        },
        "required": ["decisions", "initiatives", "advice"],
    },
}

_EXTRACTION_SYSTEM = (
    "You are a memory extraction assistant. Most conversation turns produce NO memories — "
    "empty arrays are the default and expected output. Only extract an item when a future session "
    "would clearly suffer without it.\n"
    "\n"
    "Source attribution (read this first): only extract items the USER originated in the "
    "USER QUESTION block. Prescriptive prose in the EXECUTIVE RESPONSE — including confident "
    "'we will / we should / the team will X' framing — is a proposal, not a commitment, and is "
    "never a decision, initiative, or advice unless the USER explicitly endorses it in their "
    "own text. The executive's recommendations are the output of the conversation; they only "
    "become memory when the user accepts them.\n"
    "\n"
    "Required for every item: a `user_commitment_quote` field with verbatim text copied from "
    "the USER QUESTION block. The quote must be a declarative commitment — NOT a question, "
    "NOT a hypothetical, NOT a paraphrase. If you cannot find such a quote in the USER QUESTION "
    "text, do not include the item at all. A deterministic post-extraction validator will "
    "drop any item whose quote does not appear verbatim in the USER QUESTION or whose quote "
    "ends in a question mark — fabricating quotes wastes a tool call.\n"
    "\n"
    "Required signal for each category:\n"
    "- Decision: the user explicitly committed to a specific course of action "
    "(e.g. 'we'll hire 3 engineers in Q3', 'we're killing project X'). "
    "Speculation, options under consideration, and 'we should probably' are NOT decisions.\n"
    "- Initiative: a named, ongoing project the user is actively running or just kicked off. "
    "Generic activities ('improve marketing') are NOT initiatives — there must be a concrete project name or scope.\n"
    "- Advice: company-specific guidance tied to a named situation at THIS company. "
    "Universal frameworks, general principles, and anything that would still make sense for a "
    "random other company are NEVER advice.\n"
    "When in doubt, extract nothing. Saving a marginal item is worse than missing one — "
    "the user can always restate it. Never extract from clarifying questions, small talk, or exploratory discussion."
)


# Fold iOS / macOS smart-quote auto-substitution to ASCII so the LLM (which
# emits straight quotes by default) can still match a user message that
# came from an Apple device. Without this, a perfectly legitimate
# "We're going with X" commitment is silently dropped on every iPhone.
_SMART_PUNCT_FOLDS = str.maketrans({
    "‘": "'",  # ‘
    "’": "'",  # ’
    "“": '"',  # “
    "”": '"',  # ”
})


def _normalize_for_quote_match(s: str) -> str:
    """Case-fold + collapse horizontal whitespace + fold smart punctuation
    + treat newlines as soft sentence terminators.

    Newlines get rewritten to `. ` BEFORE whitespace collapse so the
    forward-scan validator sees a real terminator there. A user who hits
    Return between a commitment and a follow-up question — a very common
    chat shape — should get the commitment stored, not rejected because
    the scanner walked across the newline straight to the `?`.

    English-only — `?`, `.`, `!` are the recognized terminators. CJK
    fullwidth equivalents (`。`, `？`, `！`) are not handled; the codebase
    has no multilingual extraction path.
    """
    s = s.translate(_SMART_PUNCT_FOLDS).lower()
    s = s.replace("\n", ". ")
    return " ".join(s.split())


def _is_valid_user_commitment(quote: str, user_message: str) -> bool:
    """Validate that an extracted commitment quote is real, declarative, and
    actually present in the user's text.

    Rules, all must hold:
      1. The quote is non-empty after stripping.
      2. The quote contains NO '?' anywhere — a quote that includes a
         question mark isn't a clean declarative commitment.
      3. SOME occurrence of the quote (normalized) appears in the user
         message (normalized) followed by an accepting terminator. Walk
         every occurrence in order. For each, scan forward: '.' or '!'
         (or end of message) accepts and the whole quote is valid; '?'
         rejects this occurrence and we try the next one. Iterating all
         occurrences handles user self-correction in the same turn —
         "Should we kill X? Yes, kill X." — where the FIRST occurrence
         sits inside a question but the SECOND is a real commitment.

    Normalization (`_normalize_for_quote_match`): lowercase, smart
    punctuation folded to ASCII, newlines rewritten to '. ', remaining
    whitespace collapsed.
    """
    if not quote:
        return False
    stripped = quote.strip()
    if not stripped:
        return False
    if "?" in stripped:
        return False

    nq = _normalize_for_quote_match(stripped)
    nm = _normalize_for_quote_match(user_message)
    if not nq:
        return False

    search_from = 0
    while True:
        pos = nm.find(nq, search_from)
        if pos == -1:
            # No remaining occurrence had an accepting terminator.
            return False
        for ch in nm[pos + len(nq):]:
            if ch in ".!":
                return True
            if ch == "?":
                # This occurrence is inside a question — try the next one.
                break
        else:
            # Ran off the end of the message without seeing '?' — accept.
            return True
        search_from = pos + 1


_MAX_INPUT_CHARS = 20_000  # cap each side to avoid runaway cost

# Minimum combined chars (user + assistant) required to schedule extraction.
# Below this floor, the turn is a clarifying question or small-talk exchange
# and the LLM gate's cost isn't justified. Both stream_chat and the committee
# path use this constant — keeping them symmetric prevents the committee path
# from firing 2.5x more often than streaming, which it used to.
MIN_TURN_CHARS_FOR_EXTRACTION = 1500


async def extract_and_store(
    user_message: str,
    assistant_response: str,
    db_path: Path = DB_PATH,
    session_id: str = "",
) -> None:
    """Extract memorable items from a conversation turn and persist them.

    `session_id` is forwarded to store_decision/store_advice so the stored
    rows can later be scoped back to this conversation via format_for_prompt.
    Runs as a background task — never blocks the response stream.
    """
    try:
        from openexecutive.audit.usage import log_model_usage
        from openexecutive.config import get_settings
        from openexecutive.providers import get_provider

        routing_model = get_settings().routing_model

        # Show the LLM what initiatives already exist so it can reuse a
        # canonical title instead of inventing a slight rephrase. Without
        # this the same real-world project ("AI Opportunity Assessment")
        # ends up stored as 9 separate rows under variant names because
        # store_initiative's upsert is exact-string match only.
        #
        # Cap to the 30 most-recently-updated rows to bound per-turn cost
        # on a database that is already sprawled — draining duplicates is
        # the job of the consolidate-initiatives CLI, not this block.
        _MAX_EXISTING_TITLES = 30
        active = get_active_initiatives(db_path=db_path)[:_MAX_EXISTING_TITLES]
        if active:
            existing_block = "EXISTING ACTIVE INITIATIVES (reuse the title verbatim if this turn touches one — do NOT rephrase or create a variant):\n" + "\n".join(
                f"- {i.title}" for i in active
            ) + "\n\n"
        else:
            existing_block = ""

        response = await get_provider(routing_model).messages_create(
            model=routing_model,
            max_tokens=1024,
            system=_EXTRACTION_SYSTEM,
            tools=[_EXTRACTION_TOOL],
            tool_choice={"type": "auto"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{existing_block}"
                        f"USER QUESTION:\n{user_message[:_MAX_INPUT_CHARS]}\n\n"
                        f"EXECUTIVE RESPONSE:\n{assistant_response[:_MAX_INPUT_CHARS]}"
                    ),
                }
            ],
        )

        log_model_usage(response, model=routing_model, actor="memory_extractor")

        for block in response.content:
            if block.type != "tool_use" or block.name != "store_memories":
                continue

            inp = block.input
            # Validate every item against the user's actual text. The LLM has
            # repeatedly proven willing to log the executive's recommendations
            # as if the user had committed to them — see the May 27 incident
            # where "Should we scope as fixed POC or hourly?" produced a
            # decision "First $30K deal will be structured as fixed POC".
            # The user_commitment_quote field + this validator are the
            # hard gate that catches that pattern; the prompt is now just
            # the soft instruction layer.
            for d in inp.get("decisions", []):
                domain = d.get("domain", "general")
                summary = d.get("summary", "")
                quote = d.get("user_commitment_quote", "")
                if not summary:
                    continue
                if not _is_valid_user_commitment(quote, user_message):
                    logger.debug(
                        "Dropping decision — invalid user_commitment_quote %r (summary=%r)",
                        quote[:120],
                        summary[:80],
                    )
                    continue
                store_decision(
                    domain=domain,
                    summary=summary,
                    rationale=d.get("rationale", ""),
                    session_id=session_id,
                    db_path=db_path,
                )
            for i in inp.get("initiatives", []):
                title = i.get("title", "")
                status = i.get("status", "active")
                summary = i.get("summary", "")
                quote = i.get("user_commitment_quote", "")
                if not (title and summary):
                    continue
                if not _is_valid_user_commitment(quote, user_message):
                    logger.debug(
                        "Dropping initiative — invalid user_commitment_quote %r (title=%r)",
                        quote[:120],
                        title[:80],
                    )
                    continue
                store_initiative(title=title, status=status, summary=summary, db_path=db_path)
            for a in inp.get("advice", []):
                domain = a.get("domain", "general")
                query_summary = a.get("query_summary", "")
                advice_summary = a.get("advice_summary", "")
                quote = a.get("user_commitment_quote", "")
                if not (query_summary and advice_summary):
                    continue
                if not _is_valid_user_commitment(quote, user_message):
                    logger.debug(
                        "Dropping advice — invalid user_commitment_quote %r (query=%r)",
                        quote[:120],
                        query_summary[:80],
                    )
                    continue
                store_advice(
                    domain=domain,
                    query_summary=query_summary,
                    advice_summary=advice_summary,
                    session_id=session_id,
                    db_path=db_path,
                )

    except Exception:
        logger.exception("Episodic memory extraction failed — skipping silently")


def schedule_extraction(
    user_message: str,
    assistant_response: str,
    session_id: str = "",
) -> None:
    """Fire-and-forget extraction. Safe to call from sync or async context.

    Pass `session_id` to tag extracted decisions and advice with the
    originating conversation so format_for_prompt can scope them later.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            extract_and_store(user_message, assistant_response, session_id=session_id)
        )
        # Hold a strong reference so GC cannot cancel the task mid-flight.
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        # No running event loop (CLI context) — run in a daemon thread.
        threading.Thread(
            target=lambda: asyncio.run(
                extract_and_store(user_message, assistant_response, session_id=session_id)
            ),
            daemon=True,
        ).start()
