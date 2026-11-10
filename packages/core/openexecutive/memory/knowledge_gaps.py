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

**One line per question.** A question that matches one of its asker's lines
(the same content words, give or take) is kept as another ask on it instead
of a new line. Lines are per person; the Knowledge page shows lines that ask
the same question as one row. A line leaves the list when the owner adds a document for it, tells
the answer (kept as a standing fact, ``memory.facts``), or dismisses it, and
also when the same person asks it again and the reply drew on a company
document. A
dismissed question doesn't come back for whoever asked it.

**Who sees it.** The principal, on the Knowledge page. Each asking is kept
on its own (who, which chat, their wording, where, when), and what the
principal sees of a line is built only from the asks in chats they may open
(the Reflection screen's rule, ``api.routes.reflection``): the wording, who
asked, where, when and the count. A line with no such ask isn't shown, so
merging never carries a teammate's own chat into the principal's view.
Deleting a chat drops its asks (``forget_session``); a line with no asks
left goes.

Tables (in the episodic DB, created on first use):
  knowledge_gaps      id, match_key, person_id, status (open/resolved/dismissed),
                      dismissed_for, closed_at, closed_how
  knowledge_gap_asks  id, line_id, person_id, session_id, question, place,
                      asked_at
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
ASKS = "knowledge_gap_asks"
# A standing fact's subject holds this much (memory.facts.SUBJECT_MAX), and
# "Tell me the answer" keeps the question as the subject.
QUESTION_MAX = 120
KEEP_DAYS = 90
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
# Source kinds that are the company's own documents (orchestrator
# .answer_sources.SourceKind): uploads and the synced sources. Built-in
# playbooks, web pages, research results and the person's own attachments are
# not, so a new kind counts only once it is added here.
_COMPANY_SOURCES = frozenset({"company", "notion", "drive", "onedrive", "confluence"})
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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  match_key TEXT NOT NULL,"
        "  person_id INTEGER NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open',"
        "  dismissed_for TEXT NOT NULL DEFAULT '[]',"
        "  closed_at TEXT,"
        "  closed_how TEXT"
        ")"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ASKS} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        f"  line_id INTEGER NOT NULL REFERENCES {TABLE}(id) ON DELETE CASCADE,"
        "  person_id INTEGER NOT NULL,"
        "  session_id TEXT NOT NULL,"
        "  question TEXT NOT NULL,"
        "  place TEXT NOT NULL DEFAULT 'chat',"
        "  asked_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ASKS}_line ON {ASKS} (line_id, asked_at)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ASKS}_session ON {ASKS} (session_id)")
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
        isinstance(s, dict) and str(s.get("kind") or "") in _COMPANY_SOURCES for s in items
    )


def _load(text: Any) -> list[Any]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _find(conn: sqlite3.Connection, question: str, person_id: int) -> sqlite3.Row | None:
    """This person's line one of whose asks asks the same question, whatever
    its state; the most recently asked first. Lines are per person, so no one
    else's asking (which the viewer may not be allowed to read) decides where
    an ask goes."""
    rows = conn.execute(
        f"SELECT l.*, a.question AS asked FROM {TABLE} l JOIN {ASKS} a ON a.line_id = l.id "  # noqa: S608
        "WHERE l.person_id = ? ORDER BY a.asked_at DESC, a.id DESC",
        (int(person_id),),
    ).fetchall()
    for row in rows:
        if same_question(str(row["asked"]), question):
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
    """Keep one asking of a question it couldn't answer, on the line that
    already asks it or a new one. Returns the line's id, or None when it was
    left off (dismissed for this person, or too short to match). Never raises."""
    try:
        text = " ".join(str(question or "").split())[:QUESTION_MAX]
        key = match_key(text)
        if len(key.split()) < _MIN_WORDS:
            return None
        when = (asked_at or _now()).isoformat()
        conn = _connect(db_path)
        try:
            row = _find(conn, text, int(person_id))
            if row is None:
                cur = conn.execute(
                    f"INSERT INTO {TABLE} (match_key, person_id) VALUES (?, ?)",  # noqa: S608
                    (key, int(person_id)),
                )
                line_id = int(cur.lastrowid or 0)
            else:
                line_id = int(row["id"])
                if row["status"] == "dismissed" and int(person_id) in _load(row["dismissed_for"]):
                    return None
                if row["status"] != "open":
                    # Back on the list: counted afresh from this asking.
                    conn.execute(f"DELETE FROM {ASKS} WHERE line_id = ?", (line_id,))  # noqa: S608
                    conn.execute(
                        f"UPDATE {TABLE} SET status = 'open', closed_at = NULL, closed_how = NULL "  # noqa: S608
                        "WHERE id = ?",
                        (line_id,),
                    )
            conn.execute(
                f"INSERT INTO {ASKS} (line_id, person_id, session_id, question, place, asked_at) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?)",
                (line_id, int(person_id), str(session_id), text, place, when),
            )
            _prune(conn)
            conn.commit()
            return line_id
        finally:
            conn.close()
    except Exception:
        logger.warning("knowledge_gaps: couldn't note the question", exc_info=True)
        return None


def note_answered(question: str, *, person_id: int, db_path: Path | None = None) -> int:
    """A later turn by the same person asked the same thing and its reply
    drew on a company document: their open line for it leaves the list. Only
    their own lines, so no one's turn closes (or learns of) anyone else's. Returns how many lines
    closed. Never raises."""
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT DISTINCT l.id, a.question FROM {TABLE} l JOIN {ASKS} a ON a.line_id = l.id "  # noqa: S608
                "WHERE l.status = 'open' AND l.person_id = ?",
                (int(person_id),),
            ).fetchall()
            ids = {int(r["id"]) for r in rows if same_question(str(r["question"]), question)}
            for line_id in ids:
                conn.execute(
                    f"UPDATE {TABLE} SET status = 'resolved', closed_at = ?, closed_how = 'answered' "  # noqa: S608
                    "WHERE id = ?",
                    (_now().isoformat(), line_id),
                )
            conn.commit()
            return len(ids)
        finally:
            conn.close()
    except Exception:
        logger.warning("knowledge_gaps: couldn't close an answered question", exc_info=True)
        return 0


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = (_now() - timedelta(days=KEEP_DAYS)).isoformat()
    # A dismissed line keeps its asks, so the person it was dismissed for is
    # still matched and it never comes back for them.
    conn.execute(
        f"DELETE FROM {ASKS} WHERE asked_at < ? "  # noqa: S608
        f"AND line_id NOT IN (SELECT id FROM {TABLE} WHERE status = 'dismissed')",
        (cutoff,),
    )
    conn.execute(f"DELETE FROM {TABLE} WHERE id NOT IN (SELECT line_id FROM {ASKS})")  # noqa: S608


def _lines(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    lines = conn.execute(f"SELECT * FROM {TABLE} WHERE {where} ORDER BY id", params).fetchall()  # noqa: S608
    out: list[dict[str, Any]] = []
    for line in lines:
        asks = conn.execute(
            f"SELECT * FROM {ASKS} WHERE line_id = ? ORDER BY asked_at, id",  # noqa: S608
            (int(line["id"]),),
        ).fetchall()
        out.append({
            "id": int(line["id"]),
            "status": str(line["status"]),
            "asks": [
                {"person_id": int(a["person_id"]), "session_id": str(a["session_id"]),
                 "question": str(a["question"]), "place": str(a["place"] or "chat"),
                 "asked_at": str(a["asked_at"])}
                for a in asks
            ],
        })
    return out


def list_open(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """The open lines, each with its asks, oldest ask first. Who may see
    which ask is the caller's to decide (``api.routes.knowledge_questions``)."""
    conn = _connect(db_path)
    try:
        return _lines(conn, "status = 'open'", ())
    finally:
        conn.close()


def get(line_id: int, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        found = _lines(conn, "id = ?", (int(line_id),))
    finally:
        conn.close()
    return found[0] if found else None


def close(line_id: int, how: str, *, db_path: Path | None = None) -> bool:
    """Take an open line off the list: ``document`` (a document was added for
    it), ``taught`` (the owner told the answer) or ``dismissed`` (it won't come
    back for anyone who had asked it). False when it isn't open."""
    if how not in ("document", "taught", "dismissed"):
        raise ValueError(f"unknown way to close: {how}")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT id FROM {TABLE} WHERE id = ? AND status = 'open'", (int(line_id),)  # noqa: S608
        ).fetchone()
        if row is None:
            return False
        askers = sorted({
            int(r[0]) for r in conn.execute(
                f"SELECT person_id FROM {ASKS} WHERE line_id = ?", (int(line_id),)  # noqa: S608
            )
        }) if how == "dismissed" else []
        conn.execute(
            f"UPDATE {TABLE} SET status = ?, closed_at = ?, closed_how = ?, dismissed_for = ? "  # noqa: S608
            "WHERE id = ?",
            ("dismissed" if how == "dismissed" else "resolved", _now().isoformat(), how,
             json.dumps(askers), int(line_id)),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def forget_session(session_id: str, *, db_path: Path | None = None) -> None:
    """Drop one chat's asks (its chat was deleted): their wording, who, where
    and when. A line with no asks left goes. A DB without the tables is a
    no-op."""
    from openexecutive.memory.episodic import _resolve_db_path

    if not _resolve_db_path(db_path).exists():
        return
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return
    try:
        conn.execute(f"DELETE FROM {ASKS} WHERE session_id = ?", (str(session_id),))  # noqa: S608
        conn.execute(f"DELETE FROM {TABLE} WHERE id NOT IN (SELECT line_id FROM {ASKS})")  # noqa: S608
        conn.commit()
    except sqlite3.Error:
        logger.warning("knowledge_gaps: couldn't forget a chat", exc_info=True)
    finally:
        conn.close()
