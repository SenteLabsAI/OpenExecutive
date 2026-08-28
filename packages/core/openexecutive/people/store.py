"""SQLite-backed persistence for the People feature.

Lives in the same `episodic_memory.db` as alerts/episodic/departments so
there is one place to look. All tables are created idempotently via
`CREATE TABLE IF NOT EXISTS` — no migration tooling needed.

The `_resolve_db_path` pattern lets tests monkeypatch `DB_PATH` and have
it take effect at call time, mirroring `departments.store`.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    Person,
    PreferredChannel,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))


def _resolve_db_path(db_path: Path | None) -> Path:
    """Return caller-supplied path or the current module-level DB_PATH.

    Dynamic resolution allows tests to monkeypatch DB_PATH and have it
    take effect at call time rather than at def time.
    """
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def initialize_db(db_path: Path | None = None) -> None:
    """Create People tables idempotently. Call before departments init."""
    with _get_conn(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                is_principal INTEGER NOT NULL DEFAULT 0,
                department_slugs_json TEXT NOT NULL DEFAULT '[]',
                email TEXT,
                slack_user_id TEXT,
                telegram_chat_id TEXT,
                discord_user_id TEXT,
                preferred_channel TEXT NOT NULL DEFAULT 'any',
                response_sla_hours INTEGER NOT NULL DEFAULT 24,
                on_leave_until TEXT,
                reports_to_person_id INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (reports_to_person_id) REFERENCES people(id)
            );
            CREATE INDEX IF NOT EXISTS idx_people_principal
                ON people(is_principal) WHERE is_principal = 1;
            CREATE INDEX IF NOT EXISTS idx_people_archived
                ON people(archived);

            CREATE TABLE IF NOT EXISTS person_authority_scope (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                scope_token TEXT NOT NULL,
                UNIQUE(person_id, scope_token),
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pas_person
                ON person_authority_scope(person_id);
            CREATE INDEX IF NOT EXISTS idx_pas_scope
                ON person_authority_scope(scope_token);

            CREATE TABLE IF NOT EXISTS person_availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                weekdays_json TEXT NOT NULL DEFAULT '[]',
                start_local TEXT NOT NULL,
                end_local TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pa_person
                ON person_availability(person_id);
        """)
        # Additive migration: discord_user_id added after initial schema.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(people)")}
        if "discord_user_id" not in cols:
            try:
                conn.execute("ALTER TABLE people ADD COLUMN discord_user_id TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise


# --------------------------------------------------------------------------- #
# Row mapping helpers
# --------------------------------------------------------------------------- #

def _load_scope(person_id: int, conn: sqlite3.Connection) -> list[AuthorityScope]:
    rows = conn.execute(
        "SELECT scope_token FROM person_authority_scope WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    scopes: list[AuthorityScope] = []
    for row in rows:
        try:
            scopes.append(AuthorityScope(row["scope_token"]))
        except ValueError:
            logger.warning("people: unknown scope_token=%r for person_id=%d", row["scope_token"], person_id)
    return scopes


def _load_availability(
    person_id: int, conn: sqlite3.Connection
) -> list[AvailabilityWindow]:
    rows = conn.execute(
        "SELECT weekdays_json, start_local, end_local, timezone"
        " FROM person_availability WHERE person_id = ? ORDER BY id",
        (person_id,),
    ).fetchall()
    windows: list[AvailabilityWindow] = []
    for row in rows:
        try:
            weekdays = json.loads(row["weekdays_json"]) or []
            windows.append(
                AvailabilityWindow(
                    weekdays=list(weekdays),
                    start_local=row["start_local"],
                    end_local=row["end_local"],
                    timezone=row["timezone"],
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning("people: malformed availability row for person_id=%d", person_id)
    return windows


def _row_to_person(row: sqlite3.Row, conn: sqlite3.Connection) -> Person:
    person_id = int(row["id"])
    try:
        dept_slugs: list[str] = json.loads(row["department_slugs_json"]) or []
    except (ValueError, TypeError):
        dept_slugs = []
    on_leave: date | None = None
    if row["on_leave_until"]:
        with contextlib.suppress(ValueError):
            on_leave = date.fromisoformat(row["on_leave_until"])
    return Person(
        id=person_id,
        full_name=row["full_name"],
        role=row["role"],
        is_principal=bool(row["is_principal"]),
        department_slugs=dept_slugs,
        email=row["email"],
        slack_user_id=row["slack_user_id"],
        telegram_chat_id=row["telegram_chat_id"],
        discord_user_id=row["discord_user_id"],
        preferred_channel=row["preferred_channel"],
        response_sla_hours=int(row["response_sla_hours"]),
        on_leave_until=on_leave,
        reports_to_person_id=row["reports_to_person_id"],
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        authority_scope=_load_scope(person_id, conn),
        availability=_load_availability(person_id, conn),
    )


# --------------------------------------------------------------------------- #
# People CRUD
# --------------------------------------------------------------------------- #

def upsert_person(
    *,
    full_name: str,
    role: str = "",
    is_principal: bool = False,
    department_slugs: list[str] | None = None,
    email: str | None = None,
    slack_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    discord_user_id: str | None = None,
    preferred_channel: PreferredChannel = "any",
    response_sla_hours: int = 24,
    on_leave_until: date | None = None,
    reports_to_person_id: int | None = None,
    person_id: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert or update a Person row. Returns the person_id."""
    now = _now()
    dept_json = json.dumps(department_slugs or [])
    leave_str = on_leave_until.isoformat() if on_leave_until else None

    with _get_conn(db_path) as conn:
        if is_principal and person_id is None:
            # Serialize bootstrap principal creation across API workers. The
            # route layer also rejects a second principal, but onboarding can
            # run concurrently before a principal exists.
            conn.execute("BEGIN IMMEDIATE")
            existing_principal = conn.execute(
                "SELECT 1 FROM people WHERE is_principal = 1 AND archived = 0 LIMIT 1"
            ).fetchone()
            if existing_principal is not None:
                raise ValueError("An active principal is already configured")
        if person_id is not None and person_id > 0:
            conn.execute(
                """
                UPDATE people SET
                    full_name=?, role=?, is_principal=?, department_slugs_json=?,
                    email=?, slack_user_id=?, telegram_chat_id=?, discord_user_id=?,
                    preferred_channel=?,
                    response_sla_hours=?, on_leave_until=?, reports_to_person_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    full_name, role, int(is_principal), dept_json,
                    email, slack_user_id, telegram_chat_id, discord_user_id,
                    preferred_channel,
                    response_sla_hours, leave_str, reports_to_person_id,
                    now, person_id,
                ),
            )
            return person_id
        cursor = conn.execute(
            """
            INSERT INTO people
                (full_name, role, is_principal, department_slugs_json,
                 email, slack_user_id, telegram_chat_id, discord_user_id,
                 preferred_channel,
                 response_sla_hours, on_leave_until, reports_to_person_id,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                full_name, role, int(is_principal), dept_json,
                email, slack_user_id, telegram_chat_id, discord_user_id,
                preferred_channel,
                response_sla_hours, leave_str, reports_to_person_id,
                now, now,
            ),
        )
        return int(cursor.lastrowid or 0)


def set_authority_scope(
    person_id: int,
    scopes: list[AuthorityScope],
    db_path: Path | None = None,
) -> None:
    """Replace the full authority scope list for a person."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM person_authority_scope WHERE person_id = ?", (person_id,)
        )
        for scope in scopes:
            conn.execute(
                "INSERT OR IGNORE INTO person_authority_scope (person_id, scope_token)"
                " VALUES (?, ?)",
                (person_id, scope.value),
            )


def set_availability(
    person_id: int,
    windows: list[AvailabilityWindow],
    db_path: Path | None = None,
) -> None:
    """Replace the full availability window list for a person."""
    with _get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM person_availability WHERE person_id = ?", (person_id,)
        )
        for win in windows:
            conn.execute(
                "INSERT INTO person_availability"
                " (person_id, weekdays_json, start_local, end_local, timezone)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    person_id,
                    json.dumps(win.weekdays),
                    win.start_local,
                    win.end_local,
                    win.timezone,
                ),
            )


def get_person(person_id: int, db_path: Path | None = None) -> Person | None:
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def find_person_by_slack_id(slack_user_id: str, db_path: Path | None = None) -> Person | None:
    """Return the first non-archived Person with this Slack user id, or None."""
    if not slack_user_id or not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE slack_user_id = ? AND archived = 0 LIMIT 1",
            (slack_user_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def find_person_by_telegram_chat_id(telegram_chat_id: str, db_path: Path | None = None) -> Person | None:
    """Return the first non-archived Person with this Telegram chat id, or None."""
    if not telegram_chat_id or not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE telegram_chat_id = ? AND archived = 0 LIMIT 1",
            (telegram_chat_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def find_person_by_email(email: str, db_path: Path | None = None) -> Person | None:
    """Return the first non-archived Person with this email address, or None.

    Case-insensitive match — IMAP `From:` headers come back with the
    sender's chosen capitalization, which isn't necessarily what was
    stored on the Person row.
    """
    if not email or not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        # The episodic DB file is shared across subsystems; it may exist before
        # the people DDL has run. Treat a missing table like a missing file so
        # email intake (and other callers) degrade to "unknown sender" rather
        # than crashing.
        if not _table_exists(conn, "people"):
            return None
        row = conn.execute(
            "SELECT * FROM people WHERE LOWER(email) = LOWER(?) AND archived = 0 "
            "ORDER BY is_principal DESC, id ASC LIMIT 1",
            (email,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def find_person_by_discord_id(discord_user_id: str, db_path: Path | None = None) -> Person | None:
    """Return the first non-archived Person with this Discord user id, or None."""
    if not discord_user_id or not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE discord_user_id = ? AND archived = 0 LIMIT 1",
            (discord_user_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def find_person_by_channel_ref(
    channel: str, channel_ref: str, db_path: Path | None = None
) -> Person | None:
    """Map a scheduled-action (channel, channel_ref) to a non-archived Person.

    Dispatches to the per-channel finder. ``channel`` uses the scheduled-action
    vocabulary (``slack_dm`` / ``discord_dm`` / ``telegram`` / ``email``); email
    refs may carry a ``address|thread_id`` suffix, so only the address is matched.
    Returns None for an unknown channel or any miss. Single source of truth for
    the outbound guard and the outbound-context linkage, which both need to
    resolve a DM recipient back to a Person.
    """
    finder = {
        "slack_dm": find_person_by_slack_id,
        "discord_dm": find_person_by_discord_id,
        "telegram": find_person_by_telegram_chat_id,
        "email": find_person_by_email,
    }.get(channel)
    if finder is None:
        return None
    ref = channel_ref.split("|", 1)[0] if channel == "email" else channel_ref
    # Forward db_path only when explicitly given so the common default-DB path
    # calls the finder with a single positional argument (matching how the
    # finders are normally invoked across the codebase).
    return finder(ref) if db_path is None else finder(ref, db_path)


def find_principal_person(db_path: Path | None = None) -> Person | None:
    """Return the principal Person (the operator running this instance), or None.

    The web `/chat` endpoint has no per-user auth — it's gated by a shared
    secret and is the principal's terminal. Resolving web turns to this
    row lets them flow into Honcho under the same person_id as the
    principal's Discord/email/Slack/Telegram traffic.

    Tie-break: the schema does not enforce a single is_principal row, so
    if onboarding ran twice (or someone toggled the flag) multiple rows
    may match. ``ORDER BY id`` makes the oldest principal win
    deterministically — a stale row would route web traffic to the
    wrong peer card. If you re-run onboarding, archive the old
    principal first.
    """
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE is_principal = 1 AND archived = 0 "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _row_to_person(row, conn)


def list_people(
    include_archived: bool = False,
    db_path: Path | None = None,
) -> list[Person]:
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM people ORDER BY is_principal DESC, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM people WHERE archived = 0"
                " ORDER BY is_principal DESC, id"
            ).fetchall()
        return [_row_to_person(row, conn) for row in rows]


def archive_person(person_id: int, db_path: Path | None = None) -> bool:
    """Soft-delete a person. Returns True if a row was found and archived."""
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE people SET archived = 1, updated_at = ? WHERE id = ? AND archived = 0",
            (_now(), person_id),
        )
        return cursor.rowcount > 0


def find_approvers(
    scope: AuthorityScope,
    db_path: Path | None = None,
) -> list[Person]:
    """Return non-archived people who can approve the given scope token.

    Includes:
    - People who hold the exact scope token.
    - People who hold WILDCARD (approves everything).

    Sort order: non-principals first (prefer delegated humans over the
    fallback principal), then by response_sla_hours ASC (fastest SLA first).
    """
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.*
            FROM people p
            JOIN person_authority_scope pas ON pas.person_id = p.id
            WHERE p.archived = 0
              AND pas.scope_token IN (?, ?)
            ORDER BY p.is_principal ASC, p.response_sla_hours ASC, p.id ASC
            """,
            (scope.value, AuthorityScope.WILDCARD.value),
        ).fetchall()
        return [_row_to_person(row, conn) for row in rows]


def update_person(
    person_id: int,
    *,
    full_name: str | None = None,
    role: str | None = None,
    email: str | None = None,
    slack_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    discord_user_id: str | None = None,
    preferred_channel: PreferredChannel | None = None,
    response_sla_hours: int | None = None,
    on_leave_until: date | None = None,
    clear_on_leave: bool = False,
    reports_to_person_id: int | None = None,
    department_slugs: list[str] | None = None,
    db_path: Path | None = None,
) -> bool:
    """Partial update. Returns True if a row was modified.

    Pass `clear_on_leave=True` to explicitly set on_leave_until to NULL.
    """
    fields: list[tuple[str, object]] = []
    if full_name is not None:
        fields.append(("full_name", full_name))
    if role is not None:
        fields.append(("role", role))
    if email is not None:
        fields.append(("email", email))
    if slack_user_id is not None:
        fields.append(("slack_user_id", slack_user_id))
    if telegram_chat_id is not None:
        fields.append(("telegram_chat_id", telegram_chat_id))
    if discord_user_id is not None:
        fields.append(("discord_user_id", discord_user_id))
    if preferred_channel is not None:
        fields.append(("preferred_channel", preferred_channel))
    if response_sla_hours is not None:
        fields.append(("response_sla_hours", response_sla_hours))
    if clear_on_leave:
        fields.append(("on_leave_until", None))
    elif on_leave_until is not None:
        fields.append(("on_leave_until", on_leave_until.isoformat()))
    if reports_to_person_id is not None:
        fields.append(("reports_to_person_id", reports_to_person_id))
    if department_slugs is not None:
        fields.append(("department_slugs_json", json.dumps(department_slugs)))
    if not fields:
        return get_person(person_id, db_path) is not None
    fields.append(("updated_at", _now()))
    set_clause = ", ".join(f"{n} = ?" for n, _ in fields)
    values = [v for _, v in fields] + [person_id]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE people SET {set_clause} WHERE id = ?", values
        )
        return cursor.rowcount > 0
