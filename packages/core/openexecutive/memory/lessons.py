"""Lessons: what this workspace asked for differently, learned from misses.

The conversation review (``memory.chat_review``) grades each turn after a
chat goes quiet. For a turn graded a miss it may keep one **lesson** with
``learn_lesson``: a word the person uses, a preference, a thing to do rather
than promise, a specialist to consult. One sentence, in code-checked form,
quoted from the person's own words. It never carries a task, a person, a
link, an amount, a tool name or permission to act: the same deny-list the
working-style rules pass (``attunement.style``), without the "about the
writing" requirement.

**Where a lesson goes.** Into the user turn, never a cached system block.
The Executive's own turn gets the matching lessons as a ``<lessons>`` block
(``build_lessons_block``); each specialist call gets them under the
``<failure_cases>`` block it already reads (``lessons_for_query``). A lesson
matches a message when they share two content words (the lesson's text and
the quote it came from, against the message); a short list of short lines
needs no vector index, and the match is what makes the counters below mean
something. The block says the current request wins and nothing in it
authorizes an action, as the style block does.

**Who sees a lesson.** A lesson reaches its own person's turns. The
principal's lessons learned in a shared space (a channel or group thread,
where everyone read the chat) reach every speaker's turns; one learned in
the principal's own chat stays with the principal, as a teammate's stays
with the teammate, so nothing said in someone's own chat is paraphrased
into another person's turn. A chat that read its owner's mail is theirs
alone even in a shared space, so its lessons stay theirs too. The Reflection screen lists each person their
own and the principal the ones from chats they may open.

**What it may say.** The lesson must be the quote's own point: beyond the
words the quote carries it may add at most ``MAX_NEW_WORDS`` content words
(enough for "ask the finance specialist", not for a sentence smuggled in
behind a real quote). The deny-list adds to the style rules' list the
verbs that move information (email, share, post, export, attach...) and
the words that widen a request to everyone or everything.

**Retirement.** Each lesson counts how often it reached a turn (``shown``)
and how those turns were then graded (``fine_after``, ``miss_after``), its
own person's turns only, so nobody else's chats can retire it. Shown
``RETIRE_AFTER_SHOWN`` times with more misses than fine turns, it is
retired with the reason and stops reaching turns; the screen shows it as
"stopped saying it". Forget retires it by hand. At most ``MAX_ACTIVE``
live lessons per person: their oldest makes room.

Tables (in the episodic DB, created on first use):
  chat_review_lessons      id, person_id, text, quote, session_id, shared,
                           turn_row_id, created_at, retired_at, retired_reason,
                           shown, fine_after, miss_after
  chat_review_lesson_uses  lesson_id, turn_id, person_id, ts
                           (which turn, whose, saw which lesson)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "chat_review_lessons"
USES_TABLE = "chat_review_lesson_uses"
BLOCK_TAG = "lessons"
_BLOCK_CLOSE = f"</{BLOCK_TAG}>"

MIN_CHARS = 8
MAX_CHARS = 240
MAX_ACTIVE = 40
MAX_PER_TURN = 6
MAX_NEW_WORDS = 2
RETIRE_AFTER_SHOWN = 5
# Two shared content words, always: one common word ("report") matches too
# much, so a lesson with a single content word reaches nothing.
_MATCH_WORDS = 2

# Beyond the style rules' deny-list (which has the acting verbs and the
# instruction talk): the verbs that move information somewhere, and the
# words that widen a request to everyone or everything. A lesson is a
# definition or a preference; none of these belongs in one.
_LESSON_DENY = re.compile(
    r"\b(email|emails|emailed|mail|mails|mailed|share|shares|shared|post|posts|posted|"
    r"publish\w*|release\w*|submit\w*|export\w*|attach\w*|upload\w*|"
    r"give|gives|gave|hand|hands|handed|tell|tells|told|show|shows|showed|"
    r"disclos\w*|spell out|copy|copies|paste|whoever|whomever|anyone|anybody|"
    r"everyone|everybody|whole|entire|full|all of|every request|every message|"
    r"treat|treats|treated|approved|external|outside|channel|channels|address|addresses)\b",
    re.IGNORECASE,
)

REASON_UNHELPFUL = "unhelpful"
REASON_FORGOTTEN = "forgotten"
REASON_ROOM = "room"
# The replay (memory.replay) found it didn't flip its miss, or broke a turn
# that was fine.
REASON_REPLAY = "replay"


def _now() -> datetime:
    return datetime.now(UTC)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    from openexecutive.memory.episodic import _resolve_db_path

    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  person_id INTEGER NOT NULL,"
        "  text TEXT NOT NULL,"
        "  quote TEXT NOT NULL,"
        "  session_id TEXT NOT NULL,"
        "  shared INTEGER NOT NULL DEFAULT 0,"
        "  turn_row_id INTEGER,"
        "  created_at TEXT NOT NULL,"
        "  retired_at TEXT,"
        "  retired_reason TEXT,"
        "  shown INTEGER NOT NULL DEFAULT 0,"
        "  fine_after INTEGER NOT NULL DEFAULT 0,"
        "  miss_after INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {USES_TABLE} ("
        "  lesson_id INTEGER NOT NULL,"
        "  turn_id TEXT NOT NULL,"
        "  person_id INTEGER,"
        "  ts TEXT NOT NULL,"
        "  PRIMARY KEY (lesson_id, turn_id)"
        ")"
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def lesson_rejection(text: str, *, roster_names: list[str] | None = None) -> str | None:
    """Why ``text`` may not be a lesson, or None when it may. The style
    rules' deny-list (no link, handle, amount, markup, identifier, action
    verb, instruction talk, nudge to act unchecked, roster name), without
    the requirement that it be about the writing."""
    from openexecutive.attunement import style

    if len(text) < MIN_CHARS:
        return "too_short"
    if len(text) > MAX_CHARS:
        return "too_long"
    if style._non_latin_letter(text):
        return "non_latin_letters"
    for pattern in style._DENY_PATTERNS:
        if pattern.search(text):
            return "denied_content"
    if _LESSON_DENY.search(text):
        return "denied_content"
    names = roster_names if roster_names is not None else style._roster_names()
    if style._names_a_person(text, names):
        return "names_a_person"
    return None


def lesson_overreach(text: str, quote: str) -> str | None:
    """Why ``text`` says more than ``quote`` does, or None: a lesson may add
    at most ``MAX_NEW_WORDS`` content words to the quote it rests on."""
    new = _content_words(text) - _content_words(quote)
    if len(new) > MAX_NEW_WORDS:
        return "the lesson says more than the quote does"
    return None


def _normalize(text: str) -> str:
    from openexecutive.attunement.style import _normalize

    return _normalize(text)


def _shared_space(session_id: str, db_path: Path | None = None) -> bool:
    """Whether the chat was a channel or group thread everyone there read
    (``api.routes.chat._shared_space``) and never read its owner's mail
    (``session_store.mark_mail_private``: then it is theirs alone, wherever
    it happened). Fails closed: not shared."""
    try:
        from openexecutive.api.routes.chat import _shared_space
        from openexecutive.memory.episodic import _resolve_db_path
        from openexecutive.memory.session_store import session_mail_private

        if not _shared_space(str(session_id)):
            return False
        return not session_mail_private(str(session_id), db_path=_resolve_db_path(db_path))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Keeping and forgetting
# --------------------------------------------------------------------------- #


def add_lesson(
    person_id: int,
    text: str,
    *,
    quote: str,
    session_id: str,
    turn_row_id: int | None,
    db_path: Path | None = None,
    private: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Keep one lesson; ``(lesson, None)`` when stored, ``(None, reason)``
    when not. The caller has checked ``quote`` against the person's words.
    ``private``: learned from a chat that read mail, so never shared.
    A duplicate of a live lesson is refused; past ``MAX_ACTIVE`` live
    lessons the oldest retires to make room."""
    clean = _normalize(text)
    rejection = lesson_rejection(clean)
    if rejection:
        return None, rejection
    now = _now().isoformat()
    shared = not private and _shared_space(session_id, db_path)
    conn = _connect(db_path)
    try:
        # One writer at a time: two reviews landing together must not both
        # pass the duplicate check or both skip making room.
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute(
            f"SELECT id, text FROM {TABLE} WHERE retired_at IS NULL AND person_id = ? "  # noqa: S608
            "ORDER BY created_at, id",
            (int(person_id),),
        ).fetchall()
        if any(_same(str(row["text"]), clean) for row in live):
            conn.rollback()
            return None, "duplicate"
        for row in live[: max(0, len(live) - MAX_ACTIVE + 1)]:
            conn.execute(
                f"UPDATE {TABLE} SET retired_at = ?, retired_reason = ? WHERE id = ?",  # noqa: S608
                (now, REASON_ROOM, int(row["id"])),
            )
        cur = conn.execute(
            f"INSERT INTO {TABLE} (person_id, text, quote, session_id, shared, turn_row_id, created_at) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(person_id), clean, _normalize(quote)[:400], str(session_id), 1 if shared else 0,
             turn_row_id, now),
        )
        conn.commit()
        row_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    _audit(f"lesson kept for person {person_id}", {"op": "learn_lesson", "lesson_id": row_id, "person_id": person_id})
    return get_lesson(row_id, db_path=db_path), None


def _same(a: str, b: str) -> bool:
    return a.casefold().rstrip(".!") == b.casefold().rstrip(".!")


def get_lesson(lesson_id: int, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (int(lesson_id),)).fetchone()  # noqa: S608
    finally:
        conn.close()
    return _out(row) if row is not None else None


def list_lessons(
    *, person_id: int | None, limit: int = 200, offset: int = 0, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Live lessons first, newest first, then the retired ones: one
    person's, or everyone's when ``person_id`` is None, ``offset`` rows in
    (the principal's page is read in parts, like the misses)."""
    conn = _connect(db_path)
    try:
        where, params = "1 = 1", []
        if person_id is not None:
            where, params = "person_id = ?", [int(person_id)]
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE {where} "  # noqa: S608
            "ORDER BY (retired_at IS NOT NULL), created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, max(1, min(int(limit), 1000)), max(0, int(offset))),
        ).fetchall()
    finally:
        conn.close()
    return [_out(r) for r in rows]


def forget_lesson(lesson_id: int, *, caller_person_id: int | None, db_path: Path | None = None) -> bool:
    """Retire one lesson by hand: its own person or the principal may.
    False when there is no such lesson, it is already retired, or the
    caller may not."""
    from openexecutive.people.store import is_principal_or_self

    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT person_id, retired_at FROM {TABLE} WHERE id = ?", (int(lesson_id),)  # noqa: S608
        ).fetchone()
        if row is None or row["retired_at"] is not None:
            return False
        if not is_principal_or_self(caller_person_id, int(row["person_id"])):
            return False
        conn.execute(
            f"UPDATE {TABLE} SET retired_at = ?, retired_reason = ? WHERE id = ?",  # noqa: S608
            (_now().isoformat(), REASON_FORGOTTEN, int(lesson_id)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def retire_lesson(lesson_id: int, reason: str, *, db_path: Path | None = None) -> bool:
    """Retire one live lesson for ``reason`` (the replay's gate, memory.replay).
    False when there is no such live lesson. Never raises."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                f"UPDATE {TABLE} SET retired_at = ?, retired_reason = ? "  # noqa: S608
                "WHERE id = ? AND retired_at IS NULL",
                (_now().isoformat(), reason, int(lesson_id)),
            )
            changed = bool(conn.execute("SELECT changes()").fetchone()[0])
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("lessons: couldn't retire %s", lesson_id, exc_info=True)
        return False
    if changed:
        _audit(f"lesson {lesson_id} retired: {reason}",
               {"op": "retire_lesson", "lesson_id": int(lesson_id), "reason": reason})
    return changed


def lessons_since(since: str | None, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    """The live lessons learned after ``since`` (an ISO time; None = all of
    them), oldest first. Never raises."""
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} WHERE retired_at IS NULL AND created_at > ? "  # noqa: S608
                "ORDER BY created_at, id",
                (since or "",),
            ).fetchall()
        finally:
            conn.close()
        return [_out(r) for r in rows]
    except Exception:
        logger.debug("lessons: lookup failed", exc_info=True)
        return []


def _out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "person_id": int(row["person_id"]),
        "text": str(row["text"]),
        "quote": str(row["quote"]),
        "session_id": str(row["session_id"]),
        "shared": bool(row["shared"]),
        "turn_row_id": int(row["turn_row_id"]) if row["turn_row_id"] is not None else None,
        "created_at": str(row["created_at"]),
        "retired_at": str(row["retired_at"]) if row["retired_at"] else None,
        "retired_reason": str(row["retired_reason"]) if row["retired_reason"] else None,
        "shown": int(row["shown"]),
        "fine_after": int(row["fine_after"]),
        "miss_after": int(row["miss_after"]),
    }


# --------------------------------------------------------------------------- #
# Reaching a turn
# --------------------------------------------------------------------------- #

_STOPWORDS = frozenset(
    ["the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "my", "me", "i", "it", "is", "was",
     "that", "this", "with", "from", "at", "by", "be", "please", "can", "you", "your", "we", "our",
     "they", "them", "he", "she", "his", "her", "its", "are", "were", "has", "have", "had", "will",
     "would", "should", "could", "just", "about", "out", "off", "up", "down", "into", "over", "when",
     "what", "than", "then", "there", "here", "also", "always", "never", "rather", "instead", "want",
     "wants", "wanted", "need", "needs", "thing", "things", "something", "every", "each", "some",
     "more", "most", "very", "really", "like", "make", "made", "does", "did", "done", "mean", "means"]
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.casefold()) if len(w) >= 4 and w not in _STOPWORDS}


def _matches(lesson: dict[str, Any], message_words: set[str]) -> bool:
    own = _content_words(lesson["text"]) | _content_words(lesson["quote"])
    return len(own & message_words) >= _MATCH_WORDS


def lessons_for(
    person_id: int | None, message: str, *, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """The live lessons that reach this speaker's turn about ``message``:
    the speaker's own, and the principal's from shared spaces. Never
    raises."""
    if person_id is None:
        return []
    try:
        from openexecutive.people.store import find_principal_person

        words = _content_words(message)
        if not words:
            return []
        principal = find_principal_person()
        principal_id = int(principal.id) if principal is not None and principal.id is not None else -1
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} WHERE retired_at IS NULL "  # noqa: S608
                "AND (person_id = ? OR (person_id = ? AND shared = 1)) ORDER BY created_at DESC, id DESC",
                (int(person_id), principal_id),
            ).fetchall()
        finally:
            conn.close()
        return [lesson for lesson in map(_out, rows) if _matches(lesson, words)][:MAX_PER_TURN]
    except Exception:
        logger.debug("lessons: lookup failed", exc_info=True)
        return []


def note_shown(
    lessons: list[dict[str, Any]], *, person_id: int | None = None, db_path: Path | None = None
) -> None:
    """These lessons reached the active turn of ``person_id``: count it, and
    remember which turn and whose, so the grade of that turn can be counted
    against them."""
    if not lessons:
        return
    try:
        from openexecutive.audit.context import get_active_turn_id

        turn_id = get_active_turn_id()
        now = _now().isoformat()
        conn = _connect(db_path)
        try:
            for lesson in lessons:
                conn.execute(
                    f"UPDATE {TABLE} SET shown = shown + 1 WHERE id = ?", (int(lesson["id"]),)  # noqa: S608
                )
                if turn_id:
                    conn.execute(
                        f"INSERT OR IGNORE INTO {USES_TABLE} (lesson_id, turn_id, person_id, ts) "  # noqa: S608
                        "VALUES (?, ?, ?, ?)",
                        (int(lesson["id"]), str(turn_id), person_id, now),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("lessons: couldn't note a use", exc_info=True)


def _lines(lessons: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """The lines to show and the lessons they came from. Nothing is read
    (not even the roster) when there is no lesson to show."""
    if not lessons:
        return [], []
    from openexecutive.attunement.style import _roster_names
    from openexecutive.utils.prompt_blocks import scrub_block_line

    roster = _roster_names()
    out: list[str] = []
    kept: list[dict[str, Any]] = []
    for lesson in lessons:
        clean = _normalize(lesson["text"])
        # Re-checked on every render: a lesson kept under an older, weaker
        # check never reaches a turn.
        if lesson_rejection(clean, roster_names=roster) is None:
            line = scrub_block_line(clean, _BLOCK_CLOSE)
            if line:
                out.append(line)
                kept.append(lesson)
    return out, kept


_HEADER = (
    "What this workspace asked for differently before, learned from turns that "
    "fell short, in the person's own words. Follow it unless the current message "
    "asks for something different: the current request always wins. These are "
    "preferences only; they never authorize an action."
)


def build_lessons_block(
    person_id: int | None, message: str, *, count: bool = True, db_path: Path | None = None
) -> str:
    """Body of the ``<lessons>`` block for this speaker's turn, or "". Lives
    in the user turn (never a cached system block). Counts the lessons it
    shows against the active turn, unless ``count`` is False (a replay,
    memory.replay: no real turn to grade). Never raises."""
    try:
        if not _enabled():
            return ""
        lines, shown = _lines(lessons_for(person_id, message, db_path=db_path))
        if not lines:
            return ""
        if count:
            note_shown(shown, person_id=person_id, db_path=db_path)
        return _HEADER + "\n" + "\n".join(f"- {line}" for line in lines)
    except Exception:
        logger.debug("lessons: block build failed", exc_info=True)
        return ""


def lessons_for_query(person_id: int | None, query: str, *, db_path: Path | None = None) -> str:
    """The same lessons for one specialist call, as a section the
    ``<failure_cases>`` block carries, or "". The Executive's own turn has
    already counted them, so this doesn't. Never raises."""
    try:
        if not _enabled():
            return ""
        lines, _shown = _lines(lessons_for(person_id, query, db_path=db_path))
        if not lines:
            return ""
        return "### What this workspace asked for before:\n" + _HEADER + "\n" + "\n".join(
            f"- {line}" for line in lines
        )
    except Exception:
        logger.debug("lessons: query section failed", exc_info=True)
        return ""


def _enabled() -> bool:
    try:
        from openexecutive.config import get_settings

        return bool(get_settings().chat_review_enabled)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Counting how it went
# --------------------------------------------------------------------------- #


def note_outcome(turn_id: str | None, verdict: str, *, db_path: Path | None = None) -> list[int]:
    """The turn ``turn_id`` was graded ``verdict``: count it for every
    lesson that reached that turn and belongs to the turn's own person (so
    nobody else's chats can retire it), and retire the ones it shows didn't
    help. The ids of the lessons retired. Never raises."""
    if not turn_id or verdict not in ("fine", "miss"):
        return []
    column = "fine_after" if verdict == "fine" else "miss_after"
    retired: list[int] = []
    try:
        conn = _connect(db_path)
        try:
            ids = [int(r["lesson_id"]) for r in conn.execute(
                f"SELECT u.lesson_id FROM {USES_TABLE} u JOIN {TABLE} l ON l.id = u.lesson_id "  # noqa: S608
                "WHERE u.turn_id = ? AND u.person_id = l.person_id",
                (str(turn_id),),
            ).fetchall()]
            for lesson_id in ids:
                conn.execute(
                    f"UPDATE {TABLE} SET {column} = {column} + 1 WHERE id = ?", (lesson_id,)  # noqa: S608
                )
                row = conn.execute(
                    f"SELECT shown, fine_after, miss_after, retired_at FROM {TABLE} WHERE id = ?",  # noqa: S608
                    (lesson_id,),
                ).fetchone()
                if (
                    row is not None and row["retired_at"] is None
                    and int(row["shown"]) >= RETIRE_AFTER_SHOWN
                    and int(row["miss_after"]) > int(row["fine_after"])
                ):
                    conn.execute(
                        f"UPDATE {TABLE} SET retired_at = ?, retired_reason = ? WHERE id = ?",  # noqa: S608
                        (_now().isoformat(), REASON_UNHELPFUL, lesson_id),
                    )
                    retired.append(lesson_id)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("lessons: couldn't count an outcome", exc_info=True)
        return retired
    for lesson_id in retired:
        _audit(f"lesson {lesson_id} retired: the turns it reached went worse, not better",
               {"op": "retire_lesson", "lesson_id": lesson_id, "reason": REASON_UNHELPFUL})
    return retired


def _audit(summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event

        log_event("chat_review", summary, actor="chat_review", details=details)
    except Exception:
        logger.debug("lessons: audit failed", exc_info=True)
