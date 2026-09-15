"""SQLite-backed persistence for departments + their Goals.

Lives in the same `episodic_memory.db` SQLite file as alerts/episodic/audit
so there is one place to look. Schema is created idempotently via
`CREATE TABLE IF NOT EXISTS` (no migration tooling in this repo).

Department-level data is read on every Executive turn (Phase 2 prompt block)
so the store must stay cheap; queries are unindexed for now (8 rows).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from openexecutive.departments.charters import (
    DEFAULT_CHECK_IN_CADENCE,
    DEFAULT_DEPARTMENTS,
)
from openexecutive.departments.models import (
    AuthorityLevel,
    DepartmentCharter,
    DepartmentConfig,
    DepartmentState,
    Goal,
    GoalStatus,
    PeriodType,
)
from openexecutive.utils.slug import DEPARTMENT_SLUG_FALLBACK, slugify

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))

# Sentinel used by update_department to distinguish "caller did not supply the
# field" (leave it alone) from "caller explicitly passed None" (clear it to
# SQL NULL). Python's None cannot play both roles simultaneously.
_UNSET: object = object()


def _resolve_db_path(db_path: Path | None) -> Path:
    """Return the caller's path or the current module-level DB_PATH.

    Reading DB_PATH dynamically (not via default-arg binding) lets tests
    monkeypatch `openexecutive.departments.store.DB_PATH` and have it actually
    take effect — default arguments capture the value at def time. Mirrors
    `openexecutive.memory.episodic._resolve_db_path`.
    """
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    # Foreign keys are off by default in SQLite; we don't rely on FK enforcement
    # but turning it on keeps invariants visible if a caller violates them.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slugify(title: str) -> str:
    return slugify(title, fallback=DEPARTMENT_SLUG_FALLBACK)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def initialize_db(db_path: Path | None = None) -> None:
    """Create the `departments` + `department_goals` tables if missing.

    Idempotent: safe to call on every boot. `department_members` is intentionally
    deferred until Phase 3 (People) lands — there is nothing useful to populate
    it with until People rows exist.

    Also migrates older databases:
      • `specialist_key` NOT NULL → nullable
      • `department_okrs` table + `quarter` column → `department_goals` table
        with `period_type` + `period_value`
    """
    with _get_conn(db_path) as conn:
        # Migrate-first ordering: rename the legacy department_okrs table before
        # the CREATE TABLE IF NOT EXISTS for department_goals, otherwise we end
        # up with both tables and data stranded on the old one.
        _migrate_okrs_to_goals(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS departments (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                specialist_key TEXT,
                charter_mission TEXT NOT NULL DEFAULT '',
                charter_scope_json TEXT NOT NULL DEFAULT '[]',
                charter_out_of_scope_json TEXT NOT NULL DEFAULT '[]',
                authority_level TEXT NOT NULL DEFAULT 'propose_only',
                head_person_id INTEGER,
                head_persona_slug TEXT,
                cadences_json TEXT NOT NULL DEFAULT '{}',
                headcount INTEGER,
                budget_usd REAL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS department_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department_slug TEXT NOT NULL,
                period_type TEXT NOT NULL DEFAULT 'quarter',
                period_value TEXT NOT NULL,
                key_result TEXT NOT NULL,
                target TEXT NOT NULL,
                current TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'on_track',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_reviewed_at TEXT,
                FOREIGN KEY (department_slug) REFERENCES departments(slug)
            );
            CREATE INDEX IF NOT EXISTS idx_goals_dept
                ON department_goals(department_slug);
            CREATE TABLE IF NOT EXISTS departments_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        _migrate_specialist_key_nullable(conn)
        _migrate_add_channel_columns(conn)
        _migrate_add_last_reviewed_at_column(conn)
        _migrate_add_watched_entities_column(conn)


def _migrate_add_last_reviewed_at_column(conn: sqlite3.Connection) -> None:
    """Add the Phase A `last_reviewed_at` column to `department_goals` if missing.

    Records the wall-clock when OE last graded a Goal (via the
    department_check_in workflow or, in Phase B, a chat-driven tool
    call). Distinct from `updated_at`, which bumps on content edits;
    keeping them separate lets the UI render an honest "Last reviewed
    Nd ago" without conflating a human edit with a review.

    Additive ALTER pattern mirrors `_migrate_add_channel_columns` — a
    duplicate-column error on concurrent boot is treated as success.
    Default is NULL (not ''); existing rows are read back via
    `_opt_col` and coerced to '' at the model boundary.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(department_goals)")}
    if "last_reviewed_at" in existing:
        return
    try:
        conn.execute("ALTER TABLE department_goals ADD COLUMN last_reviewed_at TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_add_channel_columns(conn: sqlite3.Connection) -> None:
    """Add the Shift 3 channel columns to `departments` if missing.

    Department-scoped broadcast channels (`slack_channel_id`,
    `discord_channel_id`, `telegram_chat_id`) let OE post to a department's
    team room as an alternative to DMing the head. Additive ALTER pattern
    mirrors the episodic.py migrations — duplicate-column errors on a
    concurrent boot race are treated as success.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(departments)")}
    for col in ("slack_channel_id", "discord_channel_id", "telegram_chat_id"):
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE departments ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _migrate_add_watched_entities_column(conn: sqlite3.Connection) -> None:
    """Add `watched_entities_json` to `departments` if missing.

    A JSON list of external entity names the department wants the research
    watch policy to treat as strong grounding (see
    `monitoring/research/watch_policy.grounding_vocabulary`). Additive
    ALTER pattern mirrors `_migrate_add_channel_columns`; the NOT NULL
    DEFAULT '[]' keeps `_row_to_config` free of a null branch.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(departments)")}
    if "watched_entities_json" in existing:
        return
    try:
        conn.execute(
            "ALTER TABLE departments ADD COLUMN watched_entities_json "
            "TEXT NOT NULL DEFAULT '[]'"
        )
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_okrs_to_goals(conn: sqlite3.Connection) -> None:
    """Rename department_okrs → department_goals; add period_type column.

    Idempotent: no-op once the rename + column-add has run. Handles three
    incremental states (older → newer):
      1. Old table department_okrs with a `quarter` column, no period_type.
      2. department_okrs has been partly migrated (period_type added or
         column renamed). Finish whatever's missing.
      3. Already-renamed table department_goals — no-op.

    SQLite supports ALTER TABLE RENAME COLUMN since 3.25 (Python 3.13's
    bundled SQLite is 3.45+). We do not need to toggle PRAGMA foreign_keys
    because no FK references `department_okrs.*` — the only FK is in the
    other direction (`department_slug` → `departments.slug`), and that
    survives a RENAME TABLE.
    """
    has_old_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='department_okrs'"
    ).fetchone() is not None
    if not has_old_table:
        return  # Fresh DB or already migrated

    logger.info("departments: migrating department_okrs → department_goals")

    cols = conn.execute("PRAGMA table_info(department_okrs)").fetchall()
    col_names = {c["name"] for c in cols}

    # Step 1: add period_type column if missing. NOT NULL with DEFAULT means
    # existing rows are backfilled atomically as 'quarter'.
    if "period_type" not in col_names:
        conn.execute(
            "ALTER TABLE department_okrs "
            "ADD COLUMN period_type TEXT NOT NULL DEFAULT 'quarter'"
        )

    # Step 2: rename `quarter` → `period_value` if needed.
    if "quarter" in col_names and "period_value" not in col_names:
        conn.execute(
            "ALTER TABLE department_okrs RENAME COLUMN quarter TO period_value"
        )

    # Step 3: drop the old index (carries an outdated name even after table
    # rename) and rename the table itself.
    conn.execute("DROP INDEX IF EXISTS idx_okrs_dept")
    conn.execute("ALTER TABLE department_okrs RENAME TO department_goals")


def _migrate_specialist_key_nullable(conn: sqlite3.Connection) -> None:
    """Drop NOT NULL on departments.specialist_key for older databases.

    SQLite cannot ALTER COLUMN to remove a constraint, so we rebuild the
    table when PRAGMA reports the old shape. No-op when the column is
    already nullable (notnull=0).

    SQLite enforces foreign keys against the *table name*, and dropping the
    referenced table while ``PRAGMA foreign_keys=ON`` raises an
    IntegrityError because ``department_goals.department_slug`` references
    ``departments(slug)``. SQLite's documented pattern for table rebuilds is
    to toggle foreign_keys OFF for the duration. ``PRAGMA foreign_keys`` is
    a no-op inside an open transaction, so the toggle must straddle BEGIN /
    COMMIT — we cannot put it in the same ``executescript`` block that opens
    the transaction.
    """
    cols = conn.execute("PRAGMA table_info(departments)").fetchall()
    specialist_col = next((c for c in cols if c["name"] == "specialist_key"), None)
    if specialist_col is None or specialist_col["notnull"] == 0:
        return
    logger.info("departments: migrating specialist_key column to nullable")
    # Ensure no transaction is open (defensive — the caller's contextmanager
    # may have started one). Toggle FKs off before the rebuild and back on
    # after; PRAGMA inside a transaction is silently ignored.
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript("""
            BEGIN;
            CREATE TABLE departments_new (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                specialist_key TEXT,
                charter_mission TEXT NOT NULL DEFAULT '',
                charter_scope_json TEXT NOT NULL DEFAULT '[]',
                charter_out_of_scope_json TEXT NOT NULL DEFAULT '[]',
                authority_level TEXT NOT NULL DEFAULT 'propose_only',
                head_person_id INTEGER,
                head_persona_slug TEXT,
                cadences_json TEXT NOT NULL DEFAULT '{}',
                headcount INTEGER,
                budget_usd REAL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO departments_new SELECT * FROM departments;
            DROP TABLE departments;
            ALTER TABLE departments_new RENAME TO departments;
            COMMIT;
        """)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# --------------------------------------------------------------------------- #
# Row mapping
# --------------------------------------------------------------------------- #

def _row_to_config(row: sqlite3.Row) -> DepartmentConfig:
    try:
        scope = json.loads(row["charter_scope_json"]) or []
    except (ValueError, TypeError):
        scope = []
    try:
        out_of_scope = json.loads(row["charter_out_of_scope_json"]) or []
    except (ValueError, TypeError):
        out_of_scope = []
    try:
        cadences = json.loads(row["cadences_json"]) or {}
    except (ValueError, TypeError):
        cadences = {}
    # A future migration or hand-edited DB row could carry an unknown
    # authority_level token; default to the safest level (propose_only) and
    # warn rather than 500ing every read of `list_departments`.
    try:
        authority_level = AuthorityLevel(row["authority_level"])
    except ValueError:
        logger.warning(
            "departments: unknown authority_level=%r on slug=%s — defaulting to propose_only",
            row["authority_level"], row["slug"],
        )
        authority_level = AuthorityLevel.PROPOSE_ONLY
    # Channel columns are nullable and were added via additive ALTER in
    # _migrate_add_channel_columns; row indexing via sqlite3.Row tolerates
    # missing columns by raising IndexError, so use getattr-style access.
    def _opt_col(name: str) -> str | None:
        try:
            return row[name]
        except (IndexError, KeyError):
            return None
    try:
        watched = json.loads(_opt_col("watched_entities_json") or "[]") or []
    except (ValueError, TypeError):
        watched = []
    return DepartmentConfig(
        slug=row["slug"],
        title=row["title"],
        specialist_key=row["specialist_key"],
        charter=DepartmentCharter(
            mission=row["charter_mission"],
            scope=list(scope) if isinstance(scope, list) else [],
            out_of_scope=(
                list(out_of_scope) if isinstance(out_of_scope, list) else []
            ),
        ),
        authority_level=authority_level,
        head_person_id=row["head_person_id"],
        head_persona_slug=row["head_persona_slug"],
        cadences=dict(cadences) if isinstance(cadences, dict) else {},
        slack_channel_id=_opt_col("slack_channel_id"),
        discord_channel_id=_opt_col("discord_channel_id"),
        telegram_chat_id=_opt_col("telegram_chat_id"),
        watched_entities=(
            [str(e) for e in watched if isinstance(e, str) and e.strip()]
            if isinstance(watched, list) else []
        ),
    )


def _row_to_goal(row: sqlite3.Row) -> Goal:
    # last_reviewed_at was added additively via _migrate_add_last_reviewed_at_column.
    # On a DB that hasn't been migrated yet (e.g. a sibling subsystem created the
    # file and we're reading before initialize_db runs), the column is absent and
    # sqlite3.Row raises IndexError. Coerce NULL → '' at this boundary so the
    # Pydantic model always sees a string.
    try:
        last_reviewed_at = row["last_reviewed_at"] or ""
    except (IndexError, KeyError):
        last_reviewed_at = ""
    return Goal(
        id=int(row["id"]),
        department_slug=row["department_slug"],
        period_type=row["period_type"],
        period_value=row["period_value"],
        key_result=row["key_result"],
        target=row["target"],
        current=row["current"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_reviewed_at=last_reviewed_at,
    )


def _row_to_state(row: sqlite3.Row, goals: list[Goal]) -> DepartmentState:
    return DepartmentState(
        config=_row_to_config(row),
        goals=goals,
        headcount=row["headcount"],
        budget_usd=row["budget_usd"],
        member_person_ids=[],  # populated in Phase 3
        updated_at=row["updated_at"],
    )


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #

_SEEDED_META_KEY = "default_departments_seeded"


def _defaults_seeded(conn: sqlite3.Connection) -> bool:
    """True once the default departments have been seeded (or adopted) for this DB."""
    row = conn.execute(
        "SELECT 1 FROM departments_meta WHERE key = ?", (_SEEDED_META_KEY,)
    ).fetchone()
    return row is not None


def _mark_defaults_seeded(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO departments_meta (key, value) VALUES (?, ?)",
        (_SEEDED_META_KEY, now),
    )


def seed_default_departments(db_path: Path | None = None) -> int:
    """Insert the 8 default departments on first run only.

    Seeding happens exactly once per database, tracked by the
    ``default_departments_seeded`` row in ``departments_meta``. After that
    first run the user owns the department list: departments they delete stay
    deleted across restarts, and the defaults are never re-inserted. Returns
    the number of rows newly inserted (8 on a fresh DB, 0 thereafter).

    A database that predates the sentinel but already holds departments is
    treated as already-seeded — its current state is adopted as the baseline
    so a one-time deploy of this code never resurrects rows the user removed.
    """
    now = _now()
    inserted = 0
    with _get_conn(db_path) as conn:
        if _defaults_seeded(conn):
            return 0
        existing = conn.execute("SELECT COUNT(*) AS n FROM departments").fetchone()["n"]
        if existing:
            # Pre-sentinel database with live rows: adopt the current set as
            # the seeded baseline instead of re-inserting deleted defaults.
            _mark_defaults_seeded(conn, now)
            return 0
        for slug, title, specialist_key, charter in DEFAULT_DEPARTMENTS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO departments
                  (slug, title, specialist_key,
                   charter_mission, charter_scope_json, charter_out_of_scope_json,
                   authority_level, cadences_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    title,
                    specialist_key,
                    charter.mission,
                    json.dumps(charter.scope),
                    json.dumps(charter.out_of_scope),
                    AuthorityLevel.PROPOSE_ONLY.value,
                    json.dumps({"check_in": DEFAULT_CHECK_IN_CADENCE}),
                    now,
                ),
            )
            if cursor.rowcount:
                inserted += 1
        _mark_defaults_seeded(conn, now)
    if inserted:
        logger.info("departments.seed inserted=%d", inserted)
    return inserted


# --------------------------------------------------------------------------- #
# Department CRUD
# --------------------------------------------------------------------------- #

def list_departments(db_path: Path | None = None) -> list[DepartmentState]:
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        # The episodic DB file is shared across subsystems; a sibling (audit,
        # voice personas) can create the file before initialize_db() runs the
        # departments DDL. Treat a missing table like a missing file so a
        # read never crashes a chat turn or the registry refresh.
        if not _table_exists(conn, "departments"):
            return []
        dept_rows = conn.execute(
            "SELECT * FROM departments ORDER BY slug"
        ).fetchall()
        goal_rows = conn.execute(
            "SELECT * FROM department_goals ORDER BY department_slug, id"
        ).fetchall()
    goals_by_slug: dict[str, list[Goal]] = {}
    for row in goal_rows:
        goals_by_slug.setdefault(row["department_slug"], []).append(_row_to_goal(row))
    return [
        _row_to_state(row, goals_by_slug.get(row["slug"], []))
        for row in dept_rows
    ]


def get_department(slug: str, db_path: Path | None = None) -> DepartmentState | None:
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM departments WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            return None
        goal_rows = conn.execute(
            "SELECT * FROM department_goals WHERE department_slug = ? ORDER BY id",
            (slug,),
        ).fetchall()
    return _row_to_state(row, [_row_to_goal(r) for r in goal_rows])


def update_department(
    slug: str,
    *,
    title: str | None = None,
    charter: DepartmentCharter | None = None,
    authority_level: AuthorityLevel | None = None,
    head_person_id: int | None | object = _UNSET,
    head_persona_slug: str | None = None,
    cadences: dict[str, str] | None = None,
    headcount: int | None = None,
    budget_usd: float | None = None,
    slack_channel_id: str | None | object = _UNSET,
    discord_channel_id: str | None | object = _UNSET,
    telegram_chat_id: str | None | object = _UNSET,
    watched_entities: list[str] | None = None,
    db_path: Path | None = None,
) -> bool:
    """Partial update. Returns True if a row was modified.

    Callers that want to clear a nullable field pass an explicit value; this
    helper does not distinguish "omitted" vs "set-to-None" because the route
    layer translates Pydantic's `exclude_unset` semantics for us.

    The three channel fields use the same _UNSET sentinel as head_person_id
    so the route can clear a previously-set channel by passing None
    explicitly (without _UNSET, "None == omitted" wouldn't let the caller
    wipe a stale channel id).
    """
    fields: list[tuple[str, object]] = []
    if title is not None:
        fields.append(("title", title))
    if charter is not None:
        fields.append(("charter_mission", charter.mission))
        fields.append(("charter_scope_json", json.dumps(charter.scope)))
        fields.append(("charter_out_of_scope_json", json.dumps(charter.out_of_scope)))
    if authority_level is not None:
        fields.append(("authority_level", authority_level.value))
    if head_person_id is not _UNSET:
        fields.append(("head_person_id", head_person_id))  # None → SQL NULL (clears)
    if head_persona_slug is not None:
        fields.append(("head_persona_slug", head_persona_slug))
    if cadences is not None:
        fields.append(("cadences_json", json.dumps(cadences)))
    if headcount is not None:
        fields.append(("headcount", headcount))
    if budget_usd is not None:
        fields.append(("budget_usd", budget_usd))
    if slack_channel_id is not _UNSET:
        fields.append(("slack_channel_id", slack_channel_id))
    if discord_channel_id is not _UNSET:
        fields.append(("discord_channel_id", discord_channel_id))
    if telegram_chat_id is not _UNSET:
        fields.append(("telegram_chat_id", telegram_chat_id))
    if watched_entities is not None:
        fields.append(("watched_entities_json", json.dumps(list(watched_entities))))
    if not fields:
        # Nothing to update; confirm the row exists so the route can 404.
        return get_department(slug, db_path) is not None
    fields.append(("updated_at", _now()))
    set_clause = ", ".join(f"{name} = ?" for name, _ in fields)
    values = [value for _, value in fields] + [slug]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE departments SET {set_clause} WHERE slug = ?", values
        )
        return cursor.rowcount > 0


# --------------------------------------------------------------------------- #
# Goal CRUD
# --------------------------------------------------------------------------- #

def list_goals(
    department_slug: str, db_path: Path | None = None
) -> list[Goal]:
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM department_goals WHERE department_slug = ? ORDER BY id",
            (department_slug,),
        ).fetchall()
    return [_row_to_goal(r) for r in rows]


def get_goal(goal_id: int, db_path: Path | None = None) -> Goal | None:
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM department_goals WHERE id = ?", (goal_id,)
        ).fetchone()
    return _row_to_goal(row) if row else None


def _mirror_goal_to_honcho(
    *,
    department_slug: str,
    period_type: str,
    period_value: str,
    key_result: str,
    target: str,
    current: str = "",
    status: str = "",
    transition: str = "created",
) -> None:
    """Best-effort mirror of a goal mutation into the dept peer's notes timeline.

    ``transition`` ∈ ``{"created", "updated", "reviewed"}``. ``reviewed``
    is emitted by ``record_goal_review`` when the check-in workflow (or,
    in Phase B, a chat-driven tool) grades the goal without editing its
    content. Wrapped to never raise: the SQL commit has already landed
    by the time this runs, and a Honcho failure must not break the goal
    mutation.
    """
    try:
        from openexecutive.memory.honcho_client import append_department_note

        body = (
            f"Goal {transition} ({period_type} {period_value}): {key_result}\n"
            f"Target: {target}"
        )
        if current:
            body += f"\nCurrent: {current}"
        if status:
            body += f"\nStatus: {status}"
        append_department_note(
            department_slug=department_slug,
            kind="goal_update",
            body=body,
        )
    except Exception:  # noqa: BLE001 - mirror must never break the SQL path.
        # Wrapper itself audits the failure; this swallow is the
        # final belt-and-suspenders if the import itself somehow blows up.
        pass


def insert_goal(
    department_slug: str,
    *,
    period_value: str,
    key_result: str,
    target: str,
    period_type: PeriodType = "quarter",
    current: str = "",
    status: GoalStatus = "on_track",
    db_path: Path | None = None,
) -> int:
    now = _now()
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO department_goals
              (department_slug, period_type, period_value, key_result, target, current, status,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (department_slug, period_type, period_value, key_result, target, current, status, now, now),
        )
        goal_id = int(cursor.lastrowid or 0)
    _mirror_goal_to_honcho(
        department_slug=department_slug,
        period_type=period_type,
        period_value=period_value,
        key_result=key_result,
        target=target,
        current=current,
        status=status,
        transition="created",
    )
    return goal_id


def update_goal(
    goal_id: int,
    *,
    period_type: PeriodType | None = None,
    period_value: str | None = None,
    key_result: str | None = None,
    target: str | None = None,
    current: str | None = None,
    status: GoalStatus | None = None,
    last_reviewed_at: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Update content fields on a goal row.

    Bumps `updated_at` whenever any content field changes (period/KR/target/
    current/status). The new `last_reviewed_at` kwarg (Phase B) lets a
    caller that's both editing content AND grading the goal in the same
    turn — e.g. the `update_department_goal` agent tool when a user says
    "we shipped the migration" — record both in one transaction without a
    double Honcho mirror.

    Note: a chat-driven status flip ("we lost the deal" → at_risk) counts
    as a content edit, NOT a passive cadence review — `status` is in the
    content fields list, so `updated_at` advances. Only a pure no-content
    call (`last_reviewed_at` set, every other field None) routes to
    `record_goal_review` and skips the `updated_at` bump — that's the
    sole path reserved for the daily-cadence specialist saying "yep,
    nothing changed, still on track."
    """
    fields: list[tuple[str, object]] = []
    if period_type is not None:
        fields.append(("period_type", period_type))
    if period_value is not None:
        fields.append(("period_value", period_value))
    if key_result is not None:
        fields.append(("key_result", key_result))
    if target is not None:
        fields.append(("target", target))
    if current is not None:
        fields.append(("current", current))
    if status is not None:
        fields.append(("status", status))
    if not fields:
        # No content edit. If the caller supplied last_reviewed_at, route
        # through record_goal_review instead so we don't bump updated_at.
        if last_reviewed_at is not None and status is None:
            # Need current status to call record_goal_review — fetch it.
            existing = get_goal(goal_id, db_path)
            if existing is None:
                return False
            return record_goal_review(
                goal_id,
                status=existing.status,
                last_reviewed_at=last_reviewed_at,
                db_path=db_path,
            )
        return get_goal(goal_id, db_path) is not None
    fields.append(("updated_at", _now()))
    if last_reviewed_at is not None:
        fields.append(("last_reviewed_at", last_reviewed_at))
    set_clause = ", ".join(f"{name} = ?" for name, _ in fields)
    values = [value for _, value in fields] + [goal_id]
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE department_goals SET {set_clause} WHERE id = ?", values
        )
        updated = cursor.rowcount > 0
    if updated:
        # Re-fetch the row so the mirror body reflects the merged state,
        # not just the deltas. Better recall than "<key> changed to <new>"
        # because the dept peer sees a complete restatement of the KR.
        goal = get_goal(goal_id, db_path)
        if goal is not None:
            _mirror_goal_to_honcho(
                department_slug=goal.department_slug,
                period_type=goal.period_type,
                period_value=goal.period_value,
                key_result=goal.key_result,
                target=goal.target,
                current=goal.current,
                status=goal.status,
                transition="updated",
            )
    return updated


def record_goal_review(
    goal_id: int,
    *,
    status: GoalStatus,
    last_reviewed_at: str,
    db_path: Path | None = None,
) -> bool:
    """Persist a status review without bumping ``updated_at``.

    Used by the ``department_check_in`` workflow (and, in Phase B, the
    ``update_department_goal`` agent tool when only status changes) to
    record OE's verdict on a Goal. Only ``status`` and ``last_reviewed_at``
    move — content edits go through ``update_goal``. Keeping the two
    paths distinct lets the UI render "Last reviewed Nd ago" honestly:
    a principal renaming the KR on Tuesday and OE re-grading it on
    Wednesday must be distinguishable.

    Always overwrites — the workflow re-grades every Goal each cycle,
    and the audit row carries the prior value for the diff trail. The
    Honcho mirror fires after the SQL commit with ``transition="reviewed"``
    so the dept peer's notes timeline captures cadence reviews
    distinctly from content edits.

    Returns True iff a row was updated.
    """
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE department_goals SET status = ?, last_reviewed_at = ? WHERE id = ?",
            (status, last_reviewed_at, goal_id),
        )
        updated = cursor.rowcount > 0
    if updated:
        goal = get_goal(goal_id, db_path)
        if goal is not None:
            _mirror_goal_to_honcho(
                department_slug=goal.department_slug,
                period_type=goal.period_type,
                period_value=goal.period_value,
                key_result=goal.key_result,
                target=goal.target,
                current=goal.current,
                status=goal.status,
                transition="reviewed",
            )
    return updated


def delete_goal(goal_id: int, db_path: Path | None = None) -> bool:
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM department_goals WHERE id = ?", (goal_id,)
        )
        return cursor.rowcount > 0


def create_department(
    title: str,
    *,
    mission: str = "",
    db_path: Path | None = None,
) -> DepartmentState:
    """Insert a new custom department and return its initial state.

    The slug is derived from the title. If a collision exists, a numeric suffix
    is appended (-2, -3, …). specialist_key is always NULL for user-created
    departments. Raises ValueError if title is blank after stripping.
    """
    title = title.strip()
    if not title:
        raise ValueError("Department title must not be blank")

    base_slug = _slugify(title)
    now = _now()

    with _get_conn(db_path) as conn:
        slug = base_slug
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM departments WHERE slug = ?", (slug,)
        ).fetchone():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        try:
            conn.execute(
                """
                INSERT INTO departments
                  (slug, title, specialist_key,
                   charter_mission, charter_scope_json, charter_out_of_scope_json,
                   authority_level, cadences_json, updated_at)
                VALUES (?, ?, NULL, ?, '[]', '[]', ?, ?, ?)
                """,
                (
                    slug,
                    title,
                    mission,
                    AuthorityLevel.PROPOSE_ONLY.value,
                    json.dumps({"check_in": DEFAULT_CHECK_IN_CADENCE}),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"A department with slug '{slug}' already exists"
            ) from exc

    state = get_department(slug, db_path)
    if state is None:
        raise RuntimeError(f"Department {slug!r} vanished immediately after insert")
    return state


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def delete_department(slug: str, db_path: Path | None = None) -> bool:
    """Delete a department and all its Goals. Returns True if deleted, False if not found.

    Goals are deleted explicitly first because the schema has no ON DELETE CASCADE clause.
    Pending ``dept_cadence`` scheduled actions for this department are cancelled in
    the same transaction so a deleted department stops firing check-in alerts. All
    three writes run together.

    Note: logical references to this slug in people.department_slugs_json and
    audit/proposal records are NOT cleaned up — they become orphaned text tags.
    Audit/proposal records are intentionally kept as history. People references
    should be swept if department cleanup becomes a frequent operation.
    """
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        # scheduled_actions lives in the same DB but is created by
        # memory.episodic.initialize_db, which may not have run in narrow
        # unit-test setups — guard so the cancel is a no-op when absent.
        if _table_exists(conn, "scheduled_actions"):
            conn.execute(
                "UPDATE scheduled_actions SET status = 'cancelled' "
                "WHERE kind = 'dept_cadence' AND department = ? AND status = 'pending'",
                (slug,),
            )
        conn.execute(
            "DELETE FROM department_goals WHERE department_slug = ?", (slug,)
        )
        cursor = conn.execute(
            "DELETE FROM departments WHERE slug = ?", (slug,)
        )
        return cursor.rowcount > 0


