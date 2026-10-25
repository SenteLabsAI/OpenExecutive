"""A chat as a Markdown document, for the Download and Copy buttons
(``GET /sessions/{id}/export``).

Each message under who wrote it (the person's name from People, or the
Executive), with the reply's sources listed under it. What the reply said
is copied as written: it is already Markdown.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from openexecutive.memory.session_store import export_rows

EXECUTIVE = "Executive"
_FILE_NAME_MAX = 60


def _names(person_ids: set[int]) -> dict[int, str]:
    from openexecutive.people.store import get_person

    out: dict[int, str] = {}
    for pid in person_ids:
        try:
            person = get_person(pid)
        except Exception:
            person = None
        if person is not None and person.full_name:
            out[pid] = person.full_name
    return out


def _day(stamp: str | None) -> str:
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    return f"{when.day} {when:%B %Y}"


def _sources_block(sources: dict[str, Any] | None) -> list[str]:
    if not sources:
        return []
    lines: list[str] = []
    for item in sources.get("sources") or []:
        title = str(item.get("title") or item.get("url") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            continue
        lines.append(f"- [{title}]({url})" if url.startswith(("http://", "https://")) else f"- {title}")
    if not lines:
        return []
    return ["", "Sources:", *lines]


def to_markdown(meta: dict[str, Any], *, owner_person_id: int | None) -> str:
    """The whole chat ``meta`` describes (a ``get_session_metadata`` row)."""
    rows = export_rows(str(meta["session_id"]))
    people = {
        int(r["sender_person_id"]) for r in rows if r["sender_person_id"] is not None
    }
    if owner_person_id is not None:
        people.add(int(owner_person_id))
    names = _names(people)
    owner_name = names.get(owner_person_id) if owner_person_id is not None else None

    out = [f"# {meta['title']}"]
    started = _day(meta.get("created_at"))
    if started:
        out += ["", f"Started {started}"]
    for row in rows:
        sender = row["sender_person_id"]
        if row["role"] == "assistant":
            who = EXECUTIVE
        else:
            who = (names.get(int(sender)) if sender is not None else None) or owner_name or "You"
            # Sent from another app's page, where a script may have written it.
            if row.get("sender_surface") == "host":
                who = f"{who} (from another app's page)"
        out += ["", f"## {who}", "", str(row["content"]).strip()]
        if row["stopped"]:
            out += ["", "_(Stopped before it finished.)_"]
        if row["role"] == "assistant":
            out += _sources_block(row["sources"])
    return "\n".join(out).rstrip() + "\n"


def file_name(title: str) -> str:
    """A safe download name from the chat's title: letters, digits and
    dashes, ending in ``.md``."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:_FILE_NAME_MAX].strip("-")
    return f"{slug or 'chat'}.md"
