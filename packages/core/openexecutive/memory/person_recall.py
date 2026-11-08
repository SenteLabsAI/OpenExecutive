"""What the Executive remembers about one person, from its own stores.

``ask_about_person`` answers from here when the peer-memory service has
nothing (it is off, or knows nothing yet). Two answers, by who is asking:

* :func:`own_memory`: for the person themselves, their own private notes
  ("Keep track of what happens", ``memory.history``) that match the
  question, newest first, and how they like replies written (their style
  rules, ``attunement.style``).
* :func:`work_style`: for a teammate, when the person shares their work
  style: their role, their departments and how they like replies written.
  Never their notes.

Both are plain text for the model, capped, and empty when nothing is known.
Neither raises.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_NOTES = 8
MAX_CHARS = 2_000

# Words too common to say what a question is about.
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "about", "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if", "in", "is", "it", "its", "me", "my", "of", "on", "or", "our", "she", "so", "that", "the", "their", "them", "they", "this", "to", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with", "you", "your", "know", "tell", "remember", "said", "say", "anything"]
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _note_line(note: Any) -> str:
    day = str(getattr(note, "occurred_at", "") or "")[:10]
    summary = str(getattr(note, "correction", None) or getattr(note, "summary", "") or "").strip()
    due = getattr(note, "due_date", None)
    who = str(getattr(note, "counterpart", "") or "").strip()
    bits = [f"{day}: {summary}" if day else summary]
    if who:
        bits.append(f"(with {who})")
    if due:
        bits.append(f"(due {due})")
    return "- " + " ".join(bits)


def _matching_notes(person_id: int, question: str) -> list[Any]:
    """The person's live notes that share a word with the question, best
    match first; the newest ones when the question names nothing specific."""
    from openexecutive.memory import history

    notes = history.list_notes(person_id, limit=history.MAX_LIST)
    if not notes:
        return []
    wanted = _words(question)
    if not wanted:
        return notes[:MAX_NOTES]
    scored: list[tuple[int, int, Any]] = []
    for idx, note in enumerate(notes):
        text = " ".join(
            str(getattr(note, f, "") or "")
            for f in ("summary", "counterpart", "subject", "quote", "correction")
        )
        hits = len(wanted & _words(text))
        if hits:
            scored.append((hits, -idx, note))
    if not scored:
        return []
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [n for _, _, n in scored[:MAX_NOTES]]


def _style_lines(person_id: int) -> list[str]:
    try:
        from openexecutive.attunement.style import get_profile

        return [f"- {r.text}" for r in get_profile(person_id).rules if str(r.text).strip()]
    except Exception:
        logger.warning("person_recall: couldn't read style rules", exc_info=True)
        return []


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"


def own_memory(person_id: int, question: str) -> str:
    """What is remembered for ``person_id`` themselves. Empty when nothing."""
    try:
        parts: list[str] = []
        notes = _matching_notes(person_id, question)
        if notes:
            parts.append("Your notes (private to you):\n" + "\n".join(_note_line(n) for n in notes))
        style = _style_lines(person_id)
        if style:
            parts.append("How you like replies:\n" + "\n".join(style))
        return _cap("\n\n".join(parts))
    except Exception:
        logger.warning("person_recall: own memory failed", exc_info=True)
        return ""


def work_style(person_id: int) -> str:
    """How ``person_id`` works, for a teammate: role, departments and how
    they like replies. Never their notes. Empty when nothing."""
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
        if person is None or getattr(person, "kind", None) != "team" or getattr(person, "archived", False):
            return ""
        parts: list[str] = []
        role = str(getattr(person, "role", "") or "").strip()
        if role:
            parts.append(f"Role: {role}")
        depts = [str(s) for s in (getattr(person, "department_slugs", None) or []) if str(s).strip()]
        if depts:
            parts.append("Departments: " + ", ".join(depts))
        style = _style_lines(person_id)
        if style:
            parts.append("How they like replies:\n" + "\n".join(style))
        return _cap("\n".join(parts))
    except Exception:
        logger.warning("person_recall: work style failed", exc_info=True)
        return ""
