"""Tables for Always in the loop (``memory.history``), in the episodic DB.

Per company, like the roster the notes are keyed on: they swap with a client
slot (``clients.slots._BLANK_WIPE_TABLES``) and a factory reset clears them
(``cli.fixture_loader``).

Kept free of imports so ``memory.episodic.initialize_db`` can create them
without an import cycle.
"""
from __future__ import annotations

import sqlite3

NOTES_TABLE = "history_notes"
COMPANY_TABLE = "history_settings"
PERSON_TABLE = "history_person_settings"
EXCLUDED_TABLE = "history_excluded"

TABLES: tuple[str, ...] = (NOTES_TABLE, COMPANY_TABLE, PERSON_TABLE, EXCLUDED_TABLE)

_DDL: tuple[str, ...] = (
    # One dated note of what happened. ``person_id`` is whose note it is: a
    # private note is read by that person alone, never the principal.
    # ``conversation_key`` is a hash of where it came from (a mail thread),
    # so "Don't remember this" can find every note from one conversation.
    f"CREATE TABLE IF NOT EXISTS {NOTES_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  person_id INTEGER NOT NULL,"
    "  visibility TEXT NOT NULL DEFAULT 'private',"
    "  source TEXT NOT NULL,"
    "  channel TEXT NOT NULL,"
    "  conversation_key TEXT NOT NULL,"
    "  counterpart TEXT NOT NULL DEFAULT '',"
    "  subject TEXT NOT NULL DEFAULT '',"
    "  kind TEXT NOT NULL,"
    "  summary TEXT NOT NULL,"
    "  quote TEXT NOT NULL,"
    "  due_date TEXT,"
    "  trust TEXT NOT NULL,"
    "  occurred_at TEXT NOT NULL,"
    "  created_at TEXT NOT NULL,"
    "  expires_at TEXT,"
    "  pinned INTEGER NOT NULL DEFAULT 0,"
    "  correction TEXT,"
    "  corrected_at TEXT"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{NOTES_TABLE}_person_occurred "
    f"ON {NOTES_TABLE}(person_id, occurred_at)",
    f"CREATE INDEX IF NOT EXISTS idx_{NOTES_TABLE}_conversation "
    f"ON {NOTES_TABLE}(person_id, conversation_key)",
    # The owner's company default for how long notes last. One row; absent
    # means the built-in default. ``retention_days`` NULL means until forgotten.
    f"CREATE TABLE IF NOT EXISTS {COMPANY_TABLE} ("
    "  id INTEGER PRIMARY KEY CHECK (id = 1),"
    "  retention_days INTEGER,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # Each person's own switches. Absent means off, and the company default.
    f"CREATE TABLE IF NOT EXISTS {PERSON_TABLE} ("
    "  person_id INTEGER PRIMARY KEY,"
    "  reply_notes INTEGER NOT NULL DEFAULT 0,"
    "  retention_days INTEGER,"
    "  updated_at TEXT,"
    "  updated_by TEXT"
    ")",
    # Conversations someone asked it not to remember.
    f"CREATE TABLE IF NOT EXISTS {EXCLUDED_TABLE} ("
    "  person_id INTEGER NOT NULL,"
    "  conversation_key TEXT NOT NULL,"
    "  created_at TEXT NOT NULL,"
    "  PRIMARY KEY (person_id, conversation_key)"
    ")",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if missing. Idempotent."""
    for statement in _DDL:
        conn.execute(statement)
