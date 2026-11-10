"""Questions it could not answer: what people asked that no document covered.

The conversation review (``memory.chat_review``) reads each turn after a chat
goes quiet. While it grades a turn it also says whether the person asked
something about the company (a policy, a number, who decides something) and
whether the reply knew it. A question lands here when all three hold:

1. The person asked about the company. Small talk and requests to do
   something don't count (the grader leaves ``question`` empty).
2. Nothing the turn read covered it: the reply's recorded sources
   (``chat_messages.sources``) name no company document. Built-in playbooks
   and web pages don't count as one.
3. The reply said it didn't know, guessed, or went to the web (the grader's
   ``answer`` is ``not_known``).

Only turns the review already reads are seen (verified speakers in chat and
chat apps), and never a turn kept private to its person (one that read their
own mail): those stay out of this list entirely.

**One line per question.** A question that matches an open line (the same
content words, give or take) adds to that line's count instead of adding a
new one. A line leaves the list when the owner adds a document for it, tells
the answer (kept as a standing fact, ``memory.facts``), or dismisses it, and
also when a later turn asks the same question and the reply knew it. A
dismissed question doesn't come back for anyone who had already asked it;
someone new asking it opens the line again.

**Who sees it.** The principal, on the Knowledge page, and only the lines
with at least one ask from a chat they may open (the Reflection screen's
rule, ``api.routes.reflection``). Deleting a chat drops its asks
(``forget_session``); a line with no asks left goes.

Table (in the episodic DB, created on first use):
  knowledge_gaps  id, question, match_key, status (open/resolved/dismissed),
                  times, first_at, last_at, person_id, place, askers,
                  sessions, dismissed_for, closed_at, closed_how
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "knowledge_gaps"
QUESTION_MAX = 200
KEEP_DAYS = 90
SESSIONS_MAX = 20
# Two questions are the same line when their content words overlap this much
# (Jaccard), or when a short one's words all sit inside a longer one's.
_SAME = 0.6
_CONTAINED = 0.85
_MIN_WORDS = 2
# Where a question was asked, by the turn's recorded surface.
_PLACES = {
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "google_chat": "Google Chat",
    "teams": "Teams",
}
# Source kinds that are not the company's own documents.
_NOT_COMPANY_SOURCES = frozenset({"knowledge", "web"})
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "by", "with", "from", "about", "as", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "done", "have", "has", "had", "i", "me", "my", "we", "our", "us", "you", "your", "it", "its", "this", "that", "these", "those", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "there", "here", "can", "could", "would", "should", "will", "shall", "may", "might", "must", "any", "some", "all", "please", "tell", "know", "let", "lets", "get", "got", "just", "not", "no", "yes", "so", "if", "than", "then", "them", "they", "their", "he", "she", "his", "her", "one"]
)
_WORD_RE = re.compile(r"[a-z0-9$%][a-z0-9$%.,'-]*")


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  question TEXT NOT NULL,"
        "  match_key TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open',"
        "  times INTEGER NOT NULL DEFAULT 1,"
        "  first_at TEXT NOT NULL,"
        "  last_at TEXT NOT NULL,"
        "  person_id INTEGER,"
        "  place TEXT NOT NULL DEFAULT 'chat',"
        "  askers TEXT NOT NULL DEFAULT '[]',"
        "  sessions TEXT NOT NULL DEFAULT '[]',"
        "  dismissed_for TEXT NOT NULL DEFAULT '[]',"
        "  closed_at TEXT,"
        "  closed_how TEXT"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_status ON {TABLE} (status, last_at)")
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def _words(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _WORD_RE.findall(str(text or "").casefold()):
        word = raw.strip(".,'-")
        if not word or word in _STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def match_key(question: str) -> str:
    """The question's content words, sorted: what two askings are compared on."""
    return " ".join(sorted(_words(question)))


def same_question(a: str, b: str) -> bool:
    """Whether two questions (or a question and a later message) ask the same
    thing, by their content words."""
    wa, wb = _words(a), _words(b)
    if len(wa) < _MIN_WORDS or len(wb) < _MIN_WORDS:
        return False
    shared = len(wa & wb)
    if shared / len(wa | wb) >= _SAME:
        return True
    small = min(len(wa), len(wb))
    return small >= 3 and shared / small >= _CONTAINED


def clean_question(question: str, message: str) -> str:
    """The grader's question, if the person's own message backs it: one line,
    capped, and at least half its content words in what they wrote, so the
    list never shows a question nobody asked. Empty otherwise."""
    text = " ".join(str(question or "").split())[:QUESTION_MAX].strip()
    words = _words(text)
    if len(words) < _MIN_WORDS:
        return ""
    said = _words(message)
    if len(words & said) * 2 < len(words):
        return ""
    return text


def place_for(surface: dict[str, Any] | None) -> str:
    """Where the turn happened, in the user's words: "chat", or the chat app."""
    channel = str((surface or {}).get("origin_channel") or "").casefold()
    return _PLACES.get(channel, "chat")


def has_company_source(sources: Any) -> bool:
    """Whether a reply's saved sources (``TurnSources.payload()``) name one of
    the company's own documents."""
    if not isinstance(sources, dict):
        return False
    items = sources.get("sources")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(s, dict) and str(s.get("kind") or "") not in _NOT_COMPANY_SOURCES for s in items
    )


def _load(text: Any) -> list[Any]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _find(conn: sqlite3.Connection, question: str) -> sqlite3.Row | None:
    """The newest line asking the same question, whatever its state."""
    for row in conn.execute(f"SELECT * FROM {TABLE} ORDER BY last_at DESC, id DESC"):  # noqa: S608
        if same_question(str(row["question"]), question):
            return row
    return None


def note_unanswered(
    question: str,
    *,
    person_id: int,
    session_id: str,
    place: str = "chat",
    asked_at: datetime | None = None,
    db_path: Path | None = None,
) -> int | None:
    """Put a question it couldn't answer on the list, or add to the count of
    the line that already asks it. Returns the line's id, or None when it was
    left off (dismissed for this person, or too short to match). Never raises."""
    try:
        text = " ".join(str(question or "").split())[:QUESTION_MAX]
        key = match_key(text)
        if len(key.split()) < _MIN_WORDS:
            return None
        when = (asked_at or _now()).isoformat()
        conn = _connect(db_path)
        try:
            row = _find(conn, text)
            if row is None:
                cur = conn.execute(
                    f"INSERT INTO {TABLE} (question, match_key, first_at, last_at, person_id, "  # noqa: S608
                    "place, askers, sessions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (text, key, when, when, int(person_id), place,
                     json.dumps([int(person_id)]), json.dumps([str(session_id)])),
                )
                line_id = int(cur.lastrowid or 0)
            else:
                line_id = int(row["id"])
                if row["status"] == "dismissed" and int(person_id) in _load(row["dismissed_for"]):
                    return None
                askers = _load(row["askers"])
                if int(person_id) not in askers:
                    askers.append(int(person_id))
                sessions = [s for s in _load(row["sessions"]) if s != str(session_id)]
                sessions = (sessions + [str(session_id)])[-SESSIONS_MAX:]
                reopened = row["status"] != "open"
                conn.execute(
                    f"UPDATE {TABLE} SET status = 'open', times = ?, last_at = ?, person_id = ?, "  # noqa: S608
                    "place = ?, askers = ?, sessions = ?, closed_at = NULL, closed_how = NULL "
                    "WHERE id = ?",
                    (1 if reopened else int(row["times"]) + 1, when, int(person_id), place,
                     json.dumps(askers), json.dumps(sessions), line_id),
                )
            _prune(conn)
            conn.commit()
            return line_id
        finally:
            conn.close()
    except Exception:
        logger.warning("knowledge_gaps: couldn't note the question", exc_info=True)
        return None


def note_answered(question: str, *, db_path: Path | None = None) -> int:
    """A later turn asked the same thing and the reply knew it: the open line
    for it leaves the list. Returns how many lines closed. Never raises."""
    try:
        conn = _connect(db_path)
        try:
            closed = 0
            for row in conn.execute(f"SELECT id, question FROM {TABLE} WHERE status = 'open'").fetchall():  # noqa: S608
                if same_question(str(row["question"]), question):
                    conn.execute(
                        f"UPDATE {TABLE} SET status = 'resolved', closed_at = ?, closed_how = 'answered' "  # noqa: S608
                        "WHERE id = ?",
                        (_now().isoformat(), int(row["id"])),
                    )
                    closed += 1
            conn.commit()
            return closed
        finally:
            conn.close()
    except Exception:
        logger.warning("knowledge_gaps: couldn't close an answered question", exc_info=True)
        return 0


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = (_now() - timedelta(days=KEEP_DAYS)).isoformat()
    conn.execute(f"DELETE FROM {TABLE} WHERE last_at < ?", (cutoff,))  # noqa: S608


def _out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "question": str(row["question"]),
        "status": str(row["status"]),
        "times": int(row["times"]),
        "first_at": str(row["first_at"]),
        "last_at": str(row["last_at"]),
        "person_id": int(row["person_id"]) if row["person_id"] is not None else None,
        "place": str(row["place"] or "chat"),
        "askers": [int(a) for a in _load(row["askers"])],
        "sessions": [str(s) for s in _load(row["sessions"])],
    }


def list_open(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """The open lines, the most recently asked first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE status = 'open' ORDER BY last_at DESC, id DESC"  # noqa: S608
        ).fetchall()
    finally:
        conn.close()
    return [_out(r) for r in rows]


def get(line_id: int, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (int(line_id),)).fetchone()  # noqa: S608
    finally:
        conn.close()
    return _out(row) if row is not None else None


def close(line_id: int, how: str, *, db_path: Path | None = None) -> bool:
    """Take an open line off the list: ``document`` (a document was added for
    it), ``taught`` (the owner told the answer) or ``dismissed`` (it won't come
    back for anyone who had asked it). False when it isn't open."""
    if how not in ("document", "taught", "dismissed"):
        raise ValueError(f"unknown way to close: {how}")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT askers FROM {TABLE} WHERE id = ? AND status = 'open'", (int(line_id),)  # noqa: S608
        ).fetchone()
        if row is None:
            return False
        status = "dismissed" if how == "dismissed" else "resolved"
        dismissed_for = row["askers"] if how == "dismissed" else "[]"
        conn.execute(
            f"UPDATE {TABLE} SET status = ?, closed_at = ?, closed_how = ?, dismissed_for = ? "  # noqa: S608
            "WHERE id = ?",
            (status, _now().isoformat(), how, dismissed_for, int(line_id)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def forget_session(session_id: str, *, db_path: Path | None = None) -> None:
    """Drop one chat's asks (its chat was deleted). A line with no asks left
    goes. A DB without the table is a no-op."""
    from openexecutive.memory.episodic import _resolve_db_path

    if not _resolve_db_path(db_path).exists():
        return
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return
    try:
        for row in conn.execute(f"SELECT id, sessions FROM {TABLE}").fetchall():  # noqa: S608
            sessions = [str(s) for s in _load(row["sessions"])]
            if str(session_id) not in sessions:
                continue
            left = [s for s in sessions if s != str(session_id)]
            if left:
                conn.execute(f"UPDATE {TABLE} SET sessions = ? WHERE id = ?",  # noqa: S608
                             (json.dumps(left), int(row["id"])))
            else:
                conn.execute(f"DELETE FROM {TABLE} WHERE id = ?", (int(row["id"]),))  # noqa: S608
        conn.commit()
    except sqlite3.Error:
        logger.warning("knowledge_gaps: couldn't forget a chat", exc_info=True)
    finally:
        conn.close()
