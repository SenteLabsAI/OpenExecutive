"""Offer to do a repeated ask on a schedule.

When a person asks for the same thing on a few different days ("send me the
pipeline summary" three Mondays running), offer to do it for them on a
schedule. The offer shows under the chat reply to the repeated ask (a
``repeat_offer`` stream event) and at the top of Workflows, with Set it up
and Not now. Set it up opens the build-by-talking flow pre-filled with the
ask and the timing; nothing is created or scheduled until the person
approves the steps there, as for any workflow they build.

**What is read.** Each turn by a verified speaker (``chat_review``'s rule:
the principal on a surface that confirms it is them, or a rostered teammate
on their own signed-in surface): only what they typed (``own_words``: no
quoted backstory an adapter added, nothing from a turn with an attachment,
and nothing from a turn whose words were composed for them, such as a card's
Approve line), never the reply. Not a turn that touched their own mailbox, a
host page's turn, or a replay. Deleting a chat deletes the asks it kept and
any open offer it made (``forget_session``).

**How a repeat is found.** No model call: the ask's content words (stop
words dropped, plurals folded) are matched against the same person's asks
of the last ``KEEP_DAYS`` days. ``MIN_DAYS`` different days with a match
make a repeat. The timing comes from when the asks were made, in the
workspace's zone: the same weekday each time is weekly on that day; all
inside one week is daily; otherwise weekly on the most common day. The hour
is the earliest one they asked at, so it is ready before they would ask.

**Per person.** An offer is made to the person who asked and listed only
for them. Not now holds the same offer back for ``SNOOZE_DAYS``; Set it up
retires it. Approvals stay the one gate for what the workflow then does.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ASKS_TABLE = "repeat_asks"
OFFERS_TABLE = "repeat_ask_offers"
KEEP_DAYS = 42
MIN_DAYS = 3
SNOOZE_DAYS = 60
MAX_ASKS_PER_PERSON = 400
ASK_MAX = 300
MIN_WORDS = 2
MATCH = 0.6

# A turn that already set something up to run later or ran a workflow is not
# one to offer a schedule for.
_SCHEDULING_TOOLS = frozenset({
    "draft_workflow", "save_workflow", "run_workflow", "suggest_workflow", "schedule_followup",
})

_STOP_WORDS = """
a about above after again all also am an and any are as at be been before being below
between both but by can could did do does doing done down during each few for from further
get got had has have having he her here hers him his how i if in into is it its itself just
let lets me more most my myself no nor not now of off on once only or other our ours out over
own please same she should so some such than thanks thank that the their theirs them then
there these they this those through to too under until up us very was we were what when where
which while who whom why will with would you your yours yourself again quick quickly
send give show tell make pull run find check share draft write put get need want can could
today tonight tomorrow yesterday week weekly month monthly day daily morning afternoon evening
monday tuesday wednesday thursday friday saturday sunday latest new current last next this
hey hi hello ok okay
"""
_STOP = frozenset(_STOP_WORDS.split())


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def _connect(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ASKS_TABLE} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  person_id INTEGER NOT NULL,"
        "  session_id TEXT NOT NULL DEFAULT '',"
        "  words TEXT NOT NULL,"
        "  ask TEXT NOT NULL,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ASKS_TABLE}_person ON {ASKS_TABLE} (person_id, created_at)")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {OFFERS_TABLE} ("  # noqa: S608 — constant table name
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  person_id INTEGER NOT NULL,"
        "  session_id TEXT NOT NULL DEFAULT '',"
        "  words TEXT NOT NULL,"
        "  ask TEXT NOT NULL,"
        "  cadence TEXT NOT NULL,"
        "  asked_at TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'open',"
        "  hold_until TEXT,"
        "  created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{OFFERS_TABLE}_person ON {OFFERS_TABLE} (person_id, status)")
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    try:
        from openexecutive.memory.workspace_settings import get_user_timezone

        return get_user_timezone()
    except Exception:
        return ZoneInfo("UTC")


def key_words(text: str) -> frozenset[str]:
    """The content words of an ask, lowercased, plurals folded."""
    out: set[str] = set()
    for word in re.findall(r"[a-z0-9][a-z0-9'&-]*", (text or "").lower()):
        word = word.strip("'-")
        if len(word) < 3 or word in _STOP or word.isdigit():
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return frozenset(out)


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Two asks for the same thing: most of their content words shared."""
    if len(a) < MIN_WORDS or len(b) < MIN_WORDS:
        return False
    return len(a & b) / len(a | b) >= MATCH


_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def timing(asked: list[datetime], zone: ZoneInfo) -> str:
    """The cadence (UTC, the scheduler's DSL) for asks made at ``asked``:
    weekly on their weekday, daily when they fall inside one week, else
    weekly on the most common day; at the earliest hour they were made."""
    local = sorted(a.astimezone(zone) for a in asked)
    hour = min(d.hour for d in local)
    days = Counter(d.weekday() for d in local)
    weekly = True
    if len(days) == 1:
        weekday = local[0].weekday()
    elif (local[-1].date() - local[0].date()).days < 7:
        weekly = False
        weekday = local[-1].weekday()
    else:
        weekday = days.most_common(1)[0][0]
    # The next such local time, read back in UTC: the scheduler runs in UTC.
    last = local[-1]
    ahead = (weekday - last.weekday()) % 7 or 7
    day = last.date() + timedelta(days=ahead)
    at = datetime(day.year, day.month, day.day, hour, 0, tzinfo=zone).astimezone(UTC)
    if weekly:
        return f"weekly@{_DOW[at.weekday()]}@{at:%H:%M}"
    return f"daily@{at:%H:%M}"


def _verified_person(session: Any, person_id: int | None) -> Any:
    from openexecutive.memory.chat_review import _verified_person as verified

    return verified(session, person_id)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ask": row["ask"],
        "cadence": row["cadence"],
        "asked_at": json.loads(row["asked_at"]),
    }


def _held(conn: sqlite3.Connection, person_id: int, words: frozenset[str], now: datetime) -> bool:
    """An offer for the same thing is open, was set up, or was put off."""
    for row in conn.execute(
        f"SELECT words, status, hold_until FROM {OFFERS_TABLE} WHERE person_id = ?",  # noqa: S608
        (person_id,),
    ):
        if not similar(words, frozenset(json.loads(row["words"]))):
            continue
        if row["status"] == "open":
            return True
        if row["hold_until"] and row["hold_until"] > now.isoformat():
            return True
    return False


def note_ask(
    text: str,
    *,
    session: Any,
    person_id: int | None,
    calls: list[tuple[str, str]] | None = None,
    now: datetime | None = None,
    zone: ZoneInfo | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Keep this ask, and return a new offer when it is one the person has
    made on ``MIN_DAYS`` different days. None otherwise. Never raises."""
    try:
        from openexecutive.delegation.settings import own_words

        typed = own_words(text or "")
        if typed is None:
            return None
        words = key_words(typed)
        if len(words) < MIN_WORDS:
            return None
        person = _verified_person(session, person_id)
        if person is None or person.id is None:
            return None
        pid = int(person.id)
        current = now or _now()
        tz = zone or _zone()
        ask = " ".join(typed.split())[:ASK_MAX]
        session_id = str(getattr(session, "session_id", "") or "")
        conn = _connect(db_path)
        try:
            conn.execute(
                f"DELETE FROM {ASKS_TABLE} WHERE created_at < ?",  # noqa: S608
                ((current - timedelta(days=KEEP_DAYS)).isoformat(),),
            )
            # An answered offer is kept only while it holds the same offer
            # back. Set it up holds as long as Not now: opening the wizard
            # creates nothing, so asking on regardless is offered again.
            conn.execute(
                f"DELETE FROM {OFFERS_TABLE} WHERE status != 'open' AND hold_until < ?",  # noqa: S608
                (current.isoformat(),),
            )
            earlier = [
                (datetime.fromisoformat(r["created_at"]), frozenset(json.loads(r["words"])))
                for r in conn.execute(
                    f"SELECT words, created_at FROM {ASKS_TABLE} WHERE person_id = ? "  # noqa: S608
                    "ORDER BY created_at DESC LIMIT ?",
                    (pid, MAX_ASKS_PER_PERSON),
                )
            ]
            conn.execute(
                f"INSERT INTO {ASKS_TABLE} (person_id, session_id, words, ask, created_at) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?)",
                (pid, session_id, json.dumps(sorted(words)), ask, current.isoformat()),
            )
            conn.execute(
                f"DELETE FROM {ASKS_TABLE} WHERE person_id = ? AND id NOT IN ("  # noqa: S608
                f"SELECT id FROM {ASKS_TABLE} WHERE person_id = ? ORDER BY created_at DESC LIMIT ?)",
                (pid, pid, MAX_ASKS_PER_PERSON),
            )
            conn.commit()
            if any(name in _SCHEDULING_TOOLS for name, _ in calls or []):
                return None
            # One ask a day counts: the latest on each earlier day, then today's.
            today = current.astimezone(tz).date()
            by_day: dict[Any, datetime] = {}
            for at, other in earlier:
                day = at.astimezone(tz).date()
                if day != today and day not in by_day and similar(words, other):
                    by_day[day] = at
            if len(by_day) + 1 < MIN_DAYS:
                return None
            # Check and insert under one write lock, so the same ask sent
            # from two apps at once makes one offer, not two.
            conn.execute("BEGIN IMMEDIATE")
            if _held(conn, pid, words, current):
                conn.rollback()
                return None
            asked = sorted([*by_day.values(), current])
            cadence = timing(asked, tz)
            cur = conn.execute(
                f"INSERT INTO {OFFERS_TABLE} (person_id, session_id, words, ask, cadence, asked_at, "  # noqa: S608
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                (pid, session_id, json.dumps(sorted(words)), ask, cadence,
                 json.dumps([a.isoformat() for a in asked]), current.isoformat()),
            )
            conn.commit()
            row = conn.execute(
                f"SELECT * FROM {OFFERS_TABLE} WHERE id = ?", (cur.lastrowid,)  # noqa: S608
            ).fetchone()
            return _row(row)
        finally:
            conn.close()
    except Exception:
        logger.warning("repeat_asks: couldn't note the ask", exc_info=True)
        return None


def open_offers(person_id: int, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    """This person's open offers, newest first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM {OFFERS_TABLE} WHERE person_id = ? AND status = 'open' "  # noqa: S608
            "ORDER BY created_at DESC, id DESC",
            (person_id,),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def answer_offer(
    offer_id: int,
    person_id: int,
    *,
    accept: bool,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> bool:
    """Set it up (``accept``) or Not now, on this person's own open offer.
    False when there is no such offer."""
    current = now or _now()
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE {OFFERS_TABLE} SET status = ?, hold_until = ? "  # noqa: S608
            "WHERE id = ? AND person_id = ? AND status = 'open'",
            ("accepted" if accept else "dismissed",
             (current + timedelta(days=SNOOZE_DAYS)).isoformat(), offer_id, person_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def forget_session(session_id: str, *, db_path: Path | None = None) -> None:
    """A deleted chat takes its asks, and any open offer it made, with it.
    Never raises."""
    if not session_id:
        return
    try:
        conn = _connect(db_path)
        try:
            conn.execute(f"DELETE FROM {ASKS_TABLE} WHERE session_id = ?", (session_id,))  # noqa: S608
            conn.execute(
                f"DELETE FROM {OFFERS_TABLE} WHERE session_id = ? AND status = 'open'",  # noqa: S608
                (session_id,),
            )
            # An answered one stays only to hold the same offer back, which
            # needs its words, not the sentence they typed.
            conn.execute(
                f"UPDATE {OFFERS_TABLE} SET ask = '' WHERE session_id = ?",  # noqa: S608
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("repeat_asks: couldn't forget the chat", exc_info=True)
