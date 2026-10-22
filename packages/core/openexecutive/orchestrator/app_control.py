"""App control from chat: the Executive does what the app's own buttons do.

"Pin this", "rename it Q3 plan", "delete that chat", "turn off Handle it for
me": three tools in the ``app`` tool group (``tool_groups``) do these.

- ``find_in_app(kind, query)`` lists the things of one kind the speaker may
  act on (their chats, topics, documents, …), each with an id.
- ``do_in_app(action, id, value)`` does one action on one of them.
- ``open_in_app(screen)`` gives a link to a screen, for what chat doesn't do.

**The same code as the button.** Each action calls the API route the screen's
button calls, as a function, with a request that names the speaker
(``_as_speaker``). The route's own checks decide what this person may do: a
chat that isn't theirs is a 404 here as it is on the page. The speaker is the
one the chat surface verified (``correction_tools._speaker``); a turn with
none (an email, an unattended run) gets a refusal. Some routes check nothing
about the caller because their page is the principal's; those actions are
``principal_only``.

**What waits for a tap.** An action marked ``confirm`` (a delete, a share,
turning on something that sends as the person, anything that lets more go
on its own) is never done in the turn. It becomes a card in the chat
(decision class ``app_action``, ``api.routes.decisions``) that only the
speaker can see and answer, with the action spelled out. Their tap runs it
with their own request to ``/decisions/{id}/approve``, so a route that needs
a signed sign-in gets one, and the route checks them again then. After a turn
has read text from outside the team (``outside_content``) or the speaker's
own mail, every action waits for a tap: planted text ("delete the Q3 chat")
can't act on its own. A card left a day goes stale.

**Adding to it.** ``register_kind`` / ``register_action`` / ``register_screen``
add to the catalog (``app_control_catalog`` holds this repo's), at startup
before any turn, so each turn sees one fixed tool list. The tool definitions
are rebuilt in place, so the list holding them sees the change.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from openexecutive.delegation.verified import from_chat

logger = logging.getLogger(__name__)

FIND_IN_APP = "find_in_app"
DO_IN_APP = "do_in_app"
OPEN_IN_APP = "open_in_app"
APP_TOOL_NAMES: frozenset[str] = frozenset({FIND_IN_APP, DO_IN_APP, OPEN_IN_APP})

# The decision class a card is stored as (api.routes.decisions).
DECISION_CLASS = "app_action"
# A card not answered in this long is stale: what it named may have changed.
CARD_LIFETIME = timedelta(days=1)
# What "this" means for a chat: the conversation the person is in.
THIS = "this"

_FIND_MAX = 25
_VALUE_MAX = 300


class Refused(Exception):
    """An action that can't be done, with what to tell the person."""


@dataclass(frozen=True)
class Item:
    """One thing in the app the speaker may act on."""

    id: str
    title: str
    detail: str = ""


# (request) → everything of the kind the caller may act on.
Finder = Callable[[Any], Awaitable[list[Item]]]
# (request, item id, value) → what was done, past tense ("Pinned “Q3 plan”").
# Raises Refused, or lets the route's HTTPException through.
Runner = Callable[[Any, str, str], Awaitable[str]]
# (request, item id, value) → the value to store and run with, worked out by
# the server in the turn (e.g. the alert ids shown), or Refused.
Preparer = Callable[[Any, str, str], Awaitable[str]]


@dataclass(frozen=True)
class Kind:
    name: str
    # What the person calls one, e.g. "chat".
    label: str
    find: Finder
    principal_only: bool = False
    # Items of this kind need no id (a new department, the alert board).
    no_item: bool = False


@dataclass(frozen=True)
class Action:
    name: str
    kind: str
    # What it does, for the tool description: "Pin a chat to the top of the list".
    does: str
    run: Runner
    # Whether it waits for a tap: always, or per (item id, value).
    confirm: bool | Callable[[str, str], bool] = False
    # What ``value`` is, when the action takes one.
    value: str | None = None
    # The card's line, with {title} (quoted) and {value}: "Delete the chat {title}".
    card: str = ""
    principal_only: bool = False
    prepare: Preparer | None = None

    def needs_tap(self, item_id: str, value: str) -> bool:
        return self.confirm(item_id, value) if callable(self.confirm) else bool(self.confirm)


@dataclass
class _Catalog:
    kinds: dict[str, Kind] = field(default_factory=dict)
    actions: dict[str, Action] = field(default_factory=dict)
    # name → (app path, what it is)
    screens: dict[str, tuple[str, str]] = field(default_factory=dict)


CATALOG = _Catalog()


# --------------------------------------------------------------------------- #
# Tool definitions (rebuilt in place as the catalog grows)
# --------------------------------------------------------------------------- #

FIND_IN_APP_TOOL: dict[str, Any] = {"name": FIND_IN_APP}
DO_IN_APP_TOOL: dict[str, Any] = {"name": DO_IN_APP}
OPEN_IN_APP_TOOL: dict[str, Any] = {"name": OPEN_IN_APP}


def _rebuild() -> None:
    kinds = sorted(k for k, kind in CATALOG.kinds.items() if not kind.no_item)
    FIND_IN_APP_TOOL.update({
        "description": (
            "List things in Open Executive the person can change, with their ids: "
            + ", ".join(f"{k} ({CATALOG.kinds[k].label}s)" for k in kinds)
            + ". Call it before do_in_app to find the id of what they named. query "
            "narrows by words in the name; leave it out to list the newest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": kinds},
                "query": {"type": "string", "description": "Words from its name, as the person said them."},
            },
            "required": ["kind"],
        },
    })
    lines = []
    for name in sorted(CATALOG.actions):
        action = CATALOG.actions[name]
        extra = f" value: {action.value}." if action.value else ""
        lines.append(f"- {name} ({action.kind}): {action.does}.{extra}")
    DO_IN_APP_TOOL.update({
        "description": (
            "Do something in Open Executive that the person asked for, as its own "
            "button would: pin, rename, delete, share, turn a setting on or off. "
            f"id comes from find_in_app; for the chat you are in, use id \"{THIS}\". "
            "Some actions (deletes, shares, anything that sends as them or lets more "
            "happen on its own) leave a card for them to confirm instead: say it is "
            "waiting for their OK, never that it is done. If it is refused, say why "
            "in their words; don't retry.\n" + "\n".join(lines)
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(CATALOG.actions)},
                "id": {"type": "string", "description": "The item's id from find_in_app, or \"this\" for this chat."},
                "value": {"type": "string", "description": "The action's value, when it takes one."},
            },
            "required": ["action", "id"],
        },
    })
    screens = sorted(CATALOG.screens)
    OPEN_IN_APP_TOOL.update({
        "description": (
            "Give the person a link to a screen in Open Executive, for something chat "
            "can't do or when they ask to go somewhere ('open my settings'). Screens: "
            + "; ".join(f"{s} ({CATALOG.screens[s][1]})" for s in screens)
            + ". The link shows under your reply."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"screen": {"type": "string", "enum": screens}},
            "required": ["screen"],
        },
    })


def register_kind(kind: Kind) -> None:
    if kind.name in CATALOG.kinds:
        raise ValueError(f"kind {kind.name!r} is already registered")
    CATALOG.kinds[kind.name] = kind
    _rebuild()


def register_action(action: Action) -> None:
    if action.name in CATALOG.actions:
        raise ValueError(f"action {action.name!r} is already registered")
    if action.kind not in CATALOG.kinds:
        raise ValueError(f"action {action.name!r} names unknown kind {action.kind!r}")
    CATALOG.actions[action.name] = action
    _rebuild()


def register_screen(name: str, path: str, label: str) -> None:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"screen {name!r} needs an app path")
    CATALOG.screens[name] = (path, label)
    _rebuild()


# --------------------------------------------------------------------------- #
# Who is asking, and a request that names them
# --------------------------------------------------------------------------- #


def _session() -> Any:
    from openexecutive.orchestrator.schedule_tools import current_session

    return current_session.get()


def _speaker() -> Any:
    from openexecutive.orchestrator.correction_tools import _speaker as speaker

    return speaker()


def _running_app() -> Any:
    """The API's app when it is loaded (its knowledge store is on its state),
    else a stand-in whose state has nothing, so routes build their own."""
    main = sys.modules.get("openexecutive.api.main")
    app = getattr(main, "app", None)
    return app if app is not None else SimpleNamespace(state=SimpleNamespace())


class _AsSpeaker:
    """A request naming the turn's verified speaker, for calling a route as a
    function in the turn. Only ever made from ``_speaker()``, never from
    anything the model wrote."""

    def __init__(self, person: Any, body: dict[str, Any] | None = None) -> None:
        from openexecutive.api.caller import Caller

        email = str(getattr(person, "email", "") or "").strip().lower()
        if email:
            who = Caller("user", email)
        elif getattr(person, "is_principal", False):
            # A principal with no email is who a request naming no one is.
            who = Caller("operator")
        else:
            raise Refused("I can't tell the app who you are. Do this on the screen instead.")
        self.state = SimpleNamespace(caller=who)
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.client = None
        self.app = _running_app()
        self._body = body or {}

    async def json(self) -> Any:
        return self._body


class _WithBody:
    """A real request with the JSON body a route reads set to ``body``: for a
    route that reads its own body, run on a tap whose body is the card's."""

    def __init__(self, request: Any, body: dict[str, Any]) -> None:
        self._request = request
        self._body = body

    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)

    async def json(self) -> Any:
        return self._body


def with_body(request: Any, body: dict[str, Any]) -> Any:
    """``request`` reading ``body`` as its JSON (for the catalog)."""
    if isinstance(request, _AsSpeaker):
        request._body = body
        return request
    return _WithBody(request, body)


def refusal_text(exc: BaseException) -> str:
    """What a route's refusal says, in the person's words."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("detail")
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    text = " ".join(str(detail or exc or "").split())
    if getattr(exc, "status_code", None) == 404 or not text:
        return text or "That isn't there, or isn't yours to change."
    return text[:300]


def _error(text: str) -> str:
    return json.dumps({"error": text})


def _clean_value(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:_VALUE_MAX]


def _turn_read_outside(session: Any) -> bool:
    """Whether this turn read text from outside the team or the speaker's own
    mail: from then on every action waits for a tap."""
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator import outside_content

    try:
        if outside_content.read_outside():
            return True
    except Exception:
        return True
    pinned = turn_delegation(session)
    if pinned is None:
        return False
    from openexecutive.delegation.lockdown import mail_lock_holds
    from openexecutive.orchestrator import outside_mode

    # The mail it read holds as the safety level says (Careful: a
    # conversation that read it; Balanced: a read the check flagged or
    # hasn't checked yet; Hands-off: nothing).
    try:
        state = outside_content.current()
        if getattr(pinned, "touched_mail", False) is True and (state is None or state.mode == outside_mode.ASK):
            return True
        return mail_lock_holds(pinned)
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def _items(kind: Kind, request: Any) -> list[Item]:
    from fastapi import HTTPException

    try:
        return await kind.find(request)
    except Refused as exc:
        raise exc
    except HTTPException as exc:
        raise Refused(refusal_text(exc)) from None


def _matches(item: Item, query: str) -> bool:
    words = [w for w in query.lower().split() if w]
    text = f"{item.title} {item.detail} {item.id}".lower()
    return all(w in text for w in words)


def _private_here() -> bool:
    """Whether only the speaker reads this conversation: their list of chats,
    settings and rules isn't for a shared chat or a channel."""
    from openexecutive.delegation.settings import private_conversation

    return private_conversation(_session())


_NOT_PRIVATE = "Others can read this conversation, so do that in your own chat with me or on the screen."


async def handle_find_in_app(tool_input: dict[str, Any]) -> str:
    person = _speaker()
    if person is None:
        return _error("I can only do this when I know who is asking: in the web app signed in, or your own chat app.")
    if not _private_here():
        return _error(_NOT_PRIVATE)
    kind = CATALOG.kinds.get(str(tool_input.get("kind") or ""))
    if kind is None:
        return _error(f"kind must be one of {', '.join(sorted(CATALOG.kinds))}")
    if kind.principal_only and not getattr(person, "is_principal", False):
        return _error(f"Only the account owner can change {kind.label}s.")
    try:
        items = await _items(kind, _AsSpeaker(person))
    except Refused as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("find_in_app: listing %s failed", kind.name)
        return _error("Couldn't read that list just now.")
    query = _clean_value(tool_input.get("query"))
    found = [i for i in items if _matches(i, query)] if query else items
    return json.dumps({
        "kind": kind.name,
        "items": [
            {"id": i.id, "title": i.title, **({"detail": i.detail} if i.detail else {})}
            for i in found[:_FIND_MAX]
        ],
        **({"more": len(found) - _FIND_MAX} if len(found) > _FIND_MAX else {}),
    })


def card_text(action: Action, title: str, value: str) -> str:
    """What a card says it will do, naming the item and the new value, so a
    card left after outside content was read shows exactly what a tap does."""
    named = f"“{title}”" if title else "it"
    if action.card:
        return action.card.format(title=named, value=value)
    text = action.does + (f": {named}" if title else "")
    return text + (f" → “{value}”" if value else "")


async def _title_of(kind: Kind, request: Any, item_id: str) -> str | None:
    """The item's name when the speaker may act on it, else None."""
    if kind.no_item:
        return ""
    for item in await _items(kind, request):
        if item.id == item_id:
            return item.title
    return None


def _audit(event: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    session = _session()
    try:
        log_event(
            event, summary, session_id=getattr(session, "session_id", None),
            actor="executive", details=details,
        )
    except Exception:
        logger.warning("app_control: audit write failed", exc_info=True)


async def handle_do_in_app(tool_input: dict[str, Any]) -> str:
    from fastapi import HTTPException

    session = _session()
    person = _speaker()
    if person is None or getattr(person, "id", None) is None:
        return _error("I can only do this when I know who is asking: in the web app signed in, or your own chat app.")
    if not _private_here():
        return _error(_NOT_PRIVATE)
    action = CATALOG.actions.get(str(tool_input.get("action") or ""))
    if action is None:
        return _error("Unknown action. Pick one from the list.")
    kind = CATALOG.kinds[action.kind]
    if (action.principal_only or kind.principal_only) and not getattr(person, "is_principal", False):
        return _error("Only the account owner can do that.")
    item_id = _clean_value(tool_input.get("id"))
    if item_id == THIS and action.kind == "chat":
        item_id = str(getattr(session, "session_id", "") or "")
    value = _clean_value(tool_input.get("value"))
    if action.value and not value:
        return _error(f"{action.name} needs a value: {action.value}.")
    if not item_id and not kind.no_item:
        return _error("id is required: find it with find_in_app.")
    try:
        request = _AsSpeaker(person)
        title = await _title_of(kind, request, item_id)
        if title is None:
            return _error(f"That {kind.label} isn't there, or isn't yours to change. Look it up with find_in_app.")
        if action.prepare is not None:
            with from_chat():
                value = await action.prepare(request, item_id, value)
    except Refused as exc:
        return _error(str(exc))
    except HTTPException as exc:
        return _error(refusal_text(exc))
    except Exception:
        logger.exception("do_in_app: checking %s failed", action.name)
        return _error("Couldn't check that just now. Try again in a moment.")

    details = {"action": action.name, "kind": kind.name, "id": item_id, "person_id": person.id}
    if action.needs_tap(item_id, value) or _turn_read_outside(session):
        card = card_text(action, title or "", value)
        try:
            decision_id = _leave_card(person.id, session, action, item_id, value, title or "", card)
        except Exception:
            logger.exception("do_in_app: couldn't leave the card for %s", action.name)
            return _error("Couldn't leave the card just now. Try again in a moment.")
        _audit("app_action_proposed", f"Left a card: {card[:120]}", {**details, "decision_id": decision_id})
        return json.dumps({
            "status": "waiting_for_approval",
            "decision_id": decision_id,
            "card": card,
            "note": (
                "Nothing has happened yet. A card is under this reply in the web app for "
                "them to confirm. Tell them it is waiting for their OK."
            ),
        })

    try:
        with from_chat():
            did = await action.run(request, item_id, value)
    except Refused as exc:
        return _error(str(exc))
    except HTTPException as exc:
        _audit("app_action_refused", f"{action.name} refused", {**details, "status": exc.status_code})
        return _error(refusal_text(exc))
    except Exception:
        logger.exception("do_in_app: %s failed", action.name)
        return _error("That didn't go through. Try again in a moment, or do it on the screen.")
    _audit("app_action_done", did[:200], details)
    return json.dumps({"status": "done", "did": did})


async def handle_open_in_app(tool_input: dict[str, Any]) -> str:
    screen = CATALOG.screens.get(str(tool_input.get("screen") or ""))
    if screen is None:
        return _error(f"screen must be one of {', '.join(sorted(CATALOG.screens))}")
    path, label = screen
    return json.dumps({"status": "link", "link": path, "screen": label})


# --------------------------------------------------------------------------- #
# The card: stored, then carried out on the speaker's tap
# --------------------------------------------------------------------------- #


def _leave_card(
    person_id: int, session: Any, action: Action, item_id: str, value: str, title: str, card: str,
) -> int:
    from openexecutive.memory.decision_ledger import create_decision_instance

    return create_decision_instance(
        decision_class=DECISION_CLASS,
        department="",
        originating_session_id=getattr(session, "session_id", None),
        proposed_payload={
            "action": action.name, "id": item_id, "value": value, "title": title, "card": card,
        },
        idempotency_key=None,
        gate_mode="propose",
        approver_person_id=person_id,
        confidence=None,
    )


def card_stale(created_at: str, now: datetime | None = None) -> bool:
    try:
        made = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - made > CARD_LIFETIME


async def carry_out(payload: dict[str, Any], request: Any) -> str:
    """Do a card's action with the tapper's own request. Raises Refused, or
    the route's HTTPException."""
    action = CATALOG.actions.get(str(payload.get("action") or ""))
    if action is None:
        raise Refused("That isn't something this app can do any more.")
    return await action.run(request, str(payload.get("id") or ""), str(payload.get("value") or ""))


APP_TOOLS: list[dict[str, Any]] = [DO_IN_APP_TOOL, FIND_IN_APP_TOOL, OPEN_IN_APP_TOOL]
APP_TOOL_HANDLERS: dict[str, Any] = {
    FIND_IN_APP: handle_find_in_app,
    DO_IN_APP: handle_do_in_app,
    OPEN_IN_APP: handle_open_in_app,
}

_rebuild()
