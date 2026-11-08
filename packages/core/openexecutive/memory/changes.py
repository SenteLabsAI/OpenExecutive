"""What it remembered lately: one person's own memory changes since a time.

The chat's "Memory updated" chip and the Memory page's "New since your last
visit" read this. It gathers, newest first, what was kept for one person
after ``since`` (optionally in one conversation):

- ``fact`` / ``decision``: standing facts they recorded (``memory.facts``;
  for the principal, every fact recorded as the principal), in force or
  waiting for approval. Profile edits are left out.
- ``worked_out``: figures it worked out for them (``memory.worked_out``).
- ``lesson``: lessons the chat review learned from their chats, still live
  (``memory.lessons``).
- ``note``: their own Always in the loop notes, still live
  (``memory.history``).

Only the person's own rows are read, so narrowing by conversation needs no
access check: a conversation id that isn't theirs simply matches nothing.
``can_undo`` says whether the person may take it back with the same route
the Memory page uses (a teammate can't retire a fact the principal approved,
or one still waiting). Never raises: a source that can't be read is skipped.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_CHANGES = 50


def normalize_since(raw: str | None) -> str | None:
    """``raw`` as the UTC ISO string the stores write, or None when it isn't
    a time. A time without a zone is read as UTC."""
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def _facts(person_id: int, principal: bool, since: str, session_id: str | None,
           db_path: Path | None) -> list[dict[str, Any]]:
    from openexecutive.memory import facts

    where = (
        "created_at > ? AND status IN ('active', 'proposed') AND kind != 'profile' "
        "AND (recorded_by_person_id = ? OR (? AND recorded_by_role = 'principal'))"
    )
    params: list[Any] = [since, person_id, 1 if principal else 0]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    with facts._conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, statement, status, decision, recorded_by_role, approved_at, created_at "  # noqa: S608
            f"FROM facts WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, MAX_CHANGES),
        ).fetchall()
    out = []
    for r in rows:
        own_teammate = r["recorded_by_role"] == "teammate" and r["approved_at"] is None
        out.append({
            "kind": "decision" if r["decision"] else "fact",
            "id": int(r["id"]),
            "title": r["statement"],
            "source": "",
            "status": r["status"],
            "created_at": r["created_at"],
            "can_undo": r["status"] == "active" and (principal or own_teammate),
        })
    return out


def _figures(person_id: int, since: str, session_id: str | None, db_path: Path | None) -> list[dict[str, Any]]:
    from openexecutive.memory import worked_out

    where, params = "person_id = ? AND created_at > ?", [person_id, since]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    conn = worked_out._connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, subject, figure, source, created_at FROM {worked_out.TABLE} "  # noqa: S608
            f"WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, MAX_CHANGES),
        ).fetchall()
    finally:
        conn.close()
    return [{
        "kind": "worked_out", "id": int(r["id"]), "title": f"{r['subject']}: {r['figure']}",
        "source": r["source"], "status": "active", "created_at": r["created_at"], "can_undo": True,
    } for r in rows if not worked_out._status_like(r["subject"], r["figure"], r["source"])]


def _lessons(person_id: int, since: str, session_id: str | None, db_path: Path | None) -> list[dict[str, Any]]:
    from openexecutive.memory import lessons

    where, params = "person_id = ? AND created_at > ? AND retired_at IS NULL", [person_id, since]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    conn = lessons._connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, text, created_at FROM {lessons.TABLE} "  # noqa: S608
            f"WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, MAX_CHANGES),
        ).fetchall()
    finally:
        conn.close()
    return [{
        "kind": "lesson", "id": int(r["id"]), "title": r["text"], "source": "",
        "status": "active", "created_at": r["created_at"], "can_undo": True,
    } for r in rows]


def _notes(person_id: int, since: str, session_id: str | None, db_path: Path | None) -> list[dict[str, Any]]:
    from openexecutive.memory import history
    from openexecutive.memory.history_schema import NOTES_TABLE

    if not history._db_path(db_path).exists():
        return []
    where = f"person_id = ? AND created_at > ? AND {history._live_clause()}"
    params: list[Any] = [person_id, since, datetime.now(UTC).isoformat()]
    if session_id:
        # A web chat's notes are keyed by its session id (history_chat).
        where += " AND conversation_key = ?"
        params.append(history.conversation_key("web", session_id))
    conn = history._connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, summary, correction, created_at FROM {NOTES_TABLE} "  # noqa: S608
            f"WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, MAX_CHANGES),
        ).fetchall()
    finally:
        conn.close()
    return [{
        "kind": "note", "id": int(r[0]), "title": r[2] or r[1], "source": "",
        "status": "active", "created_at": r[3], "can_undo": True,
    } for r in rows]


def changes_since(
    person_id: int,
    *,
    principal: bool,
    since: str,
    session_id: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """The person's own memory changes after ``since`` (a UTC ISO string),
    newest first, at most :data:`MAX_CHANGES`."""
    found: list[dict[str, Any]] = []
    for name, read in (
        ("facts", lambda: _facts(person_id, principal, since, session_id, db_path)),
        ("worked_out", lambda: _figures(person_id, since, session_id, db_path)),
        ("lessons", lambda: _lessons(person_id, since, session_id, db_path)),
        ("notes", lambda: _notes(person_id, since, session_id, db_path)),
    ):
        try:
            found.extend(read())
        except Exception:
            logger.warning("memory changes: couldn't read %s", name, exc_info=True)
    found.sort(key=lambda c: str(c["created_at"]), reverse=True)
    return found[:MAX_CHANGES]
