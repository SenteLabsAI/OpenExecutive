"""Unit tests for session_store helpers."""
from pathlib import Path

import pytest

from openexecutive.memory.episodic import initialize_db
from openexecutive.memory.session_store import (
    create_session,
    delete_session,
    get_session_metadata,
    list_sessions,
    load_messages,
    save_message,
    update_session_timestamp,
    update_session_title,
)


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    initialize_db(db_path)
    return db_path


OWNER = 1
OTHER = 2


def test_create_and_list_session(db: Path) -> None:
    create_session("abc123", "What is our strategy?", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    sessions = list_sessions(OWNER, db_path=db)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "abc123"
    assert sessions[0]["title"] == "What is our strategy?"
    assert sessions[0]["message_count"] == 0


def test_save_and_load_messages(db: Path) -> None:
    create_session("s1", "Hello", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    save_message("s1", "user", "Hello there", db_path=db)
    save_message("s1", "assistant", "Hi! How can I help?", db_path=db)

    msgs = load_messages("s1", db_path=db)
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "Hello there"}
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"] == "Hi! How can I help?"
    assert isinstance(msgs[1]["id"], int)


def test_message_count_in_list(db: Path) -> None:
    create_session("s2", "Test", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    save_message("s2", "user", "Q1", db_path=db)
    save_message("s2", "assistant", "A1", db_path=db)
    save_message("s2", "user", "Q2", db_path=db)

    sessions = list_sessions(OWNER, db_path=db)
    assert sessions[0]["message_count"] == 3


def test_update_session_title(db: Path) -> None:
    create_session("s3", "", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    update_session_title("s3", "Updated title", db_path=db)
    meta = get_session_metadata("s3", db_path=db)
    assert meta is not None
    assert meta["title"] == "Updated title"


def test_load_messages_nonexistent_session(db: Path) -> None:
    assert load_messages("nonexistent", db_path=db) == []


def test_create_session_idempotent(db: Path) -> None:
    create_session("dup", "Title", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    create_session("dup", "Other", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)  # INSERT OR IGNORE
    sessions = list_sessions(OWNER, db_path=db)
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Title"


def test_sessions_ordered_by_updated_at(db: Path) -> None:
    create_session("old", "Old session", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    create_session("new", "New session", "2024-06-01T00:00:00", caller_person_id=OWNER, db_path=db)
    update_session_timestamp("new", db_path=db)

    sessions = list_sessions(OWNER, db_path=db)
    assert sessions[0]["session_id"] == "new"


def test_list_sessions_filters_by_owner(db: Path) -> None:
    """Each user sees only their own chats."""
    create_session("mine", "Mine", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    create_session("theirs", "Theirs", "2024-01-01T00:00:00", caller_person_id=OTHER, db_path=db)

    mine = list_sessions(OWNER, db_path=db)
    theirs = list_sessions(OTHER, db_path=db)
    assert [s["session_id"] for s in mine] == ["mine"]
    assert [s["session_id"] for s in theirs] == ["theirs"]


def test_list_sessions_hides_null_owner_rows(db: Path) -> None:
    """Legacy rows (created before caller_person_id existed) are excluded."""
    create_session("legacy", "Pre-migration", "2024-01-01T00:00:00", caller_person_id=None, db_path=db)
    create_session("owned", "Mine", "2024-01-02T00:00:00", caller_person_id=OWNER, db_path=db)

    rows = list_sessions(OWNER, db_path=db)
    assert [s["session_id"] for s in rows] == ["owned"]


def test_create_session_late_owner_upgrade(db: Path) -> None:
    """If turn 1 created the row with NULL owner (caller resolution
    transiently failed), turn 2 with a resolved owner must bind it —
    not silently drop the assignment via INSERT OR IGNORE."""
    create_session("late", "T1", "2024-01-01T00:00:00", caller_person_id=None, db_path=db)
    # Hidden from any user while NULL.
    assert list_sessions(OWNER, db_path=db) == []

    # Turn 2 lands with a real caller.
    create_session("late", "T1", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)

    rows = list_sessions(OWNER, db_path=db)
    assert [s["session_id"] for s in rows] == ["late"]


def test_create_session_does_not_overwrite_existing_owner(db: Path) -> None:
    """A subsequent create_session with a DIFFERENT caller must not steal
    the row — INSERT OR IGNORE protects, and the UPDATE only fires when
    the existing owner is NULL."""
    create_session("yours", "T1", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    # Someone else sends a turn for the same session_id.
    create_session("yours", "Hijack", "2024-01-01T00:00:00", caller_person_id=OTHER, db_path=db)

    assert [s["session_id"] for s in list_sessions(OWNER, db_path=db)] == ["yours"]
    assert list_sessions(OTHER, db_path=db) == []


def test_get_session_metadata_missing(db: Path) -> None:
    assert get_session_metadata("missing", db_path=db) is None


def test_get_session_metadata_includes_message_count(db: Path) -> None:
    create_session("s9", "Test", "2024-01-01T00:00:00", db_path=db)
    save_message("s9", "user", "hello", db_path=db)
    save_message("s9", "assistant", "hi", db_path=db)
    meta = get_session_metadata("s9", db_path=db)
    assert meta is not None
    assert meta["message_count"] == 2


def test_delete_session_removes_session_and_messages(db: Path) -> None:
    create_session("del1", "Doomed", "2024-01-01T00:00:00", caller_person_id=OWNER, db_path=db)
    save_message("del1", "user", "hi", db_path=db)
    save_message("del1", "assistant", "hello", db_path=db)
    create_session("keep", "Survivor", "2024-01-02T00:00:00", caller_person_id=OWNER, db_path=db)
    save_message("keep", "user", "still here", db_path=db)

    assert delete_session("del1", db_path=db) is True
    assert get_session_metadata("del1", db_path=db) is None
    assert load_messages("del1", db_path=db) == []

    remaining = list_sessions(OWNER, db_path=db)
    assert [s["session_id"] for s in remaining] == ["keep"]
    assert remaining[0]["message_count"] == 1


def test_delete_session_missing_returns_false(db: Path) -> None:
    assert delete_session("nope", db_path=db) is False


def test_delete_session_no_db_returns_false(tmp_path: Path) -> None:
    assert delete_session("anything", db_path=tmp_path / "missing.db") is False


def test_save_message_with_list_content(db: Path) -> None:
    create_session("s10", "Test", "2024-01-01T00:00:00", db_path=db)
    save_message("s10", "assistant", [{"type": "text", "text": "hello"}], db_path=db)
    msgs = load_messages("s10", db_path=db)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert isinstance(msgs[0]["content"], str)


def test_save_message_round_trips_the_stopped_flag(db: Path) -> None:
    """A stopped assistant turn is flagged, so a truncated reply is not read
    back as a complete one after a reload."""
    create_session("s11", "Test", "2024-01-01T00:00:00", db_path=db)
    save_message("s11", "user", "explain our burn rate", db_path=db)
    save_message("s11", "assistant", "partial ans", db_path=db, stopped=True)

    msgs = load_messages("s11", db_path=db)
    assert msgs[1]["stopped"] is True
    # Attached only when true, like `actions` — an ordinary message keeps the
    # exact dict shape every existing consumer expects.
    assert "stopped" not in msgs[0]


def test_unstopped_message_omits_the_key(db: Path) -> None:
    create_session("s12", "Test", "2024-01-01T00:00:00", db_path=db)
    save_message("s12", "assistant", "complete answer", db_path=db)

    msgs = load_messages("s12", db_path=db)
    assert "stopped" not in msgs[0]


def test_legacy_rows_without_the_column_still_load(tmp_path: Path) -> None:
    """The additive migration must leave a pre-existing DB readable, with old
    assistant messages defaulting to not-stopped."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Build the pre-migration shape by hand, then let initialize_db migrate it.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('old', 'Old', '2024-01-01', '2024-01-01')"
        )
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) "
            "VALUES ('old', 'assistant', 'legacy reply', '2024-01-01')"
        )

    initialize_db(db_path)

    cols = _columns(db_path, "chat_messages")
    assert "stopped" in cols
    assert "action_chips" in cols

    msgs = load_messages("old", db_path=db_path)
    assert msgs[0]["content"] == "legacy reply"
    assert "stopped" not in msgs[0]

    # And the migrated table still accepts new writes.
    save_message("old", "assistant", "new reply", db_path=db_path, stopped=True)
    assert load_messages("old", db_path=db_path)[1]["stopped"] is True


def _columns(db_path: Path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
