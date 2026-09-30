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

TABLES: tuple[str, ...] = (SETTINGS_TABLE, VOICE_TABLE, VOICE_HISTORY_TABLE, DRAFTS_TABLE)

_DDL: tuple[str, ...] = (
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
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if missing. Idempotent."""
    for statement in _DDL:
        conn.execute(statement)
