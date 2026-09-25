"""Workspace settings: how this install is used, and in which time zone.

One row (``id = 1``) in the ``workspace_settings`` table of the episodic DB:

- ``mode`` — ``"team"`` (the default: a company with departments and people)
  or ``"solo"`` (one person using Open Executive just for themselves, so
  department check-ins do not run — see ``set_workspace_mode``).
- ``timezone`` — the user's IANA zone, or NULL to fall back to the
  ``USER_TIMEZONE`` setting (and then UTC). It drives the default times of
  the morning brief, end-of-day digest and reflection, the zone the Executive
  resolves "tomorrow at 9" in, open-loop due dates, and the alert quiet
  hours when no zone was stored for them.

The row lives in its own table rather than on ``CompanyProfile`` on purpose:
onboarding's commit and the form wizard rebuild the profile from scratch,
which would silently wipe it. It is per company (it swaps with a client slot
like the other company tables — see ``clients.slots._BLANK_WIPE_TABLES``).

Reads are always fresh (one small SQLite read) and never cached on a
``Session``, so a change applies to the very next turn. They are also
read-only: a DB file or table that does not exist yet reads as the defaults
and is never created by a read, so loops and tests against an unconfigured
DB leave no trace.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from openexecutive.orchestrator.session import Session

logger = logging.getLogger(__name__)

TABLE = "workspace_settings"

WorkspaceMode = Literal["solo", "team"]
WORKSPACE_MODES: tuple[str, ...] = get_args(WorkspaceMode)
DEFAULT_MODE: WorkspaceMode = "team"

# Longest IANA key is ~30 chars; anything far past that is not a zone name.
_MAX_TZ_LEN = 64

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'team',
    timezone TEXT,
    updated_at TEXT
)
"""


class WorkspaceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: WorkspaceMode = DEFAULT_MODE
    timezone: str | None = None


def _resolve_db_path(db_path: Path | None) -> Path:
    # Resolved at call time so tests that monkeypatch episodic.DB_PATH (and
    # EPISODIC_DB_PATH deployments) see the same file as scheduled_actions.
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_workspace_settings_db(db_path: Path | None = None) -> None:
    """Create the table if missing. Idempotent."""
    conn = _connect(_resolve_db_path(db_path))
    try:
        conn.execute(_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


def validate_timezone(tz: str | None) -> str | None:
    """Return ``tz`` as a known IANA zone name, or None for "not set".

    Blank means "clear". Raises ``ValueError`` for anything ``zoneinfo`` does
    not know — the same rule as the ``USER_TIMEZONE`` setting.
    """
    if tz is None:
        return None
    name = tz.strip()
    if not name:
        return None
    if len(name) > _MAX_TZ_LEN:
        raise ValueError("timezone is not a known IANA zone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"timezone {name!r} is not a known IANA zone") from exc
    return name


def get_workspace(db_path: Path | None = None) -> WorkspaceSettings:
    """The stored settings, or the defaults when nothing is stored.

    Never raises: a read error is logged and reads as the defaults, and a
    stored value that is no longer valid (a hand-edited mode, a zone this
    Python's tz database does not know) is ignored field by field.
    """
    path = _resolve_db_path(db_path)
    if not path.exists():
        return WorkspaceSettings()
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                f"SELECT mode, timezone FROM {TABLE} WHERE id = 1"  # noqa: S608 — constant table name
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            logger.exception("workspace: could not read settings — using defaults")
        return WorkspaceSettings()
    except sqlite3.Error:
        logger.exception("workspace: could not read settings — using defaults")
        return WorkspaceSettings()
    if row is None:
        return WorkspaceSettings()

    mode = row["mode"]
    if mode not in WORKSPACE_MODES:
        logger.warning("workspace: ignoring unknown stored mode %r", mode)
        mode = DEFAULT_MODE
    try:
        timezone = validate_timezone(row["timezone"])
    except ValueError:
        logger.warning("workspace: ignoring unknown stored timezone %r", row["timezone"])
        timezone = None
    return WorkspaceSettings(mode=mode, timezone=timezone)


def _configured_timezone() -> ZoneInfo:
    """The ``USER_TIMEZONE`` setting, else UTC."""
    try:
        from openexecutive.config import get_settings

        return ZoneInfo(get_settings().user_timezone)
    except Exception:
        return ZoneInfo("UTC")


def get_user_timezone(db_path: Path | None = None) -> ZoneInfo:
    """The zone in effect: the workspace's, else ``USER_TIMEZONE``, else UTC."""
    stored = get_workspace(db_path).timezone
    if stored:
        return ZoneInfo(stored)
    return _configured_timezone()


def effective_workspace_mode(session: Session | None = None) -> WorkspaceMode:
    """The mode a turn runs in: the session's override when it carries a valid
    one (evals run scenarios concurrently on one Executive, so they cannot
    flip the install-wide setting), else the workspace's."""
    override = session.workspace_mode if session is not None else None
    if override == "solo":
        return "solo"
    if override == "team":
        return "team"
    return get_workspace().mode


def _upsert(db_path: Path | None, **columns: str | None) -> None:
    """Write the given columns of the row (``mode`` / ``timezone``), leaving
    the others as they are. Creates the table and the row as needed."""
    unknown = set(columns) - {"mode", "timezone"}
    if unknown:
        raise ValueError(f"unknown workspace column(s): {sorted(unknown)}")
    names = list(columns)
    now = datetime.now(UTC).isoformat()
    col_list = ", ".join([*names, "updated_at"])
    placeholders = ", ".join("?" for _ in range(len(names) + 1))
    updates = ", ".join(f"{n} = excluded.{n}" for n in [*names, "updated_at"])
    conn = _connect(_resolve_db_path(db_path))
    try:
        conn.execute(_CREATE_SQL)
        conn.execute(
            f"INSERT INTO {TABLE} (id, {col_list}) VALUES (1, {placeholders}) "  # noqa: S608 — allowlisted columns
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            (*columns.values(), now),
        )
        conn.commit()
    finally:
        conn.close()


def set_timezone(tz: str | None) -> WorkspaceSettings:
    """Store the user's zone (None or blank clears it). Raises ``ValueError``
    for an unknown zone.

    When the zone in effect changes, the pending morning brief, end-of-day
    digest and reflection are re-timed to the new zone
    (``scheduler.runner.reschedule_principal_rhythm``); a brief that is
    running right now finishes and chains its successor in the new zone.
    The re-time is best-effort: if it fails, it is logged and each brief
    moves to the new zone after it next fires (every link reads the zone
    fresh), so the stored setting is never rolled back.
    """
    name = validate_timezone(tz)
    before = get_user_timezone().key
    _upsert(None, timezone=name)
    after = get_user_timezone().key
    if after != before:
        logger.info("workspace: timezone %s -> %s; re-timing the principal's briefs", before, after)
        from openexecutive.scheduler.runner import reschedule_principal_rhythm

        try:
            reschedule_principal_rhythm()
        except Exception:
            logger.exception("workspace: re-timing the principal's briefs failed")
    return get_workspace()


def set_workspace_mode(mode: str) -> WorkspaceSettings:
    """Switch between ``"solo"`` and ``"team"``. Raises ``ValueError`` otherwise.

    Solo cancels every pending department check-in (``dept_cadence``); team
    re-bootstraps them. Department rows themselves are left in place, so
    switching back is instant. Both are idempotent and best-effort: a
    failure is logged, and the backstops hold — the scheduler retires a
    check-in that fires in solo, and boot re-bootstraps them in team.
    """
    if mode not in WORKSPACE_MODES:
        raise ValueError(f"mode must be one of {', '.join(WORKSPACE_MODES)}")
    _upsert(None, mode=mode)
    from openexecutive.departments.cadence import (
        bootstrap_cadences,
        cancel_pending_cadences,
    )

    try:
        if mode == "solo":
            cancelled = cancel_pending_cadences()
            logger.info("workspace: solo mode — cancelled %d department check-in(s)", cancelled)
        else:
            inserted = bootstrap_cadences()
            logger.info("workspace: team mode — scheduled %d department check-in(s)", inserted)
    except Exception:
        logger.exception("workspace: updating department check-ins for %s mode failed", mode)
    return get_workspace()


def restore_workspace_settings(
    settings: WorkspaceSettings, db_path: Path | None = None
) -> None:
    """Write ``settings`` verbatim, with none of the scheduler side effects of
    ``set_timezone`` / ``set_workspace_mode``. For callers that rebuild
    ``scheduled_actions`` themselves (fixture load / unload)."""
    _upsert(db_path, mode=settings.mode, timezone=validate_timezone(settings.timezone))


def reset_workspace_settings(db_path: Path | None = None) -> None:
    """Back to the defaults (team, no zone). No scheduler side effects.

    A DB file that does not exist has nothing to reset and is not created.
    """
    path = _resolve_db_path(db_path)
    if not path.exists():
        return
    conn = _connect(path)
    try:
        conn.execute(_CREATE_SQL)
        conn.execute(f"DELETE FROM {TABLE}")  # noqa: S608 — constant table name
        conn.commit()
    finally:
        conn.close()
