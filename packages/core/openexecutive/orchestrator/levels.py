"""The one check: Careful, Balanced and Hands-off decide what a turn does.

The owner picks one level for the company (Settings -> Safety,
``orchestrator.outside_mode``). It applies the same way on every channel: the
web chat, email, Slack, Telegram, Google Chat, Discord, Teams, the phone, an
AI app, a shared chat and scheduled work.

The checks that notice why a step might wait (a read that tried to give
orders, the owner's own mail read this turn, a turn private to the owner,
a shared chat a teammate wrote into, an AI app's turn, a tool that changes
the install) only *detect*. This module decides, in one place
(``decide``), and the dispatch guard in ``orchestrator.executive`` does what
it says:

- **Hands-off**: the step runs. Nothing waits.
- **Balanced** and **Careful**: the step comes to the owner as an Approve
  card (``hold``). Careful detects more (any outside read), Balanced less
  (only a read the check flagged): the outside-content check is the
  level's.
- **Every level**: a tool only the owner may use never runs on someone
  else's turn, and what mail from someone not in People asks for is never
  done on that mail's say-so. The Executive answers them, and anything else
  comes to the owner as an Approve card.

A step that waits is never dropped. The card is a decision with a card on
Today, under the reply in the web chat, and in the owner's chat app
(``integrations.channel_cards``). Approve carries out the exact call on the
server (``take_the_lead.carry_out``, ``api.routes.decisions``).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

RUN = "run"
CARD = "card"

# Why a step was noticed. Each is detected where it always was; only
# ``decide`` turns it into run or card.
OUTSIDE = "outside_content"
OWN_MAIL = "mail_touched"
# The owner's own mail was read this turn and the content check flagged some
# of what it read: Balanced holds then too, and the card says why.
MAIL_FLAGGED = "mail_flagged"
PRIVATE = "private_turn"
SHARED = "shared_chat"
AI_APP = "mcp_client"
NOT_OWNER = "not_principal"
UNATTENDED = "unattended"
STRANGER = "stranger_mail"
NEW_PERSON = "new_person"
FILE_PLACE = "file_place"
URL_FETCH = "url_fetch"
REASONS: frozenset[str] = frozenset({
    OUTSIDE, OWN_MAIL, MAIL_FLAGGED, PRIVATE, SHARED, AI_APP, NOT_OWNER, UNATTENDED, STRANGER, NEW_PERSON,
    FILE_PLACE, URL_FETCH,
})

# What the card says it waited for, in the owner's words.
_WHY: dict[str, str] = {
    OUTSIDE: "it read something from outside the company that tried to give it instructions",
    OWN_MAIL: "it read your own mail first",
    MAIL_FLAGGED: "something it read looked like it was trying to give it instructions",
    PRIVATE: "it came from mail that's private to you",
    SHARED: "someone else wrote in this shared chat",
    AI_APP: "another AI app asked for it",
    NOT_OWNER: "it changes how your Executive is set up",
    UNATTENDED: "it came up while nobody was chatting",
    STRANGER: "someone who isn't in People asked for it by email",
    NEW_PERSON: "it goes to someone who isn't in People",
    FILE_PLACE: "the folder it goes in is shared outside the company",
    URL_FETCH: "it has a service fetch a web address",
}

# A step the owner approved on a card is being carried out
# (``take_the_lead.carry_out``): their tap is the OK, so nothing waits again.
# Skills that run further steps of their own: approving one doesn't relax
# the checks for those.
NOT_RELAXED_SKILLS: frozenset[str] = frozenset({
    "run_workflow", "run_executive_research", "run_script", "run_python_job", "suggest_workflow",
})
_APPROVED: contextvars.ContextVar[bool] = contextvars.ContextVar("levels_approved", default=False)


@contextlib.contextmanager
def approved() -> Iterator[None]:
    """Carry out a step the owner approved: inside it, ``relaxed`` is true."""
    token = _APPROVED.set(True)
    try:
        yield
    finally:
        _APPROVED.reset(token)


def is_approved() -> bool:
    """Whether a step the owner (or the person) approved on a card is being
    carried out right now."""
    return _APPROVED.get()


def relaxed() -> bool:
    """Whether the checks that only protect the owner from their own
    Executive are off right now: Hands-off, or a step they approved. A turn
    answering mail from someone not in People never is. Never raises: an
    error is False."""
    try:
        if _APPROVED.get():
            return True
        from openexecutive.orchestrator import outside_content, outside_mode

        if outside_content.answers_off_team_mail():
            return False
        return turn_level() == outside_mode.TRUST
    except Exception:
        logger.warning("levels: couldn't read the level — checking as usual", exc_info=True)
        return False


def card_or(blocked: str, tool: str, arguments: dict[str, Any], *, reason: str, mcp: bool = True) -> str:
    """A connected app's call a check stopped below Hands-off: on a turn
    someone is in, an Approve card for the owner; else the check's own
    error. A stranger's mail turn gets the card too: its owner decides."""
    try:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
        # No turn, or a card being carried out (take_the_lead.carry_out runs
        # unattended): the check's own error, so a failed approval reads as
        # failed instead of a fresh card.
        if session is None or getattr(session, "unattended", False) is True:
            return blocked
        return hold(tool, arguments, mcp=mcp, reason=reason, session=session)
    except Exception:
        logger.warning("levels: couldn't put a stopped call on a card", exc_info=True)
        return blocked


def level() -> str:
    """The company's level (``outside_mode.ASK`` / ``OUTSIDERS`` / ``TRUST``)."""
    from openexecutive.orchestrator import outside_mode

    return outside_mode.get_mode()


def turn_level(session: Any = None) -> str:
    """The level the turn in progress runs at: the one its outside-content
    record started with, else the company's for ``session``."""
    from openexecutive.orchestrator import outside_content, outside_mode

    state = outside_content.current()
    if state is not None and state.mode in outside_mode.MODES:
        return str(state.mode)
    if session is None:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
    return outside_mode.for_turn(session)


def mail_reason() -> str:
    """Why a step waits after the owner's own mail was read this turn:
    ``MAIL_FLAGGED`` when the content check flagged some of what it read,
    else ``OWN_MAIL``. Never raises."""
    try:
        from openexecutive.orchestrator import outside_content

        state = outside_content.current()
        flags = getattr(state, "flags", None) or []
        if any(source == outside_content.MAIL for source, _, _ in flags):
            return MAIL_FLAGGED
    except Exception:
        logger.debug("levels: couldn't tell whether a mail read was flagged", exc_info=True)
    return OWN_MAIL


def decide(reason: str, *, mode: str | None = None) -> str:
    """``RUN`` or ``CARD`` for a step noticed for ``reason``. Never raises:
    an error is a card."""
    from openexecutive.orchestrator import outside_mode

    try:
        # Hands-off is the owner's trust in their own Executive: it gives no
        # one else the owner's powers, and no stranger's mail its say-so.
        if reason in (STRANGER, NOT_OWNER, SHARED):
            return CARD
        if reason == AI_APP and not _owners_ai_app():
            # The owner's own AI app, signed in as them and allowed to act,
            # follows their level like the web app; any other is a card.
            return CARD
        current = mode if mode is not None else turn_level()
        if current == outside_mode.TRUST:
            return RUN
        return CARD
    except Exception:
        logger.warning("levels: couldn't decide — putting it on a card", exc_info=True)
        return CARD


def _owners_ai_app() -> bool:
    from openexecutive.orchestrator import outside_mode
    from openexecutive.orchestrator.schedule_tools import current_session

    return outside_mode.owners_ai_app(current_session.get())


_OWNER_SEND_TOOLS = frozenset({"google_workspace__send_gmail_message"})
_RECIPIENT_KEYS = ("to", "cc", "bcc")
_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def only_to_owner(tool_name: str, tool_input: Any) -> bool:
    """Whether a step only sends the owner a message: a direct message to
    them, or mail addressed to no one but them. Waiting for their OK would
    only show them what it says, so it never waits. Fails closed."""
    try:
        from openexecutive.orchestrator import take_the_lead

        if tool_name != "call_tool":
            return isinstance(tool_input, dict) and take_the_lead._only_to_principal(tool_name, tool_input)
        if not isinstance(tool_input, dict):
            return False
        # Only a fresh Gmail send, whose recipients are all in to/cc/bcc. A
        # reply or forward also reaches the thread's people, and other mail
        # tools nest their recipients, so neither counts.
        named = str(tool_input.get("name") or "")
        if named not in _OWNER_SEND_TOOLS:
            return False
        arguments = tool_input.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            return False
        found: set[str] = set()
        for key in _RECIPIENT_KEYS:
            value = arguments.get(key)
            text = value if isinstance(value, str) else json.dumps(value) if value else ""
            found |= {a.lower() for a in _ADDRESS.findall(text)}
        if not found or arguments.get("thread_id") or arguments.get("in_reply_to"):
            return False
        from openexecutive.people.store import find_principal_person

        owner = find_principal_person()
        if owner is None:
            return False
        own = {a.lower() for a in [owner.email or "", *owner.email_aliases] if a}
        return bool(own) and found <= own
    except Exception:
        logger.debug("levels: couldn't tell whether a step only reaches the owner", exc_info=True)
        return False


def carriable(tool: str, *, mcp: bool) -> bool:
    """Whether an Approve tap can carry out ``tool`` exactly: a connected
    app's call, or a skill the Executive runs. A script or a new server is
    the turn's own and can't wait on a card."""
    if mcp:
        return True
    try:
        from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS
    except Exception:
        return False
    return tool in _ALL_SKILL_HANDLERS


def _requester(session: Any) -> Any:
    """Who the card says asked: the speaker on People, else the owner."""
    from openexecutive.people.models import Person
    from openexecutive.people.store import find_principal_person, get_person

    pid = getattr(session, "caller_person_id", None)
    if isinstance(pid, int) and not isinstance(pid, bool):
        try:
            person = get_person(pid)
            if person is not None:
                return person
        except Exception:
            logger.debug("levels: speaker lookup failed", exc_info=True)
    sender = str(getattr(session, "email_from", "") or "")
    if getattr(session, "sender_off_team", False) is True and sender:
        return Person(id=None, full_name=sender[:120])
    try:
        owner = find_principal_person()
    except Exception:
        owner = None
    return owner if owner is not None else Person(id=None, full_name="You")


def hold(
    tool: str,
    tool_input: dict[str, Any],
    *,
    mcp: bool,
    reason: str,
    session: Any = None,
) -> str:
    """Put the exact step on an Approve card for the owner, and return the
    tool result that tells the model it is waiting. The same step still
    waiting is that card, not a new one."""
    return hold_many(tool, [tool_input], mcp=mcp, reason=reason, session=session)


def hold_many(
    tool: str,
    tool_inputs: list[dict[str, Any]],
    *,
    mcp: bool,
    reason: str,
    session: Any = None,
) -> str:
    """``hold`` for several calls of one tool held together (fourteen
    renames in one round): one Approve card that lists them all, and one tap
    carries out each, in order. The tool result is the same for each call."""
    from openexecutive.orchestrator import department_gate, take_the_lead

    steps = [dict(i or {}) for i in tool_inputs] or [{}]
    if mcp and not str(tool or "").strip():
        # Nothing anyone could approve (no tool named).
        return json.dumps({"error": "call_tool needs the tool's name: find it with search_tools first."})
    if session is None:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
    why = _WHY.get(reason, "it needs your OK")
    verdict = department_gate.Verdict(
        run=False,
        approver_person_id=take_the_lead._principal_id(),
        reason=why,
    )
    try:
        decision_id = department_gate.hold(
            _requester(session),
            tool,
            steps[0],
            verdict,
            mcp=mcp,
            session_id=getattr(session, "session_id", None),
            held=True,
            more=steps[1:],
        )
    except Exception:
        logger.exception("levels: couldn't put %s on a card", tool)
        return json.dumps({
            "error": f"{tool} was not done and couldn't be put on an Approve card. Tell them what you would have done.",
        })
    together = (
        f" It is one card for all {len(steps)} of these {tool} calls: one tap does them all."
        if len(steps) > 1 else ""
    )
    return json.dumps({
        "status": "waiting_for_approval",
        "decision_instance_id": decision_id,
        "message": (
            "Not done yet: it's on an Approve card for them (in this chat, on Today and in their chat app), "
            f"because {why}. Tapping Approve does exactly this.{together} Tell them in one line it's "
            "waiting for their tap. Do not retry or work around it."
        ),
    })


def waiting_chip(result: str, *, label: str, iteration: int | None = None) -> dict[str, Any] | None:
    """The ``action_taken`` event for a step ``hold`` put on a card, carrying
    the card's id so the chat shows the card itself. None when it wasn't."""
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return None
    decision_id = parsed.get("decision_instance_id") if isinstance(parsed, dict) else None
    if not isinstance(decision_id, int) or isinstance(decision_id, bool):
        return None
    chip: dict[str, Any] = {
        "type": "action_taken",
        "tool": label,
        "summary": "Waiting for your approval",
        "target": None,
        "link": "/today",
        "decision_id": decision_id,
    }
    if iteration is not None:
        chip["iteration"] = iteration
    return chip


def not_carriable_error(tool: str, reason: str) -> str:
    """For a step a tap can't carry out (a script, a new server): what the
    model says instead. It is told, never silently dropped."""
    why = _WHY.get(reason, "it needs your OK")
    return json.dumps({
        "error": (
            f"{tool} was not run here because {why}, and it can't wait on a card. "
            "Say in one line what you would have run and that they can ask for it again "
            "from the web app or their own chat. Do not retry it in this turn."
        )
    })
