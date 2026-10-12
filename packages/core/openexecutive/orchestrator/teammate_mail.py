"""A teammate's mail with the Executive is theirs: what the chat reads of the
Executive's mailbox leaves it out for everyone else.

Teammates email the Executive the way they would message it in chat, and the
Executive's mailbox keeps every one of those threads. The gateway's mail
reads (Gmail through Google Workspace, Outlook through Microsoft 365) return
whatever the mailbox holds, so without this any turn that may call them (the
principal's, an admin's) could search "from:" a teammate and read what they
sent. Here a model's mailbox read keeps only mail the person asking is part
of, or that no other teammate is:

- A message or thread is another teammate's when an address of a rostered
  teammate other than the reader appears anywhere in it, and none of the
  reader's own addresses is in its From / To / Cc headers. An address of the
  teammate in a body (a quoted reply, a signature) counts, so a doubt hides
  rather than shows.
- A Gmail search listing drops those entries and says how many it dropped. A
  single message or thread is withheld whole, and so is a batch read holding
  one (its messages can't be split safely), with a note to read the others
  one at a time. An Outlook ``value`` list drops those items.
- Mail from outside people (vendors, counsel, the principal's contacts) is
  untouched, and so is the reader's own.

Only reads the model makes in a turn someone is in (``guard_call_tool`` wraps
the chat's ``call_tool`` and a script's calls). The email poller, the brief
and the other internal callers reach the gateway directly and are untouched,
as is an unattended run. Who is reading is the verified asker
(``people_tools._verified_asker_id``); on a turn with none (an inbound email,
an unverified chat) every teammate's mail is left out. Fails closed: when the
roster can't be read the result is withheld.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.people.identity import RosterAllow

logger = logging.getLogger(__name__)

_GW_PREFIX = "google_workspace__"
_M365_PREFIX = "microsoft_365__"

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# The header lines that say who a message is between, in workspace-mcp's
# plain-text output ("From: Ada <ada@example.com>").
_HEADER_RE = re.compile(r"^\s*(?:from|to|cc|reply-to)\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
# Outlook (Graph) fields that name a message's sender and recipients.
_GRAPH_PARTY_KEYS = frozenset({
    "from", "sender", "torecipients", "ccrecipients", "bccrecipients", "replyto",
})
# One entry of a Gmail search listing: "  3. Message ID: ...", up to the next
# entry or the usage notes.
_SEARCH_HEAD_RE = re.compile(r"^📧 MESSAGES:\s*$", re.MULTILINE)
_SEARCH_ENTRY_RE = re.compile(r"^  \d+\. Message", re.MULTILINE)
_SEARCH_TAIL_RE = re.compile(r"^💡 USAGE:", re.MULTILINE)
# Where each message of a batch read starts. A body line that looks like one
# only splits it further, which can hide more, never less.
_MESSAGE_START_RE = re.compile(r"^(?=Message ID:|=== Message \d+ ===)", re.MULTILINE)

WITHHELD_NOTE = (
    "This email involves another teammate and is their own mail with the "
    "Executive, so it is private to them. Say so plainly and suggest asking "
    "them directly; don't guess what it says."
)
BATCH_WITHHELD_NOTE = (
    "At least one of these emails involves another teammate and is their own "
    "mail with the Executive, so it is private to them, and the batch was not "
    "returned. Read the others one at a time; don't guess what the private ones say."
)
LISTING_NOTE = (
    "Left out {count} {noun} involving another teammate: a teammate's own mail "
    "with the Executive is private to them."
)


def is_mail_tool(tool_name: object) -> bool:
    """Whether a gateway tool reads or writes the Executive's mailbox: any
    Google Workspace Gmail tool, any Microsoft 365 mail tool."""
    if not isinstance(tool_name, str):
        return False
    name = tool_name.strip().lower().replace("-", "_")
    if name.startswith(_GW_PREFIX):
        return "gmail" in name
    if name.startswith(_M365_PREFIX):
        return "mail" in name
    return False


def _reader_and_others() -> tuple[RosterAllow, RosterAllow, bool] | None:
    """``(reader's addresses, other teammates' addresses, filter on)`` for
    this turn. Filter off on an unattended run or with no other teammate.
    None when the roster can't be read (the caller withholds)."""
    from openexecutive.orchestrator.people_tools import (
        _verified_asker_id,
        is_principal_on_verified_surface,
    )
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.store import find_principal_person, list_people

    session = current_session.get()
    try:
        if session is None or getattr(session, "unattended", False):
            return RosterAllow([]), RosterAllow([]), False
        reader_id = _verified_asker_id()
        if reader_id is None and is_principal_on_verified_surface(session):
            principal = find_principal_person()
            reader_id = principal.id if principal is not None else None
        team = [p for p in list_people() if p.kind == "team"]
        reader = [p for p in team if reader_id is not None and p.id == reader_id]
        others = [p for p in team if reader_id is None or p.id != reader_id]
        return RosterAllow(reader), RosterAllow(others), bool(others)
    except Exception:
        logger.warning("teammate_mail: roster read failed — withholding mail", exc_info=True)
        return None


def _addresses(text: str) -> list[str]:
    return _ADDRESS_RE.findall(text)


def _is_teammates(unit: str, header_text: str, reader: RosterAllow, others: RosterAllow) -> bool:
    """Whether ``unit`` is another teammate's mail: one of their addresses is
    anywhere in it and none of the reader's is in ``header_text``."""
    if not any(a in others for a in _addresses(unit)):
        return False
    return not any(a in reader for a in _addresses(header_text))


def _text_headers(unit: str) -> str:
    return "\n".join(m.group(1) for m in _HEADER_RE.finditer(unit))


def _graph_parties(item: Any, depth: int = 0) -> str:
    """The sender and recipient fields of a Graph message, as text."""
    if depth > 6 or not isinstance(item, dict):
        return ""
    out = []
    for key, value in item.items():
        if isinstance(key, str) and key.lower() in _GRAPH_PARTY_KEYS:
            out.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(out)


def _filter_listing(text: str, reader: RosterAllow, others: RosterAllow) -> str | None:
    """A Gmail search listing with other teammates' entries dropped, or None
    when ``text`` is not one."""
    head = _SEARCH_HEAD_RE.search(text)
    if head is None:
        return None
    tail = _SEARCH_TAIL_RE.search(text, head.end())
    body_end = tail.start() if tail else len(text)
    body = text[head.end():body_end]
    starts = [m.start() for m in _SEARCH_ENTRY_RE.finditer(body)]
    if not starts:
        return None
    entries = [body[s:e] for s, e in zip(starts, [*starts[1:], len(body)], strict=True)]
    kept = [e for e in entries if not _is_teammates(e, _text_headers(e), reader, others)]
    dropped = len(entries) - len(kept)
    if not dropped:
        return text
    note = LISTING_NOTE.format(count=dropped, noun="message" if dropped == 1 else "messages")
    if not kept:
        return f"No messages to show. {note}"
    rest = text[body_end:]
    return text[:head.end()] + "\n" + body[:starts[0]].lstrip("\n") + "".join(kept) + note + "\n\n" + rest


def _filter_graph(data: Any, reader: RosterAllow, others: RosterAllow) -> str | None:
    """An Outlook result with other teammates' messages dropped, or None
    when nothing in it is."""
    def theirs(item: Any) -> bool:
        return _is_teammates(json.dumps(item, ensure_ascii=False), _graph_parties(item), reader, others)

    if isinstance(data, dict) and isinstance(data.get("value"), list):
        kept = [i for i in data["value"] if not theirs(i)]
        dropped = len(data["value"]) - len(kept)
        if not dropped:
            return None
        out = {**data, "value": kept}
        out["private_note"] = LISTING_NOTE.format(
            count=dropped, noun="message" if dropped == 1 else "messages"
        )
        return json.dumps(out, ensure_ascii=False)
    if theirs(data):
        return json.dumps({"withheld": True, "note": WITHHELD_NOTE})
    return None


def filter_mail_result(tool_name: str, result_text: str) -> str:
    """``result_text`` of a mailbox read less other teammates' mail (see the
    module docstring). Anything but a mail tool's result passes as it is."""
    if not is_mail_tool(tool_name) or not isinstance(result_text, str):
        return result_text
    sides = _reader_and_others()
    if sides is None:
        return json.dumps({"withheld": True, "note": WITHHELD_NOTE})
    reader, others, on = sides
    if not on or not any(a in others for a in _addresses(result_text)):
        return result_text
    try:
        data = json.loads(result_text)
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, (dict, list)):
        filtered = _filter_graph(data, reader, others)
        return result_text if filtered is None else filtered
    listing = _filter_listing(result_text, reader, others)
    if listing is not None:
        return listing
    if "batch" in tool_name.lower():
        # Decided message by message: one of the reader's in the batch must
        # not carry a teammate's along with it.
        units = [u for u in _MESSAGE_START_RE.split(result_text) if u.strip()]
        if any(_is_teammates(u, _text_headers(u), reader, others) for u in units):
            return json.dumps({"withheld": True, "note": BATCH_WITHHELD_NOTE})
        return result_text
    if _is_teammates(result_text, _text_headers(result_text), reader, others):
        return json.dumps({"withheld": True, "note": WITHHELD_NOTE})
    return result_text


CallTool = Callable[[dict[str, Any]], Awaitable[str]]


def guard_call_tool(call: CallTool) -> CallTool:
    """``call`` with each mailbox read's result passed through
    ``filter_mail_result``: the chat's ``call_tool`` and a script's calls."""
    async def guarded(tool_input: dict[str, Any]) -> str:
        result = await call(tool_input)
        name = tool_input.get("name") if isinstance(tool_input, dict) else None
        if not isinstance(name, str):
            return result
        return filter_mail_result(name, result)

    return guarded
