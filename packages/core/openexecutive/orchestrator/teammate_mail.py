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
   for every other teammate's address, unless the reader is on the message
   too (not on a background run), and always asks for headers; an
   Outlook list always selects who a message is from and to. What comes back
   is filtered again, entry by entry.
2. **Mail is reached only by an id the conversation was shown.** Every other
   mail call that names a message or thread (read it, its attachments,
   reply in it, forward it, label it) must name one a filtered search or
   list returned to this same reader in this conversation
   (``Session.mail_ids_seen``), or the inbound email the turn is answering.
   An id from anywhere else is refused. An Outlook call that forwards,
   replies to or downloads from a message first reads who it is between,
   and is refused when it is another teammate's.
3. **Reads are filtered message by message.** A message, or a message inside
   a thread or batch, is another teammate's when an address of a rostered
   teammate other than the reader appears anywhere in it and none of the
   reader's own addresses is in its From / To / Cc headers. A thread keeps
   the rest and says how many it left out; a single read is withheld. A
   reply that would quote such a thread (``quote_original``) is refused.

Outside mail (vendors, counsel, the principal's contacts) and the reader's
own mail are untouched. The reader is the principal or a teammate on a
surface that proved who they are, or the teammate whose own email the turn
is answering when Gmail authenticated it; on any other turn every
teammate's mail is left out. A conversation in which a teammate other than
the principal was shown their own mail becomes theirs alone, as Act as me's
do (``session_store.mark_mail_private``), and so do the rest of that turn's
audit rows. In a web chat its owner shared with teammates
(``Session.shared_chat``) a teammate is no reader: everyone in it would see
their mail. A background run the model drives (a follow-up, the reflection)
reads as the principal it reports to; a workflow step outside any session
reads as nobody. An Outlook inbox rule that forwards, or names another
teammate, is refused. The internal callers that reach the gateway directly
(the email poller, the brief) are unaffected. Fails closed: when the roster
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
RULE_NOTE = (
    "{tool} was not run: a mailbox rule that forwards mail, or names another "
    "teammate, could pass on a teammate's own mail with the Executive, which "
    "is private to them."
)
QUERY_NOTE = (
    "{tool} was not run: close every quote and bracket in the query and try again."
)
OUTLOOK_THEIRS_NOTE = (
    "{tool} was not run: that email involves another teammate and is their "
    "own mail with the Executive, which is private to them."
)


@dataclass
class _Sides:
    reader: RosterAllow
    others: RosterAllow
    other_addresses: list[str]
    reader_addresses: list[str] = field(default_factory=list)
    reader_id: int | None = None
    reader_is_principal: bool = False
    seen: set[str] = field(default_factory=set)
    # Off only outside any session (a scheduled workflow step), which has
    # nowhere to keep what it was shown; its searches and reads stay filtered.
    gate_ids: bool = True

    @property
    def key(self) -> str:
        return str(self.reader_id) if self.reader_id is not None else "-"

    def saw(self, mail_id: str) -> bool:
        return f"{self.key}:{mail_id}" in self.seen or f"*:{mail_id}" in self.seen

    def remember(self, mail_id: str) -> None:
        self.seen.add(f"{self.key}:{mail_id}")


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
    if getattr(session, "unattended", False) is True:
        # A follow-up a teammate queued reads as them: what it writes is
        # sent to them (`Session.run_for_person_id`).
        run_for = getattr(session, "run_for_person_id", None)
        if isinstance(run_for, int) and not isinstance(run_for, bool):
            return run_for
        # Any other background run (a follow-up, the reflection) works for
        # the principal and reports to them: it reads as them.
        principal = find_principal_person()
        return principal.id if principal is not None else None
    if principal_speaking(session):
        # Not in a web chat they shared: everyone in it reads the reply.
        if getattr(session, "shared_chat", False) is True:
            return None
        if caller is not None:
            return int(caller)
        principal = find_principal_person()
        return principal.id if principal is not None else None
    teammate = teammate_on_verified_surface(session)
    if teammate is not None:
        # A chat the owner shared is read by everyone in it, and their mail
        # with the Executive can't become theirs alone there
        # (``mark_mail_private`` keeps the owner's): none of it is shown.
        if getattr(session, "shared_chat", False) is True:
            return None
        return int(teammate.id)
    if (
        str(getattr(session, "origin_channel", "") or "") == "email"
        and caller is not None
        and getattr(session, "email_sender_authenticated", False) is True
    ):
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
        # A background run reads as the principal but may report to someone
        # else (a teammate's follow-up), so its searches keep every other
        # teammate out even where the principal is on the mail too.
        reader_addresses=[] if getattr(session, "unattended", False) is True else sorted(
            {a.strip().lower() for p in reader for a in addresses_of(p) if a}
        ),
        reader_id=reader_id,
        reader_is_principal=any(p.is_principal for p in reader),
        seen=seen if isinstance(seen, set) else set(),
        gate_ids=session is not None,
    )


def allow_mail_ids(session: Any, *ids: str) -> None:
    """Let this conversation reach these mailbox ids (the inbound email a
    turn is answering)."""
    seen = getattr(session, "mail_ids_seen", None)
    if isinstance(seen, set):
        seen.update(f"*:{i}" for i in ids if i)


def _addresses(text: str) -> list[str]:
    return _ADDRESS_RE.findall(text)


def _theirs(unit: str, headers: str, sides: _Sides) -> bool:
    """Whether ``unit`` is another teammate's mail: one of their addresses
    is anywhere in it and none of the reader's is in ``headers``."""
    if not any(a in sides.others for a in _addresses(unit)):
        return False
    return not any(a in sides.reader for a in _addresses(headers))


def _text_headers(unit: str) -> str:
    """The From / To / Cc / Reply-To values of a message's header block: the
    lines before its first blank line. A body line that looks like a header
    (a quoted "To: ...") is the sender's text and never counts."""
    block = re.split(r"\n[ \t]*\n", unit.lstrip("\n"), maxsplit=1)[0]
    return "\n".join(m.group(1) for m in _HEADER_RE.finditer(block))


def _reader_in_headers(name: str, text: str, sides: _Sides) -> bool:
    """Whether a result shows the reader mail they are on: their address in
    a message's From / To / Cc, not anywhere in a body."""
    if _gmail(name):
        units = [u for u in _MESSAGE_START_RE.split(text) if u.strip()]
        headers = "\n".join(_text_headers(u) for u in units)
        return any(a in sides.reader for a in _addresses(headers))
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return False
    items = data.get("value") if isinstance(data, dict) else data
    items = items if isinstance(items, list) else [data]
    return any(a in sides.reader for i in items for a in _addresses(_graph_parties(i)))


def _left_out(count: int) -> str:
    return LEFT_OUT_NOTE.format(count=count, noun="message" if count == 1 else "messages")


def _refusal(note: str) -> str:
    return json.dumps({"error": note})


# ── before the call ──────────────────────────────────────────────────────


def _balanced(query: str) -> bool:
    """Whether the model's query leaves the exclusions appended to it alone:
    quotes paired, brackets closed."""
    if query.count('"') % 2:
        return False
    for open_, close in ("()", "{}"):
        depth = 0
        for ch in query:
            depth += (ch == open_) - (ch == close)
            if depth < 0:
                return False
        if depth:
            return False
    return True


def _gmail_query(query: str, sides: _Sides) -> str:
    excluded = " ".join(f"-{{from:{a} to:{a} cc:{a}}}" for a in sides.other_addresses)
    if not excluded:
        return query
    if sides.reader_addresses:
        # Mail the reader is on is theirs too, whoever else it went to: an
        # email the Executive sent to the reader and a teammate stays
        # findable. Reading it is still decided message by message.
        mine = " ".join(f"from:{a} to:{a} cc:{a}" for a in sides.reader_addresses)
        excluded = f"{{({excluded}) {mine}}}"
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
            if not _balanced(str(arguments.get("query") or "")):
                return _refusal(QUERY_NOTE.format(tool=name)), arguments
            return None, {
                **arguments,
                "query": _gmail_query(str(arguments.get("query") or ""), sides),
                "include_headers": True,
            }
        if sides.gate_ids and any(not sides.saw(i) for i in _gmail_ids(arguments)):
            return _refusal(UNSEEN_ID_NOTE.format(tool=name)), arguments
        return None, arguments
    tail = n[len(_M365_PREFIX):]
    if "search" in tail and "onedrive" not in tail:
        return _refusal(SEARCH_REFUSED_NOTE.format(tool=name)), arguments
    if "rule" in tail or "mailbox_settings" in tail:
        # An inbox rule or mailbox setting acts on mail still to come: one
        # that forwards, or names another teammate, is refused.
        text = json.dumps(arguments, ensure_ascii=False).lower()
        if "forward" in text or "redirect" in text or any(a in sides.others for a in _addresses(text)):
            return _refusal(RULE_NOTE.format(tool=name)), arguments
    if sides.gate_ids and any(not sides.saw(i) for i in _m365_ids(arguments)):
        return _refusal(UNSEEN_ID_NOTE.format(tool=name)), arguments
    if tail in _M365_LIST_TOOLS or tail == "get_mail_message":
        return None, {
            k: _with_party_fields(v) if re.sub(r"[^a-z]", "", str(k).lower()) == "select" else v
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
        return "No messages to show."
    entries = [body[s:e] for s, e in zip(starts, [*starts[1:], len(body)], strict=True)]
    # An entry whose headers didn't come back can't be checked: dropped.
    kept = [e for e in entries if "From:" in e and not _theirs(e, _text_headers(e), sides)]
    for entry in kept:
        for mail_id in _LISTED_ID_RE.findall(entry):
            sides.remember(mail_id)
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
    # The head's subject is the first message's, which may be one left out.
    head = "".join(line for line in head.splitlines(keepends=True) if line.startswith("Thread ID:"))
    return head + "\n" + "".join(kept) + "\n" + _left_out(dropped)


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
                    sides.remember(item["id"])
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


_M365_PARTY_READ = _M365_PARTY_FIELDS + ",body"


async def _outlook_theirs(call: CallTool, name: str, arguments: dict[str, Any], sides: _Sides) -> bool:
    """Whether an Outlook call that acts on a message (forward, reply, an
    attachment) names another teammate's. A failed read counts as yes."""
    tail = _norm(name)[len(_M365_PREFIX):]
    if tail == "get_mail_message" or tail in _M365_LIST_TOOLS:
        return False
    for message_id in set(_m365_ids(arguments)):
        try:
            text = await call({
                "name": _M365_PREFIX + "get-mail-message",
                "arguments": {"messageId": message_id, "select": _M365_PARTY_READ},
            })
            data = json.loads(text)
        except Exception:
            logger.warning("teammate_mail: Outlook party read failed — refusing", exc_info=True)
            return True
        if not isinstance(data, dict) or "error" in data or _graph_theirs(data, sides):
            return True
    return False


def _keep_private(session: Any, sides: _Sides) -> None:
    """A teammate other than the principal was shown their own mail: the
    conversation becomes theirs alone, as Act as me's do."""
    session_id = getattr(session, "session_id", None)
    if sides.reader_id is None or sides.reader_is_principal or not session_id:
        return
    from openexecutive.delegation.settings import history_len
    from openexecutive.memory.replay import is_replay
    from openexecutive.memory.session_store import mark_mail_private

    # A replay (memory.replay) keeps no conversation: marking one would leave
    # an empty chat in the teammate's list.
    if not is_replay(session):
        try:
            mark_mail_private(str(session_id), sides.reader_id, history_len=history_len(session))
        except Exception:
            logger.exception("teammate_mail: couldn't mark the conversation private")
    # The rest of this turn's rows, its answer included, are theirs too.
    from openexecutive.delegation.settings import turn_delegation

    pinned = turn_delegation(session)
    if pinned is not None and pinned.person_id in (None, sides.reader_id):
        pinned.touched_mail = True
        pinned.person_id = sides.reader_id


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
        if not _gmail(name) and await _outlook_theirs(call, name, arguments, sides):
            return _refusal(OUTLOOK_THEIRS_NOTE.format(tool=name))
        result = await call({**tool_input, "arguments": arguments})
        if not isinstance(result, str):
            return result
        filtered = _filter(name, result, sides)
        if _reader_in_headers(name, filtered, sides):
            _keep_private(session, sides)
        return filtered

    return guarded


def guard_in_turn(call: CallTool) -> CallTool:
    """``guard_call_tool`` for a caller that runs both inside a chat turn and
    on its own (a workflow step, the reflection pass). Outside a session the
    reader is nobody: searches and reads leave every teammate's mail out."""
    return guard_call_tool(call)
