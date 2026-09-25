"""Anthropic tool definitions + handlers for managing the People roster.

These tools let the Executive add, update, archive, list, and assign
department heads directly from a chat turn — no UI round-trip required.

They sit alongside `create_alert`, the schedule/send tools, and
`consult_specialist` in the Executive's main tool loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from types import SimpleNamespace
from typing import Any, ParamSpec

from openexecutive.memory.honcho_client import directional_chat

logger = logging.getLogger(__name__)


_VALID_PREFERRED_CHANNELS = {"email", "slack", "telegram", "discord", "any"}
_VALID_KINDS = ("team", "contact")


LIST_PEOPLE_TOOL: dict[str, Any] = {
    "name": "list_people",
    "description": (
        "List people on the roster, each with its `kind`. Use this to resolve "
        "a name to a person_id before calling upsert_person, archive_person, "
        "set_department_head, create_calendar_event or message_person. The "
        "principal's contacts (`kind` \"contact\") are private to the "
        "principal: they are listed only when the principal asks directly. "
        "Returns a compact JSON list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "If true, also include archived (soft-deleted) people. Default false.",
            },
        },
    },
}


UPSERT_PERSON_TOOL: dict[str, Any] = {
    "name": "upsert_person",
    "description": (
        "Create or update a person in the People roster. Pass `person_id` to "
        "update an existing row; omit it to create a new one. When the user "
        "asks you to add someone, fill in everything you know — name, role, "
        "email, department slugs, authority scopes — and call this directly. "
        "Do not refuse and do not redirect to the UI. Call list_people first "
        "if you need to look up an existing person_id by name. Set `kind` to "
        "\"contact\" for someone outside the team (a client, contractor, "
        "advisor): you can email or invite a contact only when the principal "
        "asks you to directly, and a contact cannot sign in, message you, or "
        "approve anything. \"team\" is for people who work with the principal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "full_name": {"type": "string", "description": "Person's full name."},
            "kind": {
                "type": "string",
                "enum": ["team", "contact"],
                "description": (
                    "\"team\" or \"contact\". On a new person it defaults to "
                    "\"team\" — or to \"contact\" when the principal uses Open "
                    "Executive just for themselves. On an update, omit it to "
                    "keep the current kind."
                ),
            },
            "person_id": {
                "type": "integer",
                "description": "Existing person id to UPDATE. Omit to create a new row.",
            },
            "role": {
                "type": "string",
                "description": (
                    "Role/title (e.g. 'Head of Marketing', 'Bookkeeper'); for a "
                    "contact, their role and company (e.g. 'CFO, Acme Corp')."
                ),
            },
            "department_slugs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Department slugs this person belongs to (e.g. ['marketing']). "
                    "Slugs are lowercase and match the department registry."
                ),
            },
            "email": {"type": "string"},
            "slack_user_id": {"type": "string"},
            "telegram_chat_id": {"type": "string"},
            "discord_user_id": {"type": "string"},
            "preferred_channel": {
                "type": "string",
                "enum": ["email", "slack", "telegram", "discord", "any"],
                "description": "Preferred channel for routing messages to this person.",
            },
            "authority_scopes": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "spend_lt_2k",
                        "spend_lt_10k",
                        "spend_gt_10k",
                        "hiring_signoff",
                        "vendor_onboarding",
                        "customer_credit",
                        "legal_sign",
                        "board_comms",
                        "wildcard",
                    ],
                },
                "description": (
                    "Approval scopes this person holds. Pass the full intended "
                    "list — this REPLACES the existing scope set on update."
                ),
            },
            "response_sla_hours": {
                "type": "integer",
                "description": "Expected response time in hours. Default 24.",
            },
            "on_leave_until": {
                "type": "string",
                "description": "ISO date (YYYY-MM-DD) the person is on leave until, or omit if available.",
            },
            "reports_to_person_id": {
                "type": "integer",
                "description": "Person id this person reports to, if any.",
            },
        },
        "required": ["full_name"],
    },
}


ARCHIVE_PERSON_TOOL: dict[str, Any] = {
    "name": "archive_person",
    "description": (
        "Soft-delete a person from the roster by id. Reversible by an admin "
        "via direct DB edit; the person stops appearing in normal listings "
        "and approval lookups. Use when someone leaves the company."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {"type": "integer", "description": "Id of the person to archive."},
        },
        "required": ["person_id"],
    },
}


SET_DEPARTMENT_HEAD_TOOL: dict[str, Any] = {
    "name": "set_department_head",
    "description": (
        "Assign a person as the head of a department (or clear it by passing "
        "person_id=null). The person must already exist in the roster — call "
        "upsert_person first if they don't."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department_slug": {
                "type": "string",
                "description": "Department slug, e.g. 'marketing'.",
            },
            "person_id": {
                "type": ["integer", "null"],
                "description": "Person id to assign as head, or null to clear the head.",
            },
        },
        "required": ["department_slug"],
    },
}


ASK_ABOUT_PERSON_TOOL: dict[str, Any] = {
    "name": "ask_about_person",
    "description": (
        "Ask Honcho's per-person memory about a specific Person — their preferences, "
        "ongoing concerns, factual claims they've made, or what one peer thinks of "
        "another. Synthesizes across every Honcho session that Person has appeared "
        "in (Slack, Discord, email, etc.). Use when:\n"
        " - You need facts about a Person that go beyond the current turn's "
        "`<peer_memory>` block (which is already injected for free at turn start).\n"
        " - You're advising peer A about peer B and want A's perspective specifically — "
        "set `target_person_id=B` to query A's representation of B "
        "(e.g. person_id=alice, target_person_id=bob, "
        "question=\"What concerns has Alice raised about Bob?\").\n"
        "Do NOT call when the inline `<peer_memory>` block already answers the "
        "question — that block costs nothing extra; this tool spends an extra "
        "Honcho LLM call.\n"
        "Returns the synthesized answer, or an empty string if Honcho is disabled / no data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "integer",
                "description": "Person.id of the peer whose memory you're querying.",
            },
            "question": {
                "type": "string",
                "description": "Natural-language question about that peer.",
            },
            "target_person_id": {
                "type": "integer",
                "description": (
                    "Optional Person.id of a second peer. When provided, the answer "
                    "comes from `person_id`'s representation of `target_person_id` — "
                    "i.e. what does this person know/think about that person."
                ),
            },
            "reasoning_level": {
                "type": "string",
                "enum": ["minimal", "low", "medium", "high", "max"],
                "description": (
                    "Trade latency for synthesis depth. Default `medium` is right for "
                    "most tool calls (the model already decided to spend the latency by "
                    "calling). Use `high`/`max` for deep-research questions; `low` if you "
                    "just need a quick refresher and care about latency."
                ),
            },
        },
        "required": ["person_id", "question"],
    },
}


PEOPLE_TOOLS: list[dict[str, Any]] = [
    LIST_PEOPLE_TOOL,
    UPSERT_PERSON_TOOL,
    ARCHIVE_PERSON_TOOL,
    SET_DEPARTMENT_HEAD_TOOL,
    ASK_ABOUT_PERSON_TOOL,
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _audit(tool: str, kind: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": tool, "kind": kind, "ok": ok, **details},
    )


# Surfaces that verify who sent every message: the web chat (the signed-in
# Google account, stamped by the UI proxy), and Slack / Discord (the platform's
# own user id, over an authenticated connection). Email (an unauthenticated
# From header), Google Chat (no sender identity), the CLI, the MCP server and
# unattended runs (scheduler, workflows, alert review) don't. Telegram only
# sometimes — see `_is_verified_speaker_surface`.
_VERIFIED_SPEAKER_CHANNELS = frozenset({"slack", "discord"})


def _is_verified_speaker_surface(session: Any) -> bool:
    if bool(getattr(session, "from_web_chat", False)):
        return True
    channel = str(getattr(session, "origin_channel", "") or "")
    if channel in _VERIFIED_SPEAKER_CHANNELS:
        return True
    if channel != "telegram":
        return False
    # Telegram identifies the chat, not the sender, and its webhook is exempt
    # from the shared secret: an update is only proven to come from Telegram
    # when TELEGRAM_WEBHOOK_SECRET is set to a value Telegram can send
    # (otherwise anyone can post one naming the principal's chat id). A group
    # (negative id) is everyone in it.
    from openexecutive.config import get_settings

    chat_ref = str(getattr(session, "origin_channel_ref", "") or "")
    return get_settings().telegram_webhook_secret_valid and chat_ref.isdigit()


# Set only around an action the principal took themselves outside a chat turn
# (approving a proposed meeting in the web app): those run with no session, so
# `is_principal_on_verified_surface` alone would always say no.
_contact_egress_granted: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "contact_egress_granted", default=False
)


@contextlib.contextmanager
def grant_contact_egress() -> Iterator[None]:
    """Let the egress gates reach contacts for the duration of the block.

    Only for code that has itself established the principal is acting (e.g.
    the decisions approve route, after resolving the caller). Save/restore
    rather than ``Token.reset`` for the same reason as ``set_session``.
    """
    prior = _contact_egress_granted.get()
    _contact_egress_granted.set(True)
    try:
        yield
    finally:
        _contact_egress_granted.set(prior)


def contacts_reachable_now() -> bool:
    """Whether contacts exist for this turn at all — may be listed, named,
    emailed, invited or messaged.

    Contacts are private to the principal: only a turn the principal started
    on a verified surface (the web app, their own Slack or Discord, a verified
    private Telegram chat), or code inside ``grant_contact_egress``, sees them.
    Everywhere else — an inbound email (a From header proves nothing), a
    teammate's turn, an unattended run (scheduler, workflows, alert review) —
    a contact is indistinguishable from someone who is not on the roster, so
    no refusal, listing or error message can reveal that one exists.
    """
    if _contact_egress_granted.get():
        return True
    from openexecutive.orchestrator.schedule_tools import current_session

    return is_principal_on_verified_surface(current_session.get())


def turn_is_private_to_principal() -> bool:
    """Whether the current turn is about something private to the principal
    (mail from one of their contacts, mail they forwarded — set by the email
    poller). Such a turn may reach the principal and nobody else."""
    from openexecutive.orchestrator.schedule_tools import current_session

    return getattr(current_session.get(), "private_to_principal", False) is True


PRIVATE_TURN_REFUSAL = (
    "This conversation is private to the principal, so I can only send it to "
    "the principal. Tell the principal instead and let them decide who else "
    "should know."
)


def audit_row_private_to_principal(*payloads: Any) -> bool:
    """Whether an audit row written now is the principal's alone to read
    (``api.routes.audit`` leaves it out for anyone else). ``payloads`` are the
    row's summary, details and full payload.

    True for every row a private turn writes (``turn_is_private_to_principal``)
    and, while contacts are reachable (``contacts_reachable_now``: the
    principal's own verified turn, or ``grant_contact_egress``), for a row
    that names one of their contacts — an email or invite to one, a DM, a
    question about one. On any other turn a contact is a stranger, so a row
    naming one stays as it is: hiding it would tell whoever wrote it that
    the address is a contact.

    Some rows are written before their turn binds its session, so the audit
    scopes count as well (``audit.context``): inside ``private_rows`` every
    row is private (the email poller, for the whole handling of a private
    mail), and inside ``principal_turn_rows`` a row naming a contact is, as
    on the principal's own turn (a chat adapter, once it knows the principal
    sent the message — see ``audit_rows_on_senders_turn``).
    """
    from openexecutive.audit.context import rows_on_principal_turn, rows_private

    if turn_is_private_to_principal() or rows_private():
        return True
    if not (contacts_reachable_now() or rows_on_principal_turn()):
        return False
    return _names_a_contact(payloads)


def sent_by_principal(channel: str, channel_ref: str, sender: Any) -> bool:
    """Whether a message a chat adapter received on ``channel`` from
    ``sender`` (the Person it resolved, or None) starts the principal's own
    verified turn — what ``is_principal_on_verified_surface`` answers once the
    turn's session is bound. ``channel_ref`` is the id that session carries as
    its ``origin_channel_ref`` (Telegram's chat id decides whether it is
    verified at all)."""
    if sender is None or getattr(sender, "is_principal", False) is not True:
        return False
    if getattr(sender, "archived", False) is True:
        return False
    surface = SimpleNamespace(
        from_web_chat=False, origin_channel=channel, origin_channel_ref=channel_ref
    )
    return _is_verified_speaker_surface(surface)


_P = ParamSpec("_P")


def audit_rows_on_senders_turn(
    channel: str,
    sender_ref: Callable[[dict[str, Any]], str],
    find_sender: Callable[[str], Any],
) -> Callable[[Callable[_P, Awaitable[None]]], Callable[_P, Awaitable[None]]]:
    """Decorate an inbound chat handler so the audit rows it writes before its
    turn binds its session (the inbound row, the knowledge retrieval, alert
    triage) follow the rule they follow once it is: when the principal sent
    the message on a verified surface, a row that names one of their contacts
    is private to the principal (``audit.context.principal_turn_rows``).

    ``sender_ref`` picks the sender's user or chat id out of the handler's
    arguments (by name), and ``find_sender`` resolves it to a Person, off the
    event loop. A lookup that fails counts as not the principal, as
    ``is_principal_on_verified_surface`` answers on an error, so rows keep
    the ordinary rule. This decides audit visibility only: contacts stay
    unreachable until the session is bound.
    """

    def decorate(handler: Callable[_P, Awaitable[None]]) -> Callable[_P, Awaitable[None]]:
        signature = inspect.signature(handler)

        @functools.wraps(handler)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
            from openexecutive.audit.context import principal_turn_rows

            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                ref = sender_ref(dict(bound.arguments))
                sender = await asyncio.to_thread(find_sender, ref) if ref else None
                principal = sent_by_principal(channel, ref, sender)
            except Exception as exc:
                logger.warning(
                    "people_tools: could not tell who sent an inbound %s message (%s) — "
                    "its audit rows follow the ordinary rule", channel, type(exc).__name__,
                    exc_info=True,
                )
                principal = False
            with principal_turn_rows(principal):
                await handler(*args, **kwargs)

        return wrapper

    return decorate


# Keys whose value is a person or a DM recipient: person_id,
# assigned_to_person_id, attendee_person_ids, user_id, discord_user_id,
# chat_id, channel_ref. Matched against a contact's person id and chat ids.
_PERSON_REF_KEY = re.compile(r"(?:^|_)(?:person_ids?|user_id|chat_id|channel_ref)$")
# JSON inside a string (a tool result) is walked too, up to this size.
_MAX_JSON_WALK_CHARS = 100_000


def _scalars(value: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Every (key, leaf value) in ``value``: dicts and lists are walked, and a
    string that holds a JSON object or list is parsed and walked as well."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _scalars(v, str(k))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _scalars(v, key)
    else:
        yield key, value
        if (
            isinstance(value, str)
            and value[:1] in ("{", "[")
            and len(value) <= _MAX_JSON_WALK_CHARS
        ):
            try:
                parsed = json.loads(value)
            except ValueError:
                return
            if isinstance(parsed, (dict, list)):
                yield from _scalars(parsed, key)


def _names_a_contact(payloads: tuple[Any, ...]) -> bool:
    """Whether ``payloads`` name one of the principal's contacts: their email
    address or full name (two words or more) anywhere in the text, or their
    person id or chat id in a person / recipient field. Fails closed."""
    try:
        from openexecutive.people.registry import list_people

        contacts = [p for p in list_people(include_contacts=True) if p.kind != "team"]
    except Exception:
        logger.exception("people_tools: contact lookup for an audit row failed — row kept private")
        return True
    if not contacts:
        return False
    emails = {e for p in contacts if (e := (p.email or "").strip().lower())}
    names = {
        n for p in contacts
        if len((n := " ".join((p.full_name or "").split()).lower()).split()) >= 2
    }
    refs = {str(p.id) for p in contacts if p.id is not None} | {
        r for p in contacts
        for v in (p.slack_user_id, p.discord_user_id, p.telegram_chat_id)
        if (r := str(v or "").strip())
    }
    # Longest first, so one address is never matched as part of another; the
    # guards stop "an@x.co" matching inside "dan@x.co".
    terms = sorted(
        [rf"(?<![\w.+-]){re.escape(e)}(?![\w-])" for e in emails]
        + [rf"(?<!\w){re.escape(n)}(?!\w)" for n in names],
        key=len, reverse=True,
    )
    pattern = re.compile("|".join(terms)) if terms else None
    for key, value in _scalars(payloads):
        if value is None or isinstance(value, bool):
            continue
        if (
            pattern is not None
            and isinstance(value, str)
            and pattern.search(" ".join(value.split()).lower())
        ):
            return True
        if refs and _PERSON_REF_KEY.search(key) and str(value).strip() in refs:
            return True
    return False


def _roster_refusal_reason(session: Any) -> str | None:
    """None when this turn may change the roster, else what to tell the asker."""
    from openexecutive.people.store import is_principal_or_self

    from_web = bool(getattr(session, "from_web_chat", False))
    if session is None or not _is_verified_speaker_surface(session):
        return (
            "Only the company's owner can change the People list, and this request "
            "did not come from somewhere I can confirm it is them. Tell whoever "
            "asked that the owner needs to make this change, from the web app or "
            "their own Slack or Discord."
        )
    caller = getattr(session, "caller_person_id", None)
    try:
        if is_principal_or_self(caller, None):
            return None
    except Exception:
        logger.exception("people_tools: principal lookup failed — refusing roster write")
    if from_web and caller is None:
        return (
            "Only the company's owner can change the People list, and this signed-in "
            "email is not on anyone's People entry, so I cannot confirm it is the "
            "owner — the People page refuses them for the same reason. If it is the "
            "owner, they should sign in with the email on their own People entry, or, "
            "if that entry has none yet, run the setup interview again (it adds the "
            "email they signed in with) and then ask again."
        )
    return (
        "Only the company's owner can add, change or remove people or set "
        "department heads, and this request came from someone else. Tell them "
        "the owner needs to make this change."
    )


def is_principal_on_verified_surface(session: Any) -> bool:
    """Whether this turn was started by the principal on a surface that
    verified it is them — the rule for roster writes above, as a yes/no for
    other principal-only actions (e.g. solo mode's meeting auto-booking, and
    whether the principal's private contacts exist for this turn — see
    ``contacts_reachable_now``).
    Fails closed: no session, an unverified surface, someone else, or an
    unreadable roster all answer False."""
    try:
        return _roster_refusal_reason(session) is None
    except Exception:
        logger.exception("people_tools: verified-surface check failed — answering no")
        return False


def _refuse_unless_owner(tool: str) -> str | None:
    """The refusal tool result when this turn may not change the roster, else None.

    A People row decides who may sign in to the web app, who the Executive may
    email, and who approves what — and these tools are offered on every turn,
    including one an inbound email, a Google Chat message or a teammate
    started. So only the principal may change the roster from chat, and only on
    a surface that verified it is them. The People page (behind the web
    sign-in) is unaffected.
    """
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    reason = _roster_refusal_reason(session)
    if reason is None:
        return None
    _audit(
        tool, "write", False,
        f"{tool} refused: not the principal on a verified surface",
        {
            "refused": True,
            "caller_person_id": getattr(session, "caller_person_id", None),
            "origin_channel": getattr(session, "origin_channel", "") or None,
            "from_web_chat": bool(getattr(session, "from_web_chat", False)),
        },
    )
    return json.dumps({"status": "refused", "detail": reason})


async def handle_list_people(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import store as people_store

    include_archived = bool(tool_input.get("include_archived", False))
    # Contacts only on the principal's own verified turn (the model needs a
    # contact's person_id to invite or message them then). Anyone else gets the
    # team exactly as if no contact existed — same rows, same count.
    include_contacts = contacts_reachable_now()
    try:
        people = people_store.list_people(
            include_archived=include_archived, include_contacts=include_contacts
        )
    except Exception as exc:
        logger.exception("list_people: failed")
        _audit("list_people", "read", False, f"list_people failed: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": str(exc)})

    out = [
        {
            "person_id": p.id,
            "full_name": p.full_name,
            "role": p.role,
            "is_principal": p.is_principal,
            "kind": p.kind,
            "email": p.email,
            "preferred_channel": p.preferred_channel,
            "department_slugs": p.department_slugs,
            "authority_scope": [s.value for s in p.authority_scope],
            "archived": p.archived,
        }
        for p in people
    ]
    # The audit log is readable by every signed-in user: count the team only,
    # so the principal's turn logs exactly what anyone else's would.
    team_count = sum(1 for p in people if p.kind == "team")
    _audit(
        "list_people", "read", True,
        f"list_people returned {team_count}",
        {"count": team_count, "include_archived": include_archived},
    )
    return json.dumps({"people": out, "count": len(out)})


async def handle_upsert_person(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    from openexecutive.people.models import AuthorityScope

    def _bad(err: str) -> str:
        _audit("upsert_person", "write", False, f"upsert_person bad input: {err}", {"error": err[:300]})
        return json.dumps({"error": err})

    refused = _refuse_unless_owner("upsert_person")
    if refused is not None:
        return refused

    try:
        full_name = str(tool_input["full_name"]).strip()
        if not full_name:
            raise ValueError("full_name is required and cannot be empty")
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(f"bad arguments: {exc}")

    # The principal flag controls fallback authority and can only be flipped
    # via the HTTP API behind BACKEND_SHARED_SECRET (see people/models.py).
    # The Executive must not be able to promote someone via a chat turn —
    # social-engineering would otherwise bypass the API gate entirely.
    if bool(tool_input.get("is_principal", False)):
        return _bad(
            "is_principal can only be set via the authenticated /people API, not via chat tools"
        )

    person_id = tool_input.get("person_id")
    existing = None
    if person_id is not None:
        try:
            person_id = int(person_id)
        except (TypeError, ValueError):
            return _bad("person_id must be an integer")
        existing = people_store.get_person(person_id)
        if existing is None:
            return _bad(f"person {person_id} not found")
        # Updating a row that happens to be the existing principal must also
        # not be a stealth path to flip the flag off; we block that too.
        if existing.is_principal and not bool(tool_input.get("is_principal", existing.is_principal)):
            return _bad(
                "is_principal can only be changed via the authenticated /people API"
            )

    # kind: explicit wins; an update that omits it keeps the row's kind (the
    # store preserves it); a new person defaults to team — or to contact when
    # the principal uses Open Executive just for themselves, where anyone they
    # add is almost always someone outside (a client, a contractor).
    kind_raw = tool_input.get("kind")
    kind: str | None
    if kind_raw is not None:
        kind = str(kind_raw).strip().lower()
        if kind not in _VALID_KINDS:
            return _bad(f"kind must be one of {list(_VALID_KINDS)}")
    elif existing is None:
        from openexecutive.memory.workspace_settings import effective_workspace_mode
        from openexecutive.orchestrator.schedule_tools import current_session

        kind = "contact" if effective_workspace_mode(current_session.get()) == "solo" else "team"
    else:
        kind = None
    if existing is not None and existing.is_principal and kind == "contact":
        return _bad("the principal is always on the team and cannot be made a contact")

    preferred_channel = tool_input.get("preferred_channel", "any")
    if preferred_channel not in _VALID_PREFERRED_CHANNELS:
        return _bad(
            f"preferred_channel must be one of {sorted(_VALID_PREFERRED_CHANNELS)}"
        )

    on_leave_until_raw = tool_input.get("on_leave_until")
    on_leave_until: date | None = None
    if on_leave_until_raw:
        try:
            on_leave_until = date.fromisoformat(str(on_leave_until_raw))
        except ValueError as exc:
            return _bad(f"on_leave_until must be YYYY-MM-DD: {exc}")

    authority_scopes_raw = tool_input.get("authority_scopes")
    authority_scopes: list[AuthorityScope] | None = None
    if authority_scopes_raw is not None:
        try:
            authority_scopes = [AuthorityScope(str(s)) for s in authority_scopes_raw]
        except ValueError as exc:
            return _bad(f"invalid authority scope: {exc}")

    department_slugs = tool_input.get("department_slugs")
    if department_slugs is not None:
        department_slugs = [str(s).strip() for s in department_slugs if str(s).strip()]

    # is_principal is preserved on update (we already rejected attempts to flip
    # it above); on insert it is always False.
    preserved_principal = (
        people_store.get_person(person_id).is_principal  # type: ignore[union-attr]
        if person_id is not None
        else False
    )
    kwargs: dict[str, Any] = {
        "full_name": full_name,
        "role": str(tool_input.get("role", "") or ""),
        "is_principal": preserved_principal,
        "department_slugs": department_slugs,
        "email": tool_input.get("email"),
        "slack_user_id": tool_input.get("slack_user_id"),
        "telegram_chat_id": tool_input.get("telegram_chat_id"),
        "discord_user_id": tool_input.get("discord_user_id"),
        "preferred_channel": preferred_channel,
        "response_sla_hours": int(tool_input.get("response_sla_hours", 24)),
        "on_leave_until": on_leave_until,
        "reports_to_person_id": tool_input.get("reports_to_person_id"),
        "kind": kind,
    }
    if person_id is not None:
        kwargs["person_id"] = person_id

    action = "updated" if person_id is not None else "created"
    try:
        new_id = people_store.upsert_person(**kwargs)
        if authority_scopes is not None:
            people_store.set_authority_scope(new_id, authority_scopes)
    except Exception as exc:
        logger.exception("upsert_person: failed")
        # Always invalidate — a partial write (e.g. row inserted but scopes
        # failed) must not leave the registry serving stale data.
        people_registry.invalidate()
        # No name or kind in the audit row (readable by everyone): a contact
        # is private to the principal, and a team row must look the same.
        _audit(
            "upsert_person", "write", False,
            f"upsert_person FAILED: {type(exc).__name__}",
            {"error": type(exc).__name__, "action": action, "person_id": person_id},
        )
        return json.dumps({"error": str(exc)})
    people_registry.invalidate()

    # Name-free and kind-free for every person (the audit log is readable by
    # every signed-in user and a contact is private to the principal): the
    # id and the action are what an audit reader needs.
    _audit(
        "upsert_person", "write", True,
        f"upsert_person {action} id={new_id}",
        {"person_id": new_id, "action": action},
    )
    stored = people_store.get_person(new_id)
    return json.dumps({
        "status": "ok",
        "action": action,
        "person_id": new_id,
        "full_name": full_name,
        "kind": stored.kind if stored is not None else kind,
    })


async def handle_archive_person(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    refused = _refuse_unless_owner("archive_person")
    if refused is not None:
        return refused

    try:
        person_id = int(tool_input["person_id"])
    except (KeyError, TypeError, ValueError) as exc:
        _audit("archive_person", "write", False, f"archive_person bad input: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": f"bad arguments: {exc}"})

    try:
        archived = people_store.archive_person(person_id)
        if archived:
            people_registry.invalidate()
    except Exception as exc:
        logger.exception("archive_person: failed")
        _audit("archive_person", "write", False, f"archive_person FAILED id={person_id}: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": str(exc)})

    if not archived:
        _audit(
            "archive_person", "write", False,
            f"archive_person id={person_id} not found or already archived",
            {"person_id": person_id, "archived": False},
        )
        return json.dumps({
            "status": "not_found",
            "person_id": person_id,
            "detail": "no active person with that id",
        })
    _audit(
        "archive_person", "write", True,
        f"archive_person id={person_id}",
        {"person_id": person_id, "archived": True},
    )
    return json.dumps({"status": "archived", "person_id": person_id})


async def handle_set_department_head(tool_input: dict[str, Any]) -> str:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.departments.head_persona import ensure_head_persona_override
    from openexecutive.people import store as people_store

    refused = _refuse_unless_owner("set_department_head")
    if refused is not None:
        return refused

    try:
        department_slug = str(tool_input["department_slug"]).strip()
        if not department_slug:
            raise ValueError("department_slug cannot be empty")
    except (KeyError, TypeError, ValueError) as exc:
        _audit("set_department_head", "write", False, f"set_department_head bad input: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": f"bad arguments: {exc}"})

    raw_pid = tool_input.get("person_id")
    person_id: int | None
    if raw_pid is None:
        person_id = None
    else:
        try:
            person_id = int(raw_pid)
        except (TypeError, ValueError):
            _audit("set_department_head", "write", False, "person_id not an integer", {"department_slug": department_slug})
            return json.dumps({"error": "person_id must be an integer or null"})

    head = people_store.get_person(person_id) if person_id is not None else None
    # Only a team member can head a department. A contact is answered exactly
    # like an unknown id — result and audit row alike — since the audit log is
    # readable by everyone and contacts are private to the principal.
    if person_id is not None and (head is None or head.kind != "team"):
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: person {person_id} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": (
            f"person {person_id} not found on the team — only a team member can "
            "head a department"
        )})

    # Pre-check the department exists so we don't create a head-persona
    # override row for a slug that nobody can reach.
    if dept_store.get_department(department_slug) is None:
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: dept {department_slug!r} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": f"department {department_slug!r} not found"})

    # Create the head persona override row first when assigning a head.
    # `ensure_head_persona_override` is idempotent and only creates a
    # placeholder if none exists, so doing this before the dept UPDATE means
    # we never end up with `dept.head_person_id` set but no matching override
    # row. When clearing the head (person_id is None), there is no override
    # to create.
    try:
        if person_id is not None:
            ensure_head_persona_override(department_slug, person_id)
        ok = dept_store.update_department(department_slug, head_person_id=person_id)
        if ok:
            dept_registry.invalidate()
    except Exception as exc:
        logger.exception("set_department_head: failed")
        _audit(
            "set_department_head", "write", False,
            f"set_department_head FAILED: {exc}",
            {"department_slug": department_slug, "person_id": person_id, "error": str(exc)[:300]},
        )
        return json.dumps({"error": str(exc)})

    if not ok:
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: dept {department_slug!r} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": f"department {department_slug!r} not found"})

    _audit(
        "set_department_head", "write", True,
        f"set_department_head dept={department_slug} person={person_id}",
        {"department_slug": department_slug, "person_id": person_id},
    )
    return json.dumps({
        "status": "ok",
        "department_slug": department_slug,
        "head_person_id": person_id,
    })


def _is_principal_person(person_id: int) -> bool:
    """Whether ``person_id`` is a principal row. Fails closed (True)."""
    try:
        from openexecutive.people.store import get_person

        person = get_person(person_id)
    except Exception:
        logger.exception("ask_about_person: principal lookup failed — answering nothing")
        return True
    return bool(person is not None and person.is_principal)


async def handle_ask_about_person(input: dict[str, Any]) -> str:
    """Query Honcho's per-person memory via the directional_chat wrapper.

    Lives here (vs. honcho_client) because it's the Anthropic tool
    *handler*: parameter coercion, error JSON shape, and clamping the
    reasoning_level enum to the one Honcho accepts. The actual memory
    call is delegated to :func:`honcho_client.directional_chat`.
    """
    try:
        person_id = int(input["person_id"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"error": "missing or invalid required field: person_id"})
    question = input.get("question", "")
    if not question:
        return json.dumps({"error": "missing required field: question"})
    target_raw = input.get("target_person_id")
    target_person_id: int | None
    if target_raw is None:
        target_person_id = None
    else:
        try:
            target_person_id = int(target_raw)
        except (TypeError, ValueError):
            return json.dumps({"error": "invalid target_person_id"})
    reasoning_level = input.get("reasoning_level", "medium")
    if reasoning_level not in ("minimal", "low", "medium", "high", "max"):
        reasoning_level = "medium"
    # The principal's own peer memory is drawn from all their conversations,
    # including about their contacts, which are private to them: it answers
    # only on the principal's own verified turn. Anyone else gets exactly the
    # "no data" answer (``target_person_id`` = the principal is someone
    # else's view of them — their memory, not the principal's).
    if _is_principal_person(person_id) and not contacts_reachable_now():
        return json.dumps(
            {"person_id": person_id, "target_person_id": target_person_id,
             "answer": "", "found": False},
            ensure_ascii=False,
        )
    answer = await directional_chat(
        person_id,
        question,
        target_person_id=target_person_id,
        reasoning_level=reasoning_level,
    )
    # Empty answer is a normal outcome (Honcho disabled, no data, error
    # swallowed by the wrapper) — surface `found: false` rather than a
    # raw "" so the model knows it's an empty result, not a tool failure.
    return json.dumps(
        {
            "person_id": person_id,
            "target_person_id": target_person_id,
            "answer": answer,
            "found": bool(answer),
        },
        ensure_ascii=False,
    )


PEOPLE_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_people": handle_list_people,
    "upsert_person": handle_upsert_person,
    "archive_person": handle_archive_person,
    "set_department_head": handle_set_department_head,
    "ask_about_person": handle_ask_about_person,
}
