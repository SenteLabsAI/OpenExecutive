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
- The principal's role (``PrincipalRole``), all nullable: ``role_kind``
  (``owner`` / ``in_house`` / ``independent`` / ``other``), ``role_title``,
  ``reports_to``, ``remit`` (what they are responsible for) and
  ``measured_on`` (what they are judged on). Solo mode is for anyone using
  Open Executive for themselves — a business owner, an executive inside a
  larger organisation, an independent or fractional executive — and this is
  how the Executive and its specialists learn which. Only solo mode reads it
  (the solo org block and the specialists' ``<principal_role>`` tag); it is
  kept, not cleared, in team mode.

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
from typing import TYPE_CHECKING, Literal, cast, get_args
from zoneinfo import ZoneInfo

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

# How the principal relates to the organisation in their profile.
RoleKind = Literal["owner", "in_house", "independent", "other"]
ROLE_KINDS: tuple[str, ...] = get_args(RoleKind)

# The role's free-text fields and their length caps (after trimming). The
# same caps bound the API, a fixture's workspace.yaml and an eval scenario.
ROLE_TEXT_MAX: dict[str, int] = {
    "role_title": 120,
    "reports_to": 120,
    "remit": 500,
    "measured_on": 300,
}
ROLE_FIELDS: tuple[str, ...] = ("role_kind", *ROLE_TEXT_MAX)

# role_kind in plain words, as the solo org block and the specialists'
# <principal_role> tag say it. "other" has no phrase: the title says it.
ROLE_KIND_PHRASE: dict[str, str] = {
    "owner": "the owner or founder of their own business",
    "in_house": "an executive inside an organisation they do not own",
    "independent": "an independent or fractional executive who serves clients",
}

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'team',
    timezone TEXT,
    updated_at TEXT,
    role_kind TEXT,
    role_title TEXT,
    reports_to TEXT,
    remit TEXT,
    measured_on TEXT
)
"""


class PrincipalRole(BaseModel):
    """What the principal does: the context the solo persona reads their role
    from. Every field is optional; all unset means "not said"."""

    model_config = ConfigDict(extra="ignore")

    role_kind: RoleKind | None = None
    role_title: str | None = None
    reports_to: str | None = None
    remit: str | None = None
    measured_on: str | None = None

    def principal_role(self) -> PrincipalRole:
        """Just the role fields (a ``WorkspaceSettings`` carries more)."""
        return PrincipalRole(**{f: getattr(self, f) for f in ROLE_FIELDS})

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in ROLE_FIELDS)


class WorkspaceSettings(PrincipalRole):
    """The one settings row: mode, zone, and the principal's role."""

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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table if missing and add the role columns to one created
    before they existed (additive ALTERs; a duplicate-column error from a
    concurrent boot counts as success)."""
    conn.execute(_CREATE_SQL)
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    for col in ROLE_FIELDS:
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def init_workspace_settings_db(db_path: Path | None = None) -> None:
    """Create the table (and any missing column) if needed. Idempotent."""
    conn = _connect(_resolve_db_path(db_path))
    try:
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()


def load_zone(name: str) -> ZoneInfo | None:
    """``ZoneInfo(name)``, or None when ``name`` is not a loadable zone.

    ``zoneinfo`` fails in more ways than ``ZoneInfoNotFoundError``: a region
    directory ("America") raises ``IsADirectoryError``, an unnormalized or
    escaping key ("Europe/", "../etc") ``ValueError``, a non-TZif file
    ``ValueError`` again. Every zone name this module takes from a user, a
    file or the database goes through here, so none of them can escape as a
    500, an aborted fixture load or a failed boot.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def validate_timezone(tz: str | None) -> str | None:
    """Return ``tz`` as a known IANA zone name, or None for "not set".

    Blank means "clear". Raises ``ValueError`` for anything ``zoneinfo``
    cannot load — the same rule as the ``USER_TIMEZONE`` setting.
    """
    if tz is None:
        return None
    if not isinstance(tz, str):
        raise ValueError("timezone must be a string")
    name = tz.strip()
    if not name:
        return None
    if len(name) > _MAX_TZ_LEN or load_zone(name) is None:
        raise ValueError(f"timezone {name[:_MAX_TZ_LEN]!r} is not a known IANA zone")
    return name


def validate_role_kind(kind: object) -> RoleKind | None:
    """``kind`` as a role kind, or None for "not set" (None or blank).
    Raises ``ValueError`` for anything else."""
    if kind is None:
        return None
    if not isinstance(kind, str):
        raise ValueError("role_kind must be a string")
    name = kind.strip()
    if not name:
        return None
    if name not in ROLE_KINDS:
        raise ValueError(f"role_kind must be one of {', '.join(ROLE_KINDS)}")
    return cast(RoleKind, name)


def validate_role_text(field: str, value: object) -> str | None:
    """One free-text role field, trimmed; None or blank means "not set".
    Raises ``ValueError`` for a non-string or a value over the field's cap.
    The message never quotes the value."""
    cap = ROLE_TEXT_MAX[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > cap:
        raise ValueError(f"{field} must be at most {cap} characters")
    return text


def validate_role_field(field: str, value: object) -> str | None:
    """``validate_role_kind`` or ``validate_role_text``, by field name."""
    if field == "role_kind":
        return validate_role_kind(value)
    return validate_role_text(field, value)


def _stored_role(row: sqlite3.Row) -> dict[str, str | None]:
    """The role columns of a stored row, field by field: a column the row
    lacks (a table not migrated yet) or a value that no longer validates
    (hand-edited) reads as unset. Never raises."""
    keys = set(row.keys())
    out: dict[str, str | None] = {}
    for field in ROLE_FIELDS:
        if field not in keys:
            continue
        try:
            out[field] = validate_role_field(field, row[field])
        except ValueError:
            logger.warning("workspace: ignoring an invalid stored %s", field)
    return out


def _stored_zone(raw: object) -> str | None:
    """A stored zone name if it still loads, else None (logged). Never raises."""
    try:
        if raw is not None and not isinstance(raw, str):
            raise ValueError("not a string")
        return validate_timezone(raw)
    except ValueError:
        logger.warning("workspace: ignoring unknown stored timezone %r", raw)
        return None


def get_workspace(db_path: Path | None = None) -> WorkspaceSettings:
    """The stored settings, or the defaults when nothing is stored.

    Never raises — boot, every chat turn and the scheduler read it. A
    missing file, table or row reads as the defaults; any read error (a
    locked or corrupt DB, say) also reads as the defaults but is logged as
    a warning so it can be diagnosed; and a stored value that no longer
    validates (a hand-edited mode, a zone this Python cannot load) is
    ignored field by field.
    """
    try:
        path = _resolve_db_path(db_path)
        if not path.exists():
            return WorkspaceSettings()
        conn = _connect(path)
        try:
            # SELECT *: a table created before the role columns existed has
            # none of them, and a read never migrates (_stored_role).
            row = conn.execute(
                f"SELECT * FROM {TABLE} WHERE id = 1"  # noqa: S608 — constant table name
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            _log_read_failure(exc)
        return WorkspaceSettings()
    except Exception as exc:
        _log_read_failure(exc)
        return WorkspaceSettings()
    if row is None:
        return WorkspaceSettings()

    mode = row["mode"]
    if mode not in WORKSPACE_MODES:
        logger.warning("workspace: ignoring unknown stored mode %r", mode)
        mode = DEFAULT_MODE
    return WorkspaceSettings.model_validate(
        {"mode": mode, "timezone": _stored_zone(row["timezone"]), **_stored_role(row)}
    )


def _log_read_failure(exc: BaseException) -> None:
    logger.warning(
        "workspace: could not read settings (%s: %s) — using the defaults",
        type(exc).__name__, exc,
    )
    logger.debug("workspace: settings read failure", exc_info=exc)


def _configured_timezone() -> ZoneInfo:
    """The ``USER_TIMEZONE`` setting, else UTC."""
    try:
        from openexecutive.config import get_settings

        zone = load_zone(get_settings().user_timezone)
    except Exception:
        zone = None
    return zone if zone is not None else ZoneInfo("UTC")


def get_user_timezone(db_path: Path | None = None) -> ZoneInfo:
    """The zone in effect: the workspace's, else ``USER_TIMEZONE``, else UTC.
    Never raises."""
    stored = get_workspace(db_path).timezone
    zone = load_zone(stored) if stored else None
    return zone if zone is not None else _configured_timezone()


def _valid_mode(value: object) -> WorkspaceMode | None:
    if value == "solo":
        return "solo"
    if value == "team":
        return "team"
    return None


def effective_workspace_mode(session: Session | None = None) -> WorkspaceMode:
    """The mode a turn runs in: the session's override when it carries a valid
    one (evals run scenarios concurrently on one Executive, so they cannot
    flip the install-wide setting), else the mode pinned for the session's
    current turn (``pin_turn_workspace_mode``), else the workspace's."""
    if session is not None:
        override = _valid_mode(getattr(session, "workspace_mode", None))
        if override is not None:
            return override
        pinned = _valid_mode(getattr(session, "turn_workspace_mode", None))
        if pinned is not None:
            return pinned
    return get_workspace().mode


def pin_turn_workspace_mode(session: Session) -> WorkspaceMode:
    """Resolve the mode for a NEW turn and pin it on the session.

    The override, else the workspace read fresh — never the previous turn's
    pin, so a switch applies from the next turn. Every later
    ``effective_workspace_mode(session)`` in the turn (the tool handlers:
    schedule_followup, create_calendar_event, the loop's dispatch guard)
    then returns this same mode, so a switch made mid-turn cannot leave the
    persona and tool list in one mode and a handler in the other.
    """
    mode = _valid_mode(getattr(session, "workspace_mode", None)) or get_workspace().mode
    session.turn_workspace_mode = mode
    return mode


def effective_principal_role(session: Session | None = None) -> PrincipalRole:
    """The principal's role for a turn: the session's override when it
    carries one (an eval scenario's ``principal_role`` — scenarios run
    concurrently on one Executive, so they cannot write the install-wide
    row), else the role pinned for the session's current turn
    (``pin_turn_principal_role``), else the workspace's, read fresh. Never
    raises; an empty role means none was given."""
    if session is not None:
        override = getattr(session, "principal_role", None)
        if isinstance(override, PrincipalRole):
            return override.principal_role()
        pinned = getattr(session, "turn_principal_role", None)
        if isinstance(pinned, PrincipalRole):
            return pinned.principal_role()
    return get_workspace().principal_role()


def pin_turn_principal_role(session: Session, mode: str) -> PrincipalRole:
    """Resolve the principal's role for a NEW turn in ``mode`` and pin it on
    the session, like ``pin_turn_workspace_mode``.

    Solo: the override, else the workspace read fresh — never the previous
    turn's pin, so an edit applies from the next turn. Team: an empty role
    (team never renders one). Every later ``effective_principal_role(session)``
    in the turn — the org block, the specialists' ``<principal_role>`` tag,
    and a workflow the turn starts, whose specialists read it through
    ``router.load_principal_role`` — then returns this same role, so a
    ``PUT /workspace`` sent mid-turn cannot give them different roles.
    """
    role = PrincipalRole()
    if mode == "solo":
        override = getattr(session, "principal_role", None)
        role = (
            override.principal_role()
            if isinstance(override, PrincipalRole)
            else get_workspace().principal_role()
        )
    session.turn_principal_role = role
    return role


def _upsert(db_path: Path | None, **columns: str | None) -> None:
    """Write the given columns of the row (``mode`` / ``timezone`` / the role
    fields), leaving the others as they are. Creates the table, any missing
    column and the row as needed."""
    unknown = set(columns) - {"mode", "timezone", *ROLE_FIELDS}
    if unknown:
        raise ValueError(f"unknown workspace column(s): {sorted(unknown)}")
    names = list(columns)
    now = datetime.now(UTC).isoformat()
    col_list = ", ".join([*names, "updated_at"])
    placeholders = ", ".join("?" for _ in range(len(names) + 1))
    updates = ", ".join(f"{n} = excluded.{n}" for n in [*names, "updated_at"])
    conn = _connect(_resolve_db_path(db_path))
    try:
        _ensure_schema(conn)
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
    digest and reflection rows are re-timed to the new zone in place
    (``scheduler.runner.reschedule_principal_rhythm``: nothing inserted,
    never two runs of a kind within 12h, never a skipped day); a brief that
    is running right now finishes and chains its successor in the new zone.
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


def set_principal_role(**fields: object) -> WorkspaceSettings:
    """Store the given role fields (any of ``ROLE_FIELDS``), leaving the
    others as they are; None or blank clears one. Raises ``ValueError`` for
    an unknown field or a value that does not validate — before anything is
    written. No side effects: the next turn reads it fresh."""
    unknown = set(fields) - set(ROLE_FIELDS)
    if unknown:
        raise ValueError(f"unknown role field(s): {sorted(unknown)}")
    clean = {f: validate_role_field(f, v) for f, v in fields.items()}
    if clean:
        _upsert(None, **clean)
    return get_workspace()


def restore_workspace_settings(
    settings: WorkspaceSettings, db_path: Path | None = None
) -> None:
    """Write ``settings`` verbatim, with none of the scheduler side effects of
    ``set_timezone`` / ``set_workspace_mode``. For callers that rebuild
    ``scheduled_actions`` themselves (fixture load / unload)."""
    _upsert(
        db_path,
        mode=settings.mode,
        timezone=validate_timezone(settings.timezone),
        **{f: validate_role_field(f, getattr(settings, f)) for f in ROLE_FIELDS},
    )


def reset_workspace_settings(db_path: Path | None = None) -> None:
    """Back to the defaults (team, no zone, no role). No scheduler side effects.

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
