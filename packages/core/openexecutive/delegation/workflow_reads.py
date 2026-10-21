"""Workflows that read their creator's own mailbox and calendar (Act as me):
``oe__search_my_email``, ``oe__read_my_email``, ``oe__my_email_awaiting_reply``
and ``oe__my_calendar``, the chat's own reads (``orchestrator.mail_read_tools``)
run as the workflow's creator instead of the chat's speaker.

What a mail read lets into a run is other people's words, and what a run
produces can reach other people, so it is fenced in code, as chat is:

- **Whose mailbox.** Always the workflow's creator (``dynamic_store.get_owner``),
  and only on a run that is theirs: started by them by hand, or by its
  schedule (the scheduler then keeps the run as theirs, ``owns_runs``).
  Never on a run someone else started, one the Executive's own unattended
  passes started, one an inbound message started, or when it was started by
  another workflow, whose later steps would see what the mail said.
- **Only when they turned it on.** The creator may use Act as me, turned it
  on, and their mailbox is connected; ``my_calendar`` also needs Read my
  calendar on.
- **Private.** A run of a workflow that reads is its creator's alone: its
  history, its report (a scheduled one is delivered to them, never to
  someone else) and every audit row the read writes. A run started from chat
  marks the conversation as one that read their mail.
- **The run is locked down.** As a chat turn that read the mail is for the
  rest of the turn (``delegation.lockdown``), a workflow that reads is for
  all of its steps: it may only list tools ``MAIL_TOUCHED_ALLOWED_TOOLS``
  lets run after a read, the email-as-me tools and these reads; no gateway
  tool, web search, script or workflow run; no approval gate asking anyone
  but its creator. Checked when it is saved (``definition_refusal``) and
  again on every run (``run_rules``).
- **Saved by its creator.** Like one that sends, a workflow that reads can
  only be saved or switched on by its creator, verified
  (``workflow_email.save_refusal``).
"""
from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any

logger = logging.getLogger(__name__)

# Workflow tool name -> the chat tool it runs as the creator.
_CHAT_NAMES: dict[str, str] = {
    "oe__search_my_email": "search_my_email",
    "oe__read_my_email": "read_my_email",
    "oe__my_email_awaiting_reply": "my_email_awaiting_reply",
    "oe__my_calendar": "my_calendar",
}
TOOLS = frozenset(_CHAT_NAMES)

_FALL_BACK = " Say in your report that it couldn't be read, and carry on without it."


def reads(defn: Any) -> bool:
    """Whether a workflow definition has a step that reads its creator's mail."""
    return any(TOOLS & set(getattr(step, "tools", None) or []) for step in getattr(defn, "steps", []))


def _allowed_alongside(tool: str) -> bool:
    """Whether a step of a workflow that reads may list ``tool``."""
    from openexecutive.delegation import workflow_email
    from openexecutive.delegation.lockdown import MAIL_TOUCHED_ALLOWED_TOOLS
    from openexecutive.workflows.chat_tools import ACCESS, LEGACY_NAMES, PREFIX, USE

    if tool in TOOLS or tool in workflow_email.TOOLS:
        return True
    if not tool.startswith(PREFIX):
        return False  # gateway, extension or web search: a door outside
    name = LEGACY_NAMES.get(tool, tool.removeprefix(PREFIX))
    entry = ACCESS.get(name)
    return entry is not None and entry[0] == USE and name in MAIL_TOUCHED_ALLOWED_TOOLS


def definition_refusal(defn: Any, owner_id: int | None) -> str | None:
    """Why ``defn``, created by ``owner_id``, may not read its creator's mail
    as it stands, or None (also when it doesn't read). Plain code: fails closed."""
    if not reads(defn):
        return None
    if owner_id is None:
        return "A workflow that reads someone's email needs a creator on record."
    outside = sorted({
        t for step in getattr(defn, "steps", []) for t in (getattr(step, "tools", None) or [])
        if not _allowed_alongside(t)
    })
    if outside:
        return (
            "A workflow that reads your email or calendar can't also use "
            f"{', '.join(outside[:5])}: once it has read your mail, nothing in it may "
            "open a link, run a script or another workflow, or post to everyone. "
            "Split that part into a workflow of its own."
        )
    for step in getattr(defn, "steps", []):
        person_id = getattr(step, "person_id", None)
        if getattr(step, "kind", None) == "approval_gate" and person_id != owner_id:
            return "A workflow that reads your email can only ask you to approve its steps."
    cadence_to = getattr(defn, "cadence_person_id", None)
    if cadence_to is not None and cadence_to != owner_id:
        return "A workflow that reads your email can only send its report to you."
    return None


def run_rules(workflow_name: str) -> tuple[bool, str | None]:
    """For a step of ``workflow_name`` about to run: whether the workflow reads
    its creator's mail (its steps then get no scripts), and why it may not run
    as it stands, or None. Fails closed."""
    from openexecutive.workflows.dynamic_store import get_definition, get_owner

    try:
        defn = get_definition(workflow_name)
        if defn is None or not reads(defn):
            return False, None
        return True, definition_refusal(defn, get_owner(workflow_name))
    except Exception:
        logger.warning("workflow reads: couldn't read %r", workflow_name, exc_info=True)
        return True, "Couldn't check this workflow's tools just now. Try again later."


def owns_runs(workflow_name: str) -> int | None:
    """The person a run of ``workflow_name`` must belong to (its creator) when
    it reads their mail, or None. Never raises: None on a lookup that fails,
    and the reads then refuse on the run."""
    from openexecutive.workflows.dynamic_store import get_definition, get_owner

    try:
        defn = get_definition(workflow_name)
        return get_owner(workflow_name) if defn is not None and reads(defn) else None
    except Exception:
        logger.warning("workflow reads: couldn't read %r", workflow_name, exc_info=True)
        return None


def run_refusal(creator_id: int, runner_id: int | None, caller: Any) -> str | None:
    """Why ``caller`` (on the roster as ``runner_id``) may not start, from the
    API, a run of a workflow that reads ``creator_id``'s mail, or None: only
    its creator, verified the way a save of it is (``verified.caller_refusal``).
    Never raises; a lookup that fails refuses."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import caller_refusal
    from openexecutive.people.store import get_person

    only_them = "This workflow reads its creator's email, so only they can run it."
    if runner_id != creator_id:
        return only_them
    try:
        creator = get_person(creator_id)
        refused = caller_refusal(caller, normalize_email(creator.email)) if creator is not None else "no"
    except Exception:
        logger.warning("workflow reads: couldn't check who is running it", exc_info=True)
        refused = "error"
    if refused is None:
        return None
    return only_them + " Running it needs signed sign-ins on this server, so nobody else can in their name."


def chat_run_refusal(workflow_name: str) -> str | None:
    """Before chat's ``run_workflow`` starts ``workflow_name``: None when it
    doesn't read its creator's mail. When it does, the person chatting must be
    its creator with Act as me offered on this turn (a verified surface, a
    conversation private to them), and the turn is then marked as one that read
    their mail (the run's report comes back into it): private to them, and
    locked down for the rest of it (``delegation.lockdown``). Else the refusal."""
    from openexecutive.delegation.settings import turn_delegation
    from openexecutive.orchestrator.delegation_tools import _keep_conversation_private, _Writer
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.people.store import get_person
    from openexecutive.workflows.dynamic_store import get_definition

    try:
        defn = get_definition(workflow_name)
        if defn is None or not reads(defn):
            return None
        creator_id = owns_runs(workflow_name)
        pinned = turn_delegation(current_session.get())
        person = get_person(creator_id) if creator_id is not None else None
    except Exception:
        logger.warning("workflow reads: couldn't check %r before running it", workflow_name, exc_info=True)
        return "Couldn't check this workflow just now. Try again in a moment."
    if (
        pinned is None or not pinned.offered or person is None
        or person.id is None or pinned.person_id != person.id
    ):
        return (
            "This workflow reads its creator's email, so only they can run it, from a "
            "conversation that is theirs alone (or from its page)."
        )
    pinned.touched_mail = pinned.read_mail = True
    if not _keep_conversation_private(_Writer(pinned, person, "", None)):
        return "I couldn't keep this conversation private, so I didn't run it. Try again."
    return None


def private_run(workflow_name: str) -> AbstractContextManager[None]:
    """Wrap a whole run of ``workflow_name`` (its steps, its report's
    delivery, a pause's question): when it reads its creator's mail, every
    audit row written in it is theirs alone and no send reaches the shared
    activity rail (``audit.context.rows_for_person``), as on a chat turn that
    read their mail. Otherwise nothing changes."""
    from openexecutive.audit.context import rows_for_person

    owner = owns_runs(workflow_name)
    return rows_for_person(owner) if owner is not None else contextlib.nullcontext()


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def _creator_writer() -> Any:
    """The creator to read as (a ``mail_read_tools`` writer), or the error result."""
    from openexecutive.delegation.gmail import gmail_for, normalize_email
    from openexecutive.delegation.settings import TurnDelegation, can_delegate, is_enabled
    from openexecutive.orchestrator.artifact_records import turn_owner
    from openexecutive.orchestrator.delegation_tools import _Writer
    from openexecutive.people.store import get_person
    from openexecutive.workflows.chat_tools import running, started_by_a_pass
    from openexecutive.workflows.dynamic_store import get_owner

    chain, creator_id = running()
    if creator_id is None or len(chain) != 1:
        return _error(
            "Only a workflow its creator runs, not one another workflow started, can "
            "read their email." + _FALL_BACK
        )
    if started_by_a_pass() or turn_owner() != creator_id or get_owner(chain[0]) != creator_id:
        return _error(
            "Only the person who created this workflow can have it read their email, "
            "on a run that is theirs." + _FALL_BACK
        )
    reading, refused = run_rules(chain[0])
    if not reading or refused is not None:
        return _error((refused or "This workflow doesn't read email.") + _FALL_BACK)
    person = get_person(creator_id)
    if not can_delegate(person) or not is_enabled(creator_id):
        return _error("Its creator hasn't turned on Act as me." + _FALL_BACK)
    if person is None or person.id is None:  # can_delegate checked both; for the type checker
        return _error("I can't tell whose mailbox this is." + _FALL_BACK)
    email = normalize_email(person.email)
    pinned = TurnDelegation(enabled=True, offered=True, person_id=person.id)
    return _Writer(pinned, person, email, gmail_for(email))


def _reader(chat_name: str, handler: Callable[[dict[str, Any]], Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    async def read(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
        from openexecutive.audit.context import rows_for_person
        from openexecutive.orchestrator.mail_read_tools import AS_CREATOR

        try:
            writer = _creator_writer()
        except Exception:
            logger.warning("workflow reads: couldn't check %s", chat_name, exc_info=True)
            return _error("Couldn't check whose mailbox this is just now." + _FALL_BACK)
        if isinstance(writer, str):
            return writer
        if mailbox is not None:
            writer.mailbox = mailbox
        token = AS_CREATOR.set(writer)
        try:
            with rows_for_person(writer.person.id):
                return await handler(tool_input)
        finally:
            AS_CREATOR.reset(token)

    return read


def specs() -> dict[str, tuple[str, str, dict[str, Any], Callable[..., Awaitable[str]]]]:
    """name -> (name, description, input schema, handler) for the tool catalog."""
    from openexecutive.orchestrator.mail_read_tools import MAIL_READ_TOOL_HANDLERS, MAIL_READ_TOOLS

    defs = {str(t["name"]): t for t in MAIL_READ_TOOLS}
    out: dict[str, tuple[str, str, dict[str, Any], Callable[..., Awaitable[str]]]] = {}
    for name, chat_name in _CHAT_NAMES.items():
        tool = defs[chat_name]
        description = (
            "Reads the mailbox or calendar of the person who created this workflow "
            "(Act as me), as theirs alone. " + str(tool["description"])
        )
        out[name] = (name, description, tool["input_schema"], _reader(chat_name, MAIL_READ_TOOL_HANDLERS[chat_name]))
    return out
