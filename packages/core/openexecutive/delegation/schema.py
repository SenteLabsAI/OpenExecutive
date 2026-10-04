"""Tables for Act as me, in the episodic DB (per company: they swap with a
client slot — see ``clients.slots._BLANK_WIPE_TABLES``).

Kept free of imports so ``memory.episodic.initialize_db`` can create them
without an import cycle.
"""
from __future__ import annotations

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
    # Handle it for me, one row per person (absent: off). levels is a JSON
    # object of kind -> off | ask | handle (delegation.handle_it).
    f"CREATE TABLE IF NOT EXISTS {HANDLE_IT_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  enabled INTEGER NOT NULL DEFAULT 0,"
    "  levels TEXT NOT NULL DEFAULT '{}',"
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
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if missing. Idempotent."""
    for statement in _DDL:
        conn.execute(statement)
