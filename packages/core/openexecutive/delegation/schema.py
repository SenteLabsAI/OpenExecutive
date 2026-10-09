"""Tables for Act as me, in the episodic DB (per company: they swap with a
client slot — see ``clients.slots._BLANK_WIPE_TABLES``).

Kept free of imports so ``memory.episodic.initialize_db`` can create them
without an import cycle.
"""
from __future__ import annotations

import json
import sqlite3

SETTINGS_TABLE = "delegation_settings"
VOICE_TABLE = "delegation_voice"
VOICE_HISTORY_TABLE = "delegation_voice_history"
DRAFTS_TABLE = "delegation_drafts"
INBOX_WATCH_TABLE = "delegation_inbox_watch"
INBOX_MESSAGES_TABLE = "delegation_inbox_messages"
TEAM_TABLE = "delegation_team"
HANDLE_IT_TABLE = "delegation_handle_it"
HANDLED_TABLE = "delegation_handled"
REMINDERS_TABLE = "delegation_reminders"
MAIL_READ_TABLE = "delegation_mail_read"
TRAINING_TABLE = "delegation_training"

TABLES: tuple[str, ...] = (
    SETTINGS_TABLE,
    VOICE_TABLE,
    VOICE_HISTORY_TABLE,
    DRAFTS_TABLE,
    INBOX_WATCH_TABLE,
    INBOX_MESSAGES_TABLE,
    TEAM_TABLE,
    HANDLE_IT_TABLE,
    HANDLED_TABLE,
    REMINDERS_TABLE,
    MAIL_READ_TABLE,
    TRAINING_TABLE,
)

_DDL: tuple[str, ...] = (
    # The owner's "Let team members use Act as me": one row, absent means off.
    f"CREATE TABLE IF NOT EXISTS {TEAM_TABLE} ("
    "  id INTEGER PRIMARY KEY CHECK (id = 1),"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # One row per person who has ever set it. Absent means off.
    f"CREATE TABLE IF NOT EXISTS {SETTINGS_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # "How I write": one profile per person (JSON), lockable.
    f"CREATE TABLE IF NOT EXISTS {VOICE_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  profile TEXT NOT NULL DEFAULT '{}',"
    "  locked INTEGER NOT NULL DEFAULT 0,"
    "  learned_at TEXT,"
    "  sample_count INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # Every change to a profile, for review.
    f"CREATE TABLE IF NOT EXISTS {VOICE_HISTORY_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  created_at TEXT NOT NULL,"
    "  profile TEXT NOT NULL,"
    "  locked INTEGER NOT NULL DEFAULT 0,"
    "  updated_by TEXT NOT NULL"
    ")",
    # Every draft saved in someone's Gmail (delegation.drafts): ids and
    # times only, never an address, a subject or text.
    f"CREATE TABLE IF NOT EXISTS {DRAFTS_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  source TEXT NOT NULL,"
    "  thread_id TEXT,"
    "  draft_id TEXT,"
    "  message_id TEXT,"
    "  sent_message_id TEXT,"
    "  created_at TEXT NOT NULL"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{DRAFTS_TABLE}_person_created "
    f"ON {DRAFTS_TABLE}(person_id, created_at)",
    # The inbox watcher's switch and health, one row per person (absent: off).
    # watch_since resets on every off → on, so it never drafts for old mail.
    f"CREATE TABLE IF NOT EXISTS {INBOX_WATCH_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  watch_since TEXT,"
    "  last_poll_at TEXT,"
    "  status TEXT NOT NULL DEFAULT 'off',"
    "  backoff_until TEXT,"
    "  failures INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # What the watcher did with each inbound message: its durable cursor.
    # Codes and ids only: sender_key is a hash of the address, never the
    # address, and no subject or text is kept here.
    f"CREATE TABLE IF NOT EXISTS {INBOX_MESSAGES_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  message_id TEXT NOT NULL,"
    "  thread_id TEXT,"
    "  received_at TEXT,"
    "  sender_key TEXT,"
    "  relation TEXT,"
    "  outcome TEXT NOT NULL,"
    "  reason TEXT,"
    "  classified INTEGER NOT NULL DEFAULT 0,"
    "  attempts INTEGER NOT NULL DEFAULT 0,"
    "  draft_id TEXT,"
    "  decision_id INTEGER,"
    "  flags TEXT NOT NULL DEFAULT '[]',"
    "  created_at TEXT NOT NULL,"
    "  updated_at TEXT NOT NULL,"
    "  UNIQUE(person_id, message_id)"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{INBOX_MESSAGES_TABLE}_thread "
    f"ON {INBOX_MESSAGES_TABLE}(person_id, thread_id)",
    f"CREATE INDEX IF NOT EXISTS idx_{INBOX_MESSAGES_TABLE}_created "
    f"ON {INBOX_MESSAGES_TABLE}(person_id, created_at)",
    # Handle it for me, one row per person (absent: off). mode is careful |
    # balanced | bold (delegation.handle_it.MODES; a 'training' row from
    # before is moved by _move_training_off_the_dial). levels is the per-kind
    # setting it replaced, kept for older rows (_add_handle_it_mode).
    f"CREATE TABLE IF NOT EXISTS {HANDLE_IT_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  levels TEXT NOT NULL DEFAULT '{}',"
    "  mode TEXT NOT NULL DEFAULT 'balanced',"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # Every reply the inbox watcher sent on its own: ids and times only.
    f"CREATE TABLE IF NOT EXISTS {HANDLED_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  thread_id TEXT NOT NULL,"
    "  decision_id INTEGER,"
    "  sent_message_id TEXT,"
    "  sent_at TEXT NOT NULL"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{HANDLED_TABLE}_person_sent "
    f"ON {HANDLED_TABLE}(person_id, sent_at)",
    # remind_me: plain text sent to the person who asked, when it is due
    # (delegation.reminders). Never a scheduled_actions row: that list is
    # everyone's, and its rows run an Executive turn when they fire.
    f"CREATE TABLE IF NOT EXISTS {REMINDERS_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  text TEXT NOT NULL,"
    "  due_at TEXT NOT NULL,"
    "  created_at TEXT NOT NULL,"
    "  claimed_at TEXT,"
    "  sent_at TEXT,"
    "  cancelled_at TEXT"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{REMINDERS_TABLE}_due "
    f"ON {REMINDERS_TABLE}(claimed_at, cancelled_at, due_at)",
    # The threads of someone's own mailbox read_my_email opened for them
    # (delegation.mail_reads): where, never what. One row per thread, the
    # subject and sender as one line each and never a message's text, so a
    # later conversation can open it again; mailbox is the address it was
    # read from, so a list never points into another mailbox.
    f"CREATE TABLE IF NOT EXISTS {MAIL_READ_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  mailbox TEXT NOT NULL,"
    "  thread_id TEXT NOT NULL,"
    "  subject TEXT NOT NULL DEFAULT '',"
    "  sender TEXT NOT NULL DEFAULT '',"
    "  read_at TEXT NOT NULL,"
    "  UNIQUE(person_id, mailbox, thread_id)"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{MAIL_READ_TABLE}_person_read "
    f"ON {MAIL_READ_TABLE}(person_id, read_at)",
    # Act as me in training, per setting (delegation.training.SETTINGS):
    # one row per person and setting, absent means not in training.
    f"CREATE TABLE IF NOT EXISTS {TRAINING_TABLE} ("
    "  person_id INTEGER NOT NULL,"
    "  setting TEXT NOT NULL,"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  updated_at TEXT,"
    "  updated_by TEXT,"
    "  PRIMARY KEY (person_id, setting)"
    ")",
)


def _add_handle_it_mode(conn: sqlite3.Connection) -> None:
    """Give a table made before ``mode`` existed the column, never letting
    more go than before. Replies to people they know on their own (the old
    default) stay ``balanced``, or ``bold`` if strangers were handled too.
    Anyone who had known people on ask or off sent nothing on its own, and
    every mode does, so it is turned off for them to turn back on."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({HANDLE_IT_TABLE})")}
    if "mode" in columns:
        return
    conn.execute(f"ALTER TABLE {HANDLE_IT_TABLE} ADD COLUMN mode TEXT NOT NULL DEFAULT 'balanced'")
    for person_id, levels in conn.execute(f"SELECT person_id, levels FROM {HANDLE_IT_TABLE}").fetchall():  # noqa: S608
        try:
            chosen = json.loads(levels or "{}")
            known, stranger = chosen.get("reply_known", "handle"), chosen.get("reply_stranger")
        except (ValueError, AttributeError):
            continue
        if known != "handle":
            conn.execute(f"UPDATE {HANDLE_IT_TABLE} SET enabled = 0 WHERE person_id = ?", (person_id,))  # noqa: S608
        elif stranger == "handle":
            conn.execute(f"UPDATE {HANDLE_IT_TABLE} SET mode = 'bold' WHERE person_id = ?", (person_id,))  # noqa: S608
    conn.commit()


def _move_training_off_the_dial(conn: sqlite3.Connection) -> None:
    """In training was once a step on Handle it for me's dial (mode
    ``training``); it is now a switch per Act as me setting. Such a row moves
    to Balanced, the limits its allowed senders had, with Replies and
    Follow-ups in training (follow-ups always waited there), so nothing more
    goes on its own than before."""
    rows = conn.execute(
        f"SELECT person_id FROM {HANDLE_IT_TABLE} WHERE mode = 'training'"  # noqa: S608 — constant table name
    ).fetchall()
    if not rows:
        return
    for (person_id,) in rows:
        for setting in ("replies", "follow_ups"):
            conn.execute(
                f"INSERT INTO {TRAINING_TABLE} (person_id, setting, enabled, updated_by) "  # noqa: S608
                "VALUES (?, ?, 1, 'migration') ON CONFLICT(person_id, setting) DO UPDATE SET enabled = 1",
                (person_id, setting),
            )
        conn.execute(
            f"UPDATE {HANDLE_IT_TABLE} SET mode = 'balanced' WHERE person_id = ?",  # noqa: S608
            (person_id,),
        )
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if missing. Idempotent."""
    for statement in _DDL:
        conn.execute(statement)
    _add_handle_it_mode(conn)
    _move_training_off_the_dial(conn)
