"""Always in the loop in Act as me drafts: what the writer already told the
people a draft goes to, so the ghostwriter (``delegation.ghostwriter``) can
keep the draft consistent with it.

Only the writer's own notes from replies they sent through Act as me
(``history.SOURCE_APPROVED_REPLY``), and only a note whose recipients include
**every** recipient of the new draft: each of them already heard it, so
nothing told to one person can reach another through a draft. Chat notes name
no recipient, so they never qualify. Only with the writer's switch on.

The ghostwriter uses them to stay consistent, never as content: a conflict
with the draft becomes an open question on the draft's card, which only the
writer sees. Never raises; logs codes, never text.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

logger = logging.getLogger(__name__)

MAX_NOTES = 8
_ADDRESS = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def _addresses(counterpart: str) -> set[str]:
    return {a.lower() for a in _ADDRESS.findall(counterpart or "")}


def notes_for_draft(person_id: int | None, recipients: Iterable[str]) -> str | None:
    """The writer's notes from mail every one of ``recipients`` was on,
    newest first, as plain lines for the ghostwriter's ``<writer_noted>``
    block; None when there are none or the writer's switch is off."""
    try:
        from openexecutive.delegation.ghostwriter import one_line
        from openexecutive.memory import history
        from openexecutive.utils.prompt_blocks import plain

        # Bare addresses, so "Dana <dana@x>" and "dana@x" match alike.
        wanted = set().union(*(_addresses(str(r)) for r in recipients)) if recipients else set()
        if person_id is None or not wanted or not history.person_settings(person_id).reply_notes:
            return None
        lines: list[str] = []
        for note in history.list_notes(person_id, limit=history.MAX_LIST):
            if note.source != history.SOURCE_APPROVED_REPLY or not wanted <= _addresses(note.counterpart):
                continue
            words = note.correction or note.quote
            line = f"[{note.occurred_at[:10]}] {note.kind}: \"{one_line(plain(words), 300)}\""
            if note.due_date:
                line += f" (due {note.due_date})"
            lines.append(line)
            if len(lines) >= MAX_NOTES:
                break
        return "\n".join(lines) or None
    except Exception:
        logger.warning("history: couldn't read notes for a draft", exc_info=True)
        return None


__all__ = ["notes_for_draft"]
