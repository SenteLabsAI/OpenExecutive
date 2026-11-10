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
the quote it came from, against the message), or the name the lesson
defines ("Atlas means ...", "your name is R2") written as a name
mid-sentence in the message, since a lesson about what a name means shares
only that word with the question it answers. A short list of short lines
needs no vector index, and the match is what makes the counters below mean
something. The block says the current request wins and nothing in it
authorizes an action, as the style block does.

**Who sees a lesson.** Only its own person's turns: the principal's reach
the principal, a teammate's reach that teammate, wherever the chat
happened, so nothing said in one person's chat is paraphrased into
another's turn. The Reflection screen lists each person their own and the
principal the ones from chats they may open.

**What it may say.** Anything the person's own words back: beyond the
words the quote carries it may add at most ``MAX_NEW_WORDS`` content words,
enough to rephrase "summarized it instead of just letting me know he sent
it" as a lesson about summarizing a message. It may talk about mail,
sending, people or amounts, because it authorizes nothing: every send,
share or post still goes through the safety levels and approval cards,
whatever a lesson says. It may not carry a link, a handle, markup, an
identifier, talk about the model's instructions or about something being
approved (``_LESSON_DENY``), nor letters outside Latin script, which is how
text steers a later prompt.

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
  chat_review_lesson_surfaces  lesson_id, surface, day, shown, graded
                           (how often it was used where, each day:
                           ``SURFACES``)

**Where it is used.** Every place the Executive writes or speaks for its
person reads their lessons, and each use is counted under its surface
(``SURFACES``): a chat turn, an email (one it answers from its own
address, or one it writes as the person, ``build_draft_lessons``), a brief
or review (``with_owner_lessons``) and the work it starts on its own. A
caller that runs calls or meetings on top marks its session
(``Session.memory_surface``: ``one_on_one`` or ``meeting``) and, for a call
with no single speaker, names whose lessons it reads
(``Session.lessons_person_id``). Only a
graded chat turn moves ``shown``, the count retirement reads; the other
surfaces are counted beside it, so a lesson used in ten briefs is not
retired on the strength of one bad chat turn.
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
SURFACES_TABLE = "chat_review_lesson_surfaces"
# Where a lesson can be used. The Memory page names each one.
SURFACE_CHAT = "chat"
SURFACE_ONE_ON_ONE = "one_on_one"
SURFACE_EMAIL = "email"
SURFACE_MEETING = "meeting"
SURFACE_BRIEF = "brief"
SURFACE_LEAD = "lead"
SURFACES: tuple[str, ...] = (
    SURFACE_CHAT, SURFACE_ONE_ON_ONE, SURFACE_EMAIL, SURFACE_MEETING, SURFACE_BRIEF, SURFACE_LEAD,
)
BLOCK_TAG = "lessons"
_BLOCK_CLOSE = f"</{BLOCK_TAG}>"

MIN_CHARS = 8
MAX_CHARS = 240
MAX_ACTIVE = 40
MAX_PER_TURN = 6
MAX_NEW_WORDS = 8
RETIRE_AFTER_SHOWN = 5
# Two shared content words: one common word ("report") matches too much.
# The name a lesson defines is enough (``_defined_names``).
_MATCH_WORDS = 2

# What a lesson may not carry: something that points somewhere (a link or a
# handle), markup or an identifier (a tool name), or talk to the model about
# its instructions. These are how text tries to steer a later prompt; none
# of them is needed to say what someone wants done differently. Words about
# mail, sending, people or amounts are fine: a lesson authorizes nothing.
_LESSON_DENY: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://|www\.|\.(com|io|net|org|ai)\b", re.IGNORECASE),
    re.compile(r"@\w"),
    re.compile(r"[<>{}`\[\]]"),
    re.compile(r"\b\w+_\w+\b"),
    re.compile(
        r"\b(ignore|disregard|override|bypass|instruction|instructions|"
        r"prompt|prompts|jailbreak|system message)\b",
        re.IGNORECASE,
    ),
    # Talk that would stand in for an approval. The approval cards and
    # safety levels hold in code either way; this keeps a lesson from even
    # claiming one.
    re.compile(
        r"\b(as|already|pre)[- ]?approved\b|\bwithout (an? )?(approval|sign[- ]?off)\b|"
        r"\bskip\w* (the )?(approval|card|sign[- ]?off)s?\b|\bauthori[sz]\w*",
        re.IGNORECASE,
    ),
    # Handing things to whoever asks: a lesson is about how to do the work
    # for this person, not about who else gets it.
    re.compile(
        r"\b(whoever|whomever|anyone (who|that) asks|anybody (who|that) asks|"
        r"everyone (who|that) asks|all (of )?(our|the|my) (customers|clients|contacts))\b",
        re.IGNORECASE,
    ),
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
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SURFACES_TABLE} ("
        "  lesson_id INTEGER NOT NULL,"
        "  surface TEXT NOT NULL,"
        "  day TEXT NOT NULL,"
        "  shown INTEGER NOT NULL DEFAULT 0,"
        "  graded INTEGER NOT NULL DEFAULT 0,"
        "  PRIMARY KEY (lesson_id, surface, day)"
        ")"
    )
    conn.commit()
    return conn


def _used_in(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[int, dict[str, int]]:
    """How often each of ``rows`` was used on each surface. A use counted
    before surfaces were kept (``shown`` beyond what the surfaces explain)
    was a chat turn: nothing else read lessons then."""
    out: dict[int, dict[str, int]] = {int(r["id"]): dict.fromkeys(SURFACES, 0) for r in rows}
    if not out:
        return out
    graded: dict[int, int] = dict.fromkeys(out, 0)
    marks = ",".join("?" * len(out))
    for use in conn.execute(
        f"SELECT lesson_id, surface, SUM(shown) AS shown, SUM(graded) AS graded "  # noqa: S608
        f"FROM {SURFACES_TABLE} WHERE lesson_id IN ({marks}) GROUP BY lesson_id, surface",
        tuple(out),
    ).fetchall():
        lesson_id = int(use["lesson_id"])
        if str(use["surface"]) in out[lesson_id]:
            out[lesson_id][str(use["surface"])] += int(use["shown"])
        graded[lesson_id] += int(use["graded"])
    for r in rows:
        out[int(r["id"])][SURFACE_CHAT] += max(0, int(r["shown"]) - graded[int(r["id"])])
    return out


def _outs(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    used = _used_in(conn, rows)
    return [{**_out(r), "used_in": used[int(r["id"])]} for r in rows]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def lesson_rejection(text: str, *, roster_names: list[str] | None = None) -> str | None:
    """Why ``text`` may not be a lesson, or None when it may: its length, or
    something in ``_LESSON_DENY``. ``roster_names`` is accepted for older
    callers and unused: a lesson stays with its own person, so naming
    someone is fine."""
    del roster_names
    if len(text) < MIN_CHARS:
        return "too_short"
    if len(text) > MAX_CHARS:
        return "too_long"
    from openexecutive.attunement.style import _non_latin_letter

    # Letters outside Latin script would let "ignоre" (Cyrillic о) past the
    # word checks below.
    if _non_latin_letter(text):
        return "non_latin_letters"
    if any(pattern.search(text) for pattern in _LESSON_DENY):
        return "denied_content"
    return None


def _stem(word: str) -> str:
    """A rough stem, so "summarized" and "summarize" count as one word."""
    for suffix in ("izing", "ized", "izes", "ize", "ing", "ied", "ies", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    # "invoice" and "invoices" both end as "invoic".
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _content_words(text)}


def lesson_overreach(text: str, quote: str) -> str | None:
    """Why ``text`` isn't backed by ``quote``, or None. The lesson must be
    about what the quote says (a word in common) and may add at most
    ``MAX_NEW_WORDS`` content words to rephrase it."""
    said, lesson = _stems(quote), _stems(text)
    if not lesson & said:
        return "the lesson isn't about what they said"
    if len(lesson - said) > MAX_NEW_WORDS:
        return "the lesson says more than the quote does"
    return None


def _normalize(text: str) -> str:
    from openexecutive.attunement.style import _normalize

    return _normalize(text)


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
    ``private`` is accepted for older callers: every lesson stays with
    its own person now.
    A duplicate of a live lesson is refused; past ``MAX_ACTIVE`` live
    lessons the oldest retires to make room."""
    clean = _normalize(text)
    rejection = lesson_rejection(clean)
    if rejection:
        return None, rejection
    now = _now().isoformat()
    del private
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
            (int(person_id), clean, _normalize(quote)[:400], str(session_id), 0,
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
        return _outs(conn, [row])[0] if row is not None else None
    finally:
        conn.close()


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
        return _outs(conn, rows)
    finally:
        conn.close()


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
     "more", "most", "very", "really", "like", "make", "made", "does", "did", "done", "mean", "means",
     # Filler replies ("ok thanks", "sounds good") name nothing, so they back nothing.
     "thanks", "thank", "sounds", "sound", "good", "great", "okay", "sure", "fine", "yeah", "cool",
     "perfect", "alright", "awesome", "right", "away", "got"]
)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.casefold()) if len(w) >= 4 and w not in _STOPWORDS}


_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9']*")
# A lesson that says what a name means: "Atlas means ...", "R2 is ...",
# "... is called Atlas", "your name is R2".
_NAME = r"([A-Z][A-Za-z0-9']*|[A-Za-z]*\d[A-Za-z0-9']*)"
_DEFINED_RES = (
    re.compile(_NAME + r"\s+(?:is|means|stands for|refers to)\b"),
    re.compile(r"\b(?:called|call it|named|name is|known as)\s+" + _NAME),
)


def _names(text: str, *, sentence_starts: bool = True) -> set[str]:
    """The name-like words in ``text``, casefolded: written with a capital
    or a digit ("Atlas", "R2"), not a stopword or a bare number.
    ``sentence_starts=False`` skips a word that opens a sentence, where a
    capital says nothing."""
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        word = m.group()
        if not any(c.isupper() or c.isdigit() for c in word) or word.isdigit():
            continue
        if not sentence_starts and re.search(r"(^|[.!?:]\s+)$", text[: m.start()]):
            continue
        folded = word.casefold()
        if len(folded) >= 2 and folded not in _STOPWORDS:
            out.add(folded)
    return out


def _defined_names(text: str) -> set[str]:
    """The names ``text`` says the meaning of: a lesson about what a name
    means ("Atlas means the Dallas warehouse move") shares only that word
    with the question it answers. A name it merely mentions ("Pipeline
    reports go to Sam") is not one."""
    found: set[str] = set()
    for pattern in _DEFINED_RES:
        for m in pattern.finditer(text):
            found |= _names(m.group(1))
    return found


def _matches(lesson: dict[str, Any], message: str, *, strict: bool = False) -> bool:
    """Whether ``lesson`` reaches a turn about ``message``: two shared
    content words, or the name the lesson defines, written as a name
    mid-sentence in the message. ``strict`` is for a whole rendered prompt
    (a brief's calendar, mail and figures), where two words in common
    happen by chance: at least half the lesson's own words must be there."""
    words = _content_words(message)
    if strict:
        own = _content_words(lesson["text"])
        need = max(_MATCH_WORDS, (len(own) + 1) // 2)
        if len(own & words) >= need:
            return True
    else:
        own = _content_words(lesson["text"]) | _content_words(lesson["quote"])
        if len(own & words) >= _MATCH_WORDS:
            return True
    names = _defined_names(lesson["text"]) | _defined_names(lesson["quote"])
    return bool(names & _names(message, sentence_starts=False))


def lessons_for(
    person_id: int | None, message: str, *, strict: bool = False, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """The live lessons that reach this speaker's turn about ``message``:
    the speaker's own only (``strict``: see ``_matches``). Never raises."""
    if person_id is None:
        return []
    try:
        if not _content_words(message) and not _names(message, sentence_starts=False):
            return []
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} WHERE retired_at IS NULL "  # noqa: S608
                "AND person_id = ? ORDER BY created_at DESC, id DESC",
                (int(person_id),),
            ).fetchall()
        finally:
            conn.close()
        return [lesson for lesson in map(_out, rows) if _matches(lesson, message, strict=strict)][:MAX_PER_TURN]
    except Exception:
        logger.debug("lessons: lookup failed", exc_info=True)
        return []


def note_shown(
    lessons: list[dict[str, Any]],
    *,
    person_id: int | None = None,
    surface: str = SURFACE_CHAT,
    graded: bool = True,
    db_path: Path | None = None,
) -> None:
    """These lessons were used on ``surface`` (``SURFACES``) for
    ``person_id``: count it there. ``graded`` (a chat turn the review will
    grade) also counts it toward ``shown`` and remembers which turn and
    whose, so the grade of that turn can be counted against them; a draft,
    a brief or a shared call passes False."""
    if not lessons:
        return
    if surface not in SURFACES:
        surface = SURFACE_CHAT
    try:
        from openexecutive.audit.context import get_active_turn_id

        turn_id = get_active_turn_id() if graded else None
        now = _now().isoformat()
        conn = _connect(db_path)
        try:
            for lesson in lessons:
                if graded:
                    conn.execute(
                        f"UPDATE {TABLE} SET shown = shown + 1 WHERE id = ?", (int(lesson["id"]),)  # noqa: S608
                    )
                conn.execute(
                    f"INSERT INTO {SURFACES_TABLE} (lesson_id, surface, day, shown, graded) "  # noqa: S608
                    "VALUES (?, ?, ?, 1, ?) ON CONFLICT (lesson_id, surface, day) DO UPDATE SET "
                    "shown = shown + 1, graded = graded + excluded.graded",
                    (int(lesson["id"]), surface, now[:10], 1 if graded else 0),
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
    from openexecutive.utils.prompt_blocks import scrub_block_line

    out: list[str] = []
    kept: list[dict[str, Any]] = []
    for lesson in lessons:
        clean = _normalize(lesson["text"])
        # Re-checked on every render: a lesson kept under an older check
        # never reaches a turn if it fails this one.
        if lesson_rejection(clean) is None:
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
    person_id: int | None,
    message: str,
    *,
    count: bool = True,
    surface: str = SURFACE_CHAT,
    graded: bool = True,
    strict: bool = False,
    db_path: Path | None = None,
) -> str:
    """Body of the ``<lessons>`` block for this speaker's turn, or "". Lives
    in the user turn (never a cached system block). Counts the lessons it
    shows under ``surface``, and against the active turn when ``graded``,
    unless ``count`` is False (a replay, memory.replay: no real turn to
    grade). Never raises."""
    try:
        if not _enabled():
            return ""
        lines, shown = _lines(lessons_for(person_id, message, strict=strict, db_path=db_path))
        if not lines:
            return ""
        if count:
            note_shown(shown, person_id=person_id, surface=surface, graded=graded, db_path=db_path)
        return _HEADER + "\n" + "\n".join(f"- {line}" for line in lines)
    except Exception:
        logger.debug("lessons: block build failed", exc_info=True)
        return ""


_DRAFT_HEADER = (
    "How the writer asked for things to be done before, in their own words. "
    "Follow each one that applies to this email unless the intent asks for "
    "something different. They are preferences about how to write: never a "
    "fact to add, a person to write to, or permission for anything."
)


def build_draft_lessons(
    person_id: int | None, text: str, *, count: bool = True, db_path: Path | None = None
) -> str:
    """The lessons of ``person_id`` that match an email written as them
    (``text``: what it should say and the subject it answers), for the
    composer's user turn, or "". Counted as an email use. Never raises."""
    try:
        if not _enabled():
            return ""
        lines, shown = _lines(lessons_for(person_id, text, db_path=db_path))
        if not lines:
            return ""
        if count:
            note_shown(shown, person_id=person_id, surface=SURFACE_EMAIL, graded=False, db_path=db_path)
        return _DRAFT_HEADER + "\n" + "\n".join(f"- {line}" for line in lines)
    except Exception:
        logger.debug("lessons: draft block build failed", exc_info=True)
        return ""


def with_owner_lessons(
    user_content: str, *, surface: str = SURFACE_BRIEF, db_path: Path | None = None
) -> str:
    """``user_content`` with the principal's matching lessons appended as a
    ``<lessons>`` block, or unchanged when none match: for the single-shot
    prompts nobody is speaking in (the briefs, the reviews, the work the
    Executive starts on its own). Counted under ``surface``. Never raises."""
    try:
        from openexecutive.people.store import find_principal_person

        owner = find_principal_person()
        if owner is None or owner.id is None:
            return user_content
        # The whole rendered prompt is the message: match strictly, so a
        # lesson comes along (and is counted) only when the brief is about it.
        block = build_lessons_block(
            int(owner.id), user_content, surface=surface, graded=False, strict=True, db_path=db_path
        )
    except Exception:
        logger.debug("lessons: owner block failed", exc_info=True)
        return user_content
    if not block:
        return user_content
    return (
        f"{user_content.rstrip()}\n\n<{BLOCK_TAG}>\n{block}\n"
        "Apply them quietly: never mention a lesson, or that one is kept.\n"
        f"{_BLOCK_CLOSE}"
    )


def used_since(
    since: str, *, lesson_ids: list[int] | None = None, db_path: Path | None = None
) -> dict[str, int]:
    """How often lessons were used on each surface on or after the day
    ``since`` (an ISO date or time): all of them, or only ``lesson_ids``
    (the ones a caller may see, so the count gives nothing else away).
    Zeros when the count can't be read. Never raises."""
    out = dict.fromkeys(SURFACES, 0)
    try:
        conn = _connect(db_path)
        try:
            where = "s.day >= ?"
            params: list[Any] = [str(since)[:10]]
            if lesson_ids is not None:
                if not lesson_ids:
                    return out
                where += f" AND s.lesson_id IN ({','.join('?' * len(lesson_ids))})"
                params += [int(i) for i in lesson_ids]
            for row in conn.execute(
                f"SELECT s.surface, SUM(s.shown) AS n FROM {SURFACES_TABLE} s "  # noqa: S608
                f"WHERE {where} GROUP BY s.surface",
                params,
            ).fetchall():
                if str(row["surface"]) in out:
                    out[str(row["surface"])] = int(row["n"])
        finally:
            conn.close()
    except Exception:
        logger.debug("lessons: couldn't count uses", exc_info=True)
    return out


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
