"""After pushback, a stronger model for one answer.

When someone pushes back on the Executive's last answer, the next answer runs
on the pushback model (``PUSHBACK_MODEL``, or the Executive's Council pick),
and the one after that goes back to the normal model. Pushback is any of:

- ``thumbs_down``: a 👎 on the last reply, or a typed "thumbs down" / 👎 as
  the message (chat apps have no reaction on the reply),
- ``said_wrong``: words that say the last answer missed ("that's not what I
  asked", "why can't you", "try again"),
- ``asked_again``: the same ask as the last message, word for word or nearly,
- ``claimed``: the claim check caught the last reply claiming a change no
  tool made (``orchestrator.claim_check``).

All of it is word matching and reads of rows the chat already wrote, so it
adds no model call. Never raises: anything it can't read counts as no
pushback, and the turn runs on its normal model.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Stored as the Executive's pushback pick to turn it off even when
# PUSHBACK_MODEL names a model. None (no pick) follows PUSHBACK_MODEL.
PUSHBACK_OFF = "off"

REASONS: tuple[str, ...] = ("thumbs_down", "said_wrong", "asked_again", "claimed")

# Phrases, not single words: "wrong" alone also opens "what went wrong with
# the deal?", which is a question, not pushback.
SAID_WRONG_RE = re.compile(
    r"\b(?:"
    r"(?:that'?s|that is|this is|it'?s|you'?re) (?:not|still not) (?:what i|right|correct|it)|"
    r"not what i (?:asked|wanted|meant|said)|"
    r"(?:that'?s|that is|this is|it'?s|you'?re|still) (?:wrong|incorrect)|"
    r"why (?:can'?t|cannot|couldn'?t|didn'?t|won'?t|wouldn'?t|aren'?t|haven'?t) you|"
    r"you (?:should be able|still (?:haven'?t|didn'?t|can'?t)|didn'?t (?:do|answer|read|listen)|"
    r"missed|got (?:it|that) wrong|keep (?:saying|telling|asking))|"
    r"try (?:it )?again|do it again|"
    r"(?:doesn'?t|didn'?t|does not|did not) work|"
    r"(?:useless|makes? no sense|not helpful|unhelpful)"
    r")\b",
    re.IGNORECASE,
)

# The same ask twice: long enough that "go" / "yes" twice isn't one, and
# nearly the same words.
_ASKED_AGAIN_MIN_CHARS = 16
_ASKED_AGAIN_RATIO = 0.85

_VOICE_PREFIX = "[Voice note, transcribed]"
_THUMB_RE = re.compile(r"^(?:thumbs?[ -]?down|👎(?:[\U0001F3FB-\U0001F3FF])?)$", re.IGNORECASE)


@dataclass(frozen=True)
class Pushback:
    model: str
    reason: str


def _clean(text: str) -> str:
    text = text.strip()
    if text.startswith(_VOICE_PREFIX):
        text = text[len(_VOICE_PREFIX):]
    return " ".join(text.split())


def _typed_thumbs_down(text: str) -> bool:
    return _THUMB_RE.match(text.strip(" \t\n.!")) is not None


def said_wrong(text: str) -> bool:
    return SAID_WRONG_RE.search(text.replace("’", "'")) is not None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def last_user_text(history: list[dict[str, Any]]) -> str:
    """The person's last message in the chat before this one."""
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        text = _clean(_text_of(msg.get("content")))
        # Tool results ride in user turns; skip those to the real message.
        if text:
            return text
    return ""


def asked_again(text: str, previous: str) -> bool:
    a, b = text.lower(), previous.lower()
    if len(a) < _ASKED_AGAIN_MIN_CHARS or len(b) < _ASKED_AGAIN_MIN_CHARS:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= _ASKED_AGAIN_RATIO


def _when(text: Any) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _last_exchange(session_id: str, db_path: Path | None) -> tuple[Any, Any]:
    """The chat's last saved reply and the message it answered, as rows
    (either None when the chat has none)."""
    from openexecutive.memory.episodic import _get_conn, _resolve_db_path

    resolved = _resolve_db_path(db_path)
    if not resolved.exists():
        return None, None
    with _get_conn(resolved) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)")}
        if "feedback" not in cols:
            return None, None
        reply = conn.execute(
            "SELECT id, created_at, feedback FROM chat_messages "
            "WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if reply is None:
            return None, None
        asked = conn.execute(
            "SELECT id, created_at FROM chat_messages "
            "WHERE session_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
            (session_id, reply["id"]),
        ).fetchone()
    return reply, asked


def _claim_caught(session_id: str, start: datetime | None, end: datetime | None) -> bool:
    """Whether the claim check caught a reply in this chat between the last
    message and its reply."""
    if end is None:
        return False
    from openexecutive.audit.logger import get_audit_logger

    events = get_audit_logger().query(
        event_type="claim_check", session_id=session_id, limit=20, all_owners=True
    )
    for event in events:
        ts = _when(getattr(event, "ts", None))
        if ts is not None and ts <= end and (start is None or ts >= start):
            return True
    return False


def reason_for(
    session_id: str | None,
    text: str,
    history: list[dict[str, Any]],
    *,
    db_path: Path | None = None,
) -> str | None:
    """Why this message is pushback on the last answer (one of ``REASONS``),
    or None. Never raises."""
    try:
        text = _clean(text)
        if _typed_thumbs_down(text):
            return "thumbs_down"
        reply, asked = _last_exchange(session_id, db_path) if session_id else (None, None)
        if reply is not None and reply["feedback"] == "down":
            return "thumbs_down"
        if said_wrong(text):
            return "said_wrong"
        if asked_again(text, last_user_text(history)):
            return "asked_again"
        if reply is not None and session_id and _claim_caught(
            session_id,
            _when(asked["created_at"]) if asked is not None else None,
            _when(reply["created_at"]),
        ):
            return "claimed"
    except Exception:
        logger.warning("pushback: couldn't read the last answer", exc_info=True)
    return None


def pushback_model(override_pick: str | None, default: str) -> str | None:
    """The model an answer after pushback runs on, or None when it's off.
    The Executive's Council pick wins over ``PUSHBACK_MODEL``."""
    pick = override_pick if override_pick is not None else default
    pick = (pick or "").strip()
    if not pick or pick == PUSHBACK_OFF:
        return None
    return pick


def for_turn(
    *,
    session_id: str | None,
    text: str,
    history: list[dict[str, Any]],
    normal_model: str,
    override_pick: str | None,
    default: str,
    db_path: Path | None = None,
) -> Pushback | None:
    """The pushback model and why, when this turn should run on it; None
    when it's off, the message isn't pushback, or the pick is the model the
    turn already uses."""
    model = pushback_model(override_pick, default)
    if model is None or model == normal_model:
        return None
    reason = reason_for(session_id, text, history, db_path=db_path)
    if reason is None:
        return None
    return Pushback(model=model, reason=reason)
