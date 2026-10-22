"""The owner's safety level (Settings -> Safety, "When should I check with
you?"): how much a read of someone else's text holds a turn
(``orchestrator.outside_content``). Every outside read is checked for
attempts to steer the Executive (``orchestrator.content_scan``) at every
level; the level decides what a read, and a flag, then hold.

Three levels, one per company, each channel able to differ (``CHANNELS``,
stored beside it; a channel not set follows the company's level):

- ``ask`` (Careful): any read of a connected app, an attachment, a web
  page, a synced folder or a vendor app holds the turn's writes.
- ``outsiders`` (Balanced, the default): a read holds the turn only when the
  check flags it or it is too big to check whole (padding would dodge the
  check). Any other read it can't check goes on behind a notice that it is data,
  and a web search (its results arrive encrypted, so nothing checks them or
  carries a notice) holds nothing: the owner's call, to cut friction. Reads whose text came from the team are not
  even checked: a Gmail search's list of ids, a Gmail message Gmail
  authenticated as a teammate's (contacts are outside), and a detailed
  calendar read of events the Executive itself organized, naming only the
  team (``outside_content.mark_read``).
- ``trust`` (Hands-off): on the principal's own turn on an interactive
  surface (``content_trust.principal_only_withheld`` offers them everything),
  no read holds; a flagged one is still flagged to them, and the model is
  told not to follow it. Anyone else's turn falls back to ``outsiders``.

Whatever the setting, a turn answering mail from someone not in People is
held from the start, a shared chat or another AI app's turn is ``ask``,
mail and messages still go only to people on People, and someone added to
People this turn is reached no earlier than the next one.

What Balanced trusts without checking rests on Gmail's sender authentication and
on the Executive's own address: a company whose domain doesn't enforce DMARC
(``p=reject`` or ``quarantine``) leaves room for an invite forged as the
Executive's own event to pass as the team's. Choose Careful there.

Stored in the episodic database (``TABLE``, one row: the level and the
channels set apart from it). Plain code, never raises: an unreadable store is
``ask``, a missing row is ``DEFAULT``.
"""
from __future__ import annotations

import json
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

# The levels by the names the person reads (Settings -> Safety, the API).
LEVELS: dict[str, str] = {ASK: "careful", OUTSIDERS: "balanced", TRUST: "hands_off"}
MODE_OF_LEVEL: dict[str, str] = {level: mode for mode, level in LEVELS.items()}

# The channels a level can be set on apart from the company's, by the
# session's ``origin_channel`` (``web`` is the web app's chat). A turn on any
# other surface follows the company's level.
CHANNELS: tuple[str, ...] = ("web", "slack", "google_chat", "telegram", "discord", "email")

TABLE = "outside_content_mode"
_DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "  id INTEGER PRIMARY KEY CHECK (id = 1),"
    "  mode TEXT NOT NULL,"
    "  updated_at TEXT,"
    "  updated_by TEXT,"
    "  channels TEXT"
    ")"
)


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    if "channels" not in columns:
        try:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN channels TEXT")
        except sqlite3.OperationalError as exc:
            # Another worker added it between the check and here.
            if "duplicate column" not in str(exc).lower():
                raise


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


def stored_channels(*, db_path: Path | None = None) -> dict[str, str]:
    """The channels set apart from the company's level: channel -> mode.
    Empty when none is, or when the store can't be read (logged); a turn's
    level then reads as ``ask`` (``channel_mode``)."""
    return _read_channels(db_path) or {}


def _read_channels(db_path: Path | None = None) -> dict[str, str] | None:
    """``stored_channels``, or None when the store can't be read."""
    path = _db_path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(f"SELECT channels FROM {TABLE} WHERE id = 1").fetchone()  # noqa: S608 — constant table name
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return {}
        logger.warning("outside_mode: couldn't read the channels (%s)", exc)
        return None
    except Exception:
        logger.warning("outside_mode: couldn't read the channels", exc_info=True)
        return None
    try:
        parsed = json.loads(row[0]) if row is not None and row[0] else {}
    except (TypeError, ValueError):
        logger.warning("outside_mode: the stored channels aren't readable")
        return None
    if not isinstance(parsed, dict):
        logger.warning("outside_mode: the stored channels aren't readable")
        return None
    return {c: m for c, m in parsed.items() if c in CHANNELS and m in MODES}


def channel_mode(channel: str | None) -> str:
    """The level a turn on ``channel`` starts from: the channel's own when
    it has one, else the company's (``get_mode``). Channels that can't be
    read are ``ask``, like an unreadable level, so a channel set to Careful
    never runs looser."""
    if not channel:
        return get_mode()
    stored = _read_channels()
    if stored is None:
        return ASK
    own = stored.get(channel)
    return own if own is not None else get_mode()


def set_mode(
    mode: str,
    *,
    updated_by: str,
    channels: dict[str, str | None] | None = None,
    db_path: Path | None = None,
) -> None:
    """Store the level and, when given, channel changes (a channel set to
    None follows the level again). Callers authorize first (the route)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    for channel, value in (channels or {}).items():
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        if value is not None and value not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
    conn = sqlite3.connect(str(_db_path(db_path)), isolation_level=None)
    try:
        _ensure(conn)
        # Read the channels and write them back in one transaction, so two
        # changes at once can't drop each other's channel.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT channels FROM {TABLE} WHERE id = 1").fetchone()  # noqa: S608 — constant table name
        try:
            parsed = json.loads(row[0]) if row is not None and row[0] else {}
        except (TypeError, ValueError):
            parsed = {}
        current = {c: m for c, m in (parsed if isinstance(parsed, dict) else {}).items() if c in CHANNELS and m in MODES}
        for channel, value in (channels or {}).items():
            if value is None:
                current.pop(channel, None)
            else:
                current[channel] = value
        conn.execute(
            f"INSERT INTO {TABLE} (id, mode, updated_at, updated_by, channels) VALUES (1, ?, ?, ?, ?) "  # noqa: S608 — constant table name
            "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by, "
            "channels = excluded.channels",
            (mode, datetime.now(UTC).isoformat(), updated_by, json.dumps(current, sort_keys=True)),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def channel_of(session: Any) -> str | None:
    """The channel a turn arrived on (``CHANNELS``), or None."""
    if getattr(session, "from_web_chat", False) is True:
        return "web"
    channel = str(getattr(session, "origin_channel", "") or "")
    return channel if channel in CHANNELS else None


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
        mode = channel_mode(channel_of(session))
        if mode != TRUST:
            return mode
        from openexecutive.orchestrator.content_trust import principal_only_withheld

        # The principal, on an interactive surface that isn't email, in a
        # chat nobody else writes into.
        return TRUST if not principal_only_withheld(session) else OUTSIDERS
    except Exception:
        logger.warning("outside_mode: couldn't tell the setting for this turn — asking every time", exc_info=True)
        return ASK
