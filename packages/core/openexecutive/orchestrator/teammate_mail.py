"""A teammate's mail with the Executive is theirs: the chat reaches the
Executive's mailbox only around it.

Teammates email the Executive the way they would message it in chat, and the
Executive's mailbox keeps every one of those threads. The gateway's mail
tools (Gmail through Google Workspace, Outlook through Microsoft 365) reach
whatever the mailbox holds, so without this any turn that may call them (the
principal's, an admin's) could search "from:" a teammate and read, quote or
forward what they sent. ``guard_call_tool`` wraps every mailbox call the
model makes (the chat's ``call_tool``, a script's calls, a workflow step run
from chat) with three rules:

1. **Searches leave teammates out.** A Gmail search gets ``-from: -to: -cc:``
   for every other teammate's address and always asks for headers; an
   Outlook list always selects who a message is from and to. What comes back
   is filtered again, entry by entry.
2. **Mail is reached only by an id the conversation was shown.** Every other
   mail call that names a message or thread (read it, its attachments,
   reply in it, forward it, label it) must name one a filtered search or
   list returned in this conversation (``Session.mail_ids_seen``), or the
   inbound email the turn is answering. An id from anywhere else is refused.
3. **Reads are filtered message by message.** A message, or a message inside
   a thread or batch, is another teammate's when an address of a rostered
   teammate other than the reader appears anywhere in it and none of the
   reader's own addresses is in its From / To / Cc headers. A thread keeps
   the rest and says how many it left out; a single read is withheld. A
   reply that would quote such a thread (``quote_original``) is refused.

Outside mail (vendors, counsel, the principal's contacts) and the reader's
own mail are untouched. The reader is the principal or a teammate on a
surface that proved who they are, or the teammate whose own email the turn
is answering; on any other turn every teammate's mail is left out. An
unattended run, and the internal callers that reach the gateway directly
(the email poller, the brief), are unaffected. Fails closed: when the roster
can't be read, the call is refused.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from openexecutive.people.identity import RosterAllow, addresses_of

logger = logging.getLogger(__name__)

_GW_PREFIX = "google_workspace__"
_M365_PREFIX = "microsoft_365__"

_ADDRESS_RE = re.compile(r"[\w.%+'-]+@[\w.-]+\.\w{2,}", re.UNICODE)
# The header lines that say who a message is between, in workspace-mcp's
# plain-text output ("From: Ada <ada@example.com>").
_HEADER_RE = re.compile(r"^\s*(?:from|to|cc|reply-to)\s*:(.*)$", re.IGNORECASE | re.MULTILINE)
# Where each message of a thread or batch read starts. A body line that
# looks like one only splits it further, which can hide more, never less.
_MESSAGE_START_RE = re.compile(r"^(?=Message ID:|=== Message \d+ ===)", re.MULTILINE)
_THREAD_MESSAGE_RE = re.compile(r"^(?==== Message \d+ ===)", re.MULTILINE)
# A Gmail search listing: its entries ("  3. Message ID: ...") up to the
# usage notes.
_SEARCH_HEAD_RE = re.compile(r"^📧 MESSAGES:\s*$", re.MULTILINE)
_SEARCH_ENTRY_RE = re.compile(r"^  \d+\. Message", re.MULTILINE)
_SEARCH_TAIL_RE = re.compile(r"^💡 USAGE:", re.MULTILINE)
_LISTED_ID_RE = re.compile(r"^\s*(?:\d+\.\s*)?(?:Message|Thread) ID:\s*(\S+)", re.MULTILINE)
# Gmail argument keys that name a message or thread.
_GMAIL_ID_KEYS = frozenset({"message_id", "message_ids", "thread_id", "thread_ids"})
# Outlook: argument keys that name a message (normalized), and a Graph path
# that does.
_M365_ID_KEYS = frozenset({"messageid"})
_M365_PATH_ID_RE = re.compile(r"/messages/([^/?$]+)", re.IGNORECASE)
_M365_LIST_TOOLS = frozenset({"list_mail_messages", "list_mail_folder_messages"})
_M365_PARTY_FIELDS = "id,conversationId,from,sender,toRecipients,ccRecipients,bccRecipients"
_GRAPH_PARTY_KEYS = frozenset({
    "from", "sender", "torecipients", "ccrecipients", "bccrecipients", "replyto",
})

PRIVATE_NOTE = (
    "This email involves another teammate and is their own mail with the "
    "Executive, so it is private to them. Say so plainly and suggest asking "
    "them directly; don't guess what it says."
)
BATCH_NOTE = (
    "At least one of these emails involves another teammate and is their own "
    "mail with the Executive, so it is private to them, and the batch was not "
    "returned. Read the others one at a time; don't guess what the private ones say."
)
LEFT_OUT_NOTE = (
    "Left out {count} {noun} involving another teammate: a teammate's own mail "
    "with the Executive is private to them."
)
UNSEEN_ID_NOTE = (
    "{tool} was not run: it names an email this conversation hasn't found. "
    "Search the mailbox first and use an id from the results."
)
QUOTE_NOTE = (
    "{tool} was not run: that thread holds another teammate's own mail with "
    "the Executive, which is private to them, so it can't be quoted. Send "
    "without quote_original."
)
SEARCH_REFUSED_NOTE = (
    "{tool} is not available for the mailbox here. Use list-mail-messages "
    "with a search or filter instead."
)
ROSTER_NOTE = "{tool} was not run: the roster could not be read. Try again."


@dataclass
class _Sides:
    reader: RosterAllow
    others: RosterAllow
    other_addresses: list[str]
    seen: set[str] = field(default_factory=set)


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _gmail(name: str) -> bool:
    n = _norm(name)
    return n.startswith(_GW_PREFIX) and "gmail" in n


def _m365_ids(value: Any, key: str = "", depth: int = 0) -> Iterator[str]:
    """Every message id an Outlook call's arguments name: by key, or in a
    Graph path (``/me/messages/{id}/attachments/...``)."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _m365_ids(v, str(k), depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _m365_ids(v, key, depth + 1)
    elif isinstance(value, str):
        if re.sub(r"[^a-z]", "", key.lower()) in _M365_ID_KEYS:
            yield value
        yield from (m.group(1) for m in _M365_PATH_ID_RE.finditer(value))


def _gmail_ids(arguments: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in arguments.items():
        if str(key).lower() not in _GMAIL_ID_KEYS:
            continue
        for v in value if isinstance(value, list) else [value]:
            if v not in (None, ""):
                out.append(str(v))
    return out


def is_mail_call(tool_name: object, arguments: Any) -> bool:
    """Whether a gateway call reaches the Executive's mailbox: any Gmail tool,
    any Outlook mail, message or search tool, or an Outlook call naming a
    message."""
    if not isinstance(tool_name, str):
        return False
    if _gmail(tool_name):
        return True
    n = _norm(tool_name)
    if not n.startswith(_M365_PREFIX):
        return False
    tail = n[len(_M365_PREFIX):]
    if "search" in tail and "onedrive" not in tail:
        # A Graph search can return mail hits without naming a message.
        return True
    return "mail" in tail or "message" in tail or any(True for _ in _m365_ids(arguments))


def _reader_id(session: Any) -> int | None:
    """Who is reading: the principal or a teammate on a surface that proved
    it, or the teammate whose own email this turn answers. None otherwise."""
    from openexecutive.orchestrator.content_trust import principal_speaking
    from openexecutive.orchestrator.people_tools import teammate_on_verified_surface
    from openexecutive.people.store import find_principal_person, get_person

    caller = getattr(session, "caller_person_id", None)
    if principal_speaking(session):
        if caller is not None:
            return int(caller)
        principal = find_principal_person()
        return principal.id if principal is not None else None
    teammate = teammate_on_verified_surface(session)
    if teammate is not None:
        return int(teammate.id)
    if str(getattr(session, "origin_channel", "") or "") == "email" and caller is not None:
        person = get_person(int(caller))
        if person is not None and person.kind == "team" and not person.archived:
            return int(person.id) if person.id is not None else None
    return None


def _sides(session: Any) -> _Sides | None:
    """This turn's reader and other teammates, or None when the roster can't
    be read."""
    from openexecutive.people.store import list_people

    try:
        reader_id = _reader_id(session) if session is not None else None
        team = [p for p in list_people() if p.kind == "team"]
    except Exception:
        logger.warning("teammate_mail: roster read failed — refusing", exc_info=True)
        return None
    reader = [p for p in team if reader_id is not None and p.id == reader_id]
    others = [p for p in team if reader_id is None or p.id != reader_id]
    seen = getattr(session, "mail_ids_seen", None) if session is not None else None
    return _Sides(
        reader=RosterAllow(reader),
        others=RosterAllow(others),
        other_addresses=sorted({a.strip().lower() for p in others for a in addresses_of(p) if a}),
        seen=seen if isinstance(seen, set) else set(),
    )


def allow_mail_ids(session: Any, *ids: str) -> None:
    """Let this conversation reach these mailbox ids (the inbound email a
    turn is answering)."""
    seen = getattr(session, "mail_ids_seen", None)
    if isinstance(seen, set):
        seen.update(i for i in ids if i)


def _addresses(text: str) -> list[str]:
    return _ADDRESS_RE.findall(text)


def _theirs(unit: str, headers: str, sides: _Sides) -> bool:
    """Whether ``unit`` is another teammate's mail: one of their addresses
    is anywhere in it and none of the reader's is in ``headers``."""
    if not any(a in sides.others for a in _addresses(unit)):
        return False
    return not any(a in sides.reader for a in _addresses(headers))


def _text_headers(unit: str) -> str:
    return "\n".join(m.group(1) for m in _HEADER_RE.finditer(unit))


def _left_out(count: int) -> str:
    return LEFT_OUT_NOTE.format(count=count, noun="message" if count == 1 else "messages")


def _refusal(note: str) -> str:
    return json.dumps({"error": note})


# ── before the call ──────────────────────────────────────────────────────


def _gmail_query(query: str, sides: _Sides) -> str:
    excluded = " ".join(f"-from:{a} -to:{a} -cc:{a}" for a in sides.other_addresses)
    if not excluded:
        return query
    return f"({query}) {excluded}" if query.strip() else excluded


def _with_party_fields(select: Any) -> Any:
    fields = _M365_PARTY_FIELDS.split(",")
    if isinstance(select, list):
        return [*select, *(f for f in fields if f not in select)]
    if isinstance(select, str) and select.strip():
        return f"{select},{_M365_PARTY_FIELDS}"
    return select


def _prepare(name: str, arguments: dict[str, Any], sides: _Sides) -> tuple[str | None, dict[str, Any]]:
    """``(refusal, arguments to send)`` for a mailbox call."""
    n = _norm(name)
    if _gmail(name):
        if n == _GW_PREFIX + "search_gmail_messages":
            return None, {
                **arguments,
                "query": _gmail_query(str(arguments.get("query") or ""), sides),
                "include_headers": True,
            }
        if any(i not in sides.seen for i in _gmail_ids(arguments)):
            return _refusal(UNSEEN_ID_NOTE.format(tool=name)), arguments
        return None, arguments
    tail = n[len(_M365_PREFIX):]
    if "search" in tail and "onedrive" not in tail:
        return _refusal(SEARCH_REFUSED_NOTE.format(tool=name)), arguments
    if any(i not in sides.seen for i in _m365_ids(arguments)):
        return _refusal(UNSEEN_ID_NOTE.format(tool=name)), arguments
    if tail in _M365_LIST_TOOLS or tail == "get_mail_message":
        return None, {
            k: _with_party_fields(v) if str(k).lower() == "select" else v
            for k, v in arguments.items()
        }
    return None, arguments


# ── after the call ───────────────────────────────────────────────────────


def _filter_listing(text: str, sides: _Sides) -> str | None:
    """A Gmail search listing with other teammates' entries dropped and the
    kept ids remembered, or None when ``text`` is not one."""
    head = _SEARCH_HEAD_RE.search(text)
    if head is None:
        return None
    tail = _SEARCH_TAIL_RE.search(text, head.end())
    body_end = tail.start() if tail else len(text)
    body = text[head.end():body_end]
    starts = [m.start() for m in _SEARCH_ENTRY_RE.finditer(body)]
    if not starts:
        return text
    entries = [body[s:e] for s, e in zip(starts, [*starts[1:], len(body)], strict=True)]
    # An entry whose headers didn't come back can't be checked: dropped.
    kept = [e for e in entries if "From:" in e and not _theirs(e, _text_headers(e), sides)]
    for entry in kept:
        sides.seen.update(_LISTED_ID_RE.findall(entry))
    dropped = len(entries) - len(kept)
    if not dropped:
        return text
    if not kept:
        return f"No messages to show. {_left_out(dropped)}"
    return text[:head.end()] + "\n" + "".join(kept) + _left_out(dropped) + "\n\n" + text[body_end:]


def _filter_thread(text: str, sides: _Sides) -> str:
    parts = _THREAD_MESSAGE_RE.split(text)
    head, messages = parts[0], parts[1:]
    if not messages:
        if _theirs(text, _text_headers(text), sides):
            return json.dumps({"withheld": True, "note": PRIVATE_NOTE})
        return text
    kept = [m for m in messages if not _theirs(m, _text_headers(m), sides)]
    dropped = len(messages) - len(kept)
    if not dropped:
        return text
    if not kept:
        return json.dumps({"withheld": True, "note": PRIVATE_NOTE})
    return head + "".join(kept) + "\n" + _left_out(dropped)


def _graph_parties(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return "\n".join(
        json.dumps(v, ensure_ascii=False)
        for k, v in item.items()
        if isinstance(k, str) and k.lower() in _GRAPH_PARTY_KEYS
    )


def _graph_theirs(item: Any, sides: _Sides) -> bool:
    return _theirs(json.dumps(item, ensure_ascii=False), _graph_parties(item), sides)


def _filter_graph(text: str, sides: _Sides, listing: bool) -> str:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if data is None:
        if _theirs(text, _text_headers(text), sides):
            return json.dumps({"withheld": True, "note": PRIVATE_NOTE})
        return text
    items = data.get("value") if isinstance(data, dict) else data if isinstance(data, list) else None
    if isinstance(items, list):
        kept = [i for i in items if not _graph_theirs(i, sides)]
        if listing:
            for item in kept:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    sides.seen.add(item["id"])
        dropped = len(items) - len(kept)
        if not dropped:
            return text
        base = data if isinstance(data, dict) else {}
        return json.dumps({**base, "value": kept, "private_note": _left_out(dropped)}, ensure_ascii=False)
    if _graph_theirs(data, sides):
        return json.dumps({"withheld": True, "note": PRIVATE_NOTE})
    return text


def _filter(name: str, text: str, sides: _Sides) -> str:
    n = _norm(name)
    if not _gmail(name):
        return _filter_graph(text, sides, listing=n[len(_M365_PREFIX):] in _M365_LIST_TOOLS)
    if n == _GW_PREFIX + "search_gmail_messages":
        listing = _filter_listing(text, sides)
        return text if listing is None else listing
    if not any(a in sides.others for a in _addresses(text)):
        return text
    if "batch" in n:
        # Decided message by message: one of the reader's in the batch must
        # not carry a teammate's along with it.
        units = [u for u in _MESSAGE_START_RE.split(text) if u.strip()]
        if any(_theirs(u, _text_headers(u), sides) for u in units):
            return json.dumps({"withheld": True, "note": BATCH_NOTE})
        return text
    if "thread" in n:
        return _filter_thread(text, sides)
    if _theirs(text, _text_headers(text), sides):
        return json.dumps({"withheld": True, "note": PRIVATE_NOTE})
    return text


# ── the wrapper ──────────────────────────────────────────────────────────


CallTool = Callable[[dict[str, Any]], Awaitable[str]]


async def _quotes_teammate(call: CallTool, arguments: dict[str, Any], sides: _Sides) -> bool:
    """Whether a Gmail send / draft with ``quote_original`` would quote a
    thread holding another teammate's mail. A failed read counts as yes."""
    thread_id = arguments.get("thread_id")
    if not arguments.get("quote_original") or not thread_id:
        return False
    try:
        text = await call({
            "name": _GW_PREFIX + "get_gmail_thread_content",
            "arguments": {
                "thread_id": thread_id,
                "user_google_email": arguments.get("user_google_email", ""),
            },
        })
    except Exception:
        logger.warning("teammate_mail: thread read for quote_original failed — refusing", exc_info=True)
        return True
    return not isinstance(text, str) or _filter_thread(text, sides) != text


def guard_call_tool(call: CallTool) -> CallTool:
    """``call`` with the module's rules on every mailbox call the model makes."""
    from openexecutive.orchestrator.schedule_tools import current_session

    async def guarded(tool_input: dict[str, Any]) -> str:
        name = tool_input.get("name") if isinstance(tool_input, dict) else None
        arguments = tool_input.get("arguments", {}) if isinstance(tool_input, dict) else {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                arguments = {}
        if not isinstance(name, str) or not is_mail_call(name, arguments):
            return await call(tool_input)
        session = current_session.get()
        if session is not None and getattr(session, "unattended", False):
            return await call(tool_input)
        sides = _sides(session)
        if sides is None:
            return _refusal(ROSTER_NOTE.format(tool=name))
        if not sides.other_addresses or not isinstance(arguments, dict):
            return await call(tool_input)
        refused, arguments = _prepare(name, arguments, sides)
        if refused is not None:
            return refused
        if _gmail(name) and await _quotes_teammate(call, arguments, sides):
            return _refusal(QUOTE_NOTE.format(tool=name))
        result = await call({**tool_input, "arguments": arguments})
        if not isinstance(result, str):
            return result
        return _filter(name, result, sides)

    return guarded
