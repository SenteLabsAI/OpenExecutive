"""The owner's "When I read outside content" setting: how much a read of
someone else's text holds a turn (``orchestrator.outside_content``).

Three settings, one per company:

- ``ask`` (Ask every time): any read of a connected app, an attachment, a web
  page, a synced folder or a vendor app holds the turn's writes, as before.
- ``outsiders`` (Only for outsiders, the default): reads whose text came
  from the team hold nothing. That is an attachment the speaker sent, a
  synced company folder, and a mail or calendar read where every address in
  what came back is someone on People or the Executive itself
  (``outside_content.mark_read``). Web pages, documents fetched by address,
  vendor apps, and mail or calendar showing anyone else still hold.
- ``trust`` (Don't ask): on the principal's own turn on an interactive
  surface (``content_trust.principal_only_withheld`` offers them everything),
  no read holds. Anyone else's turn falls back to ``outsiders``.

Whatever the setting, a turn answering mail from someone not in People is
held from the start, a shared chat or another AI app's turn is ``ask``,
mail and messages still go only to people on People, and someone added to
People this turn is reached no earlier than the next one.

Stored in the episodic database (``TABLE``, one row). Plain code, never
raises: an unreadable store is ``ask``, a missing row is ``DEFAULT``.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ASK = "ask"
OUTSIDERS = "outsiders"
TRUST = "trust"
MODES: tuple[str, ...] = (ASK, OUTSIDERS, TRUST)
DEFAULT = OUTSIDERS

TABLE = "outside_content_mode"
_DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "  id INTEGER PRIMARY KEY CHECK (id = 1),"
    "  mode TEXT NOT NULL,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")"
)


def _db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def get_mode(*, db_path: Path | None = None) -> str:
    """The setting in force (``stored_mode``). The test suite pins it to
    ``ASK`` (``tests/conftest.py``), so a test sees the strict holds unless
    it picks a mode."""
    return stored_mode(db_path=db_path)


def stored_mode(*, db_path: Path | None = None) -> str:
    """The stored setting: ``DEFAULT`` when none is stored, ``ASK`` when the
    store can't be read (logged)."""
    path = _db_path(db_path)
    if not path.exists():
        return DEFAULT
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(f"SELECT mode FROM {TABLE} WHERE id = 1").fetchone()  # noqa: S608 — constant table name
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return DEFAULT
        logger.warning("outside_mode: couldn't read the setting (%s) — asking every time", exc)
        return ASK
    except Exception:
        logger.warning("outside_mode: couldn't read the setting — asking every time", exc_info=True)
        return ASK
    if row is None:
        return DEFAULT
    return row[0] if row[0] in MODES else ASK


def set_mode(mode: str, *, updated_by: str, db_path: Path | None = None) -> None:
    """Store the setting. Callers authorize first (the route)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    conn = sqlite3.connect(str(_db_path(db_path)))
    try:
        conn.execute(_DDL)
        conn.execute(
            f"INSERT INTO {TABLE} (id, mode, updated_at, updated_by) VALUES (1, ?, ?, ?) "  # noqa: S608 — constant table name
            "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (mode, datetime.now(UTC).isoformat(), updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def for_turn(session: Any) -> str:
    """The setting as it applies to ``session``'s turn. Never raises: an
    error is ``ASK``."""
    try:
        if session is None:
            return ASK
        if getattr(session, "sender_off_team", False) is True:
            return ASK
        if getattr(session, "shared_chat", False) is True:
            return ASK
        if getattr(session, "from_mcp_client", False) is True:
            return ASK
        mode = get_mode()
        if mode != TRUST:
            return mode
        from openexecutive.orchestrator.content_trust import principal_only_withheld

        # The principal, on an interactive surface that isn't email, in a
        # chat nobody else writes into.
        return TRUST if not principal_only_withheld(session) else OUTSIDERS
    except Exception:
        logger.warning("outside_mode: couldn't tell the setting for this turn — asking every time", exc_info=True)
        return ASK
