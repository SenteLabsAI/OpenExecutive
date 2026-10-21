"""Workflows that email as their creator (Act as me): ``oe__draft_email_as_me``
saves a draft in the creator's own mailbox for them to send, and
``oe__send_email_as_me`` sends it. Which one a step has is the choice the
creator made when they saved the workflow ("Draft for me to send" or "Send
as me"); a workflow that emails from the Executive's own address uses the
gateway's mail tools, as before.

Both write in the creator's voice (``ghostwriter.compose``, as chat's
``ghostwrite_email`` does) and are fenced in code:

- **Whose mailbox.** Always the workflow's creator (``dynamic_store.get_owner``),
  never an address the step names. Nothing is written on a run someone else
  started by hand, by a speaker the roster can't place, by one of the
  Executive's own unattended passes (``chat_tools.started_by_a_pass``), or
  when another person's workflow started this one (``chat_tools.running``): the email
  would be in the creator's name on someone else's say-so. A scheduled run
  (nobody's) writes as the creator.
- **Only when they turned it on.** The creator may use Act as me
  (``settings.can_delegate``), turned it on (``settings.is_enabled``) and
  their own mailbox is connected (``gmail_status``). Otherwise the call fails
  telling the step to use the Executive's own email, and nothing is written.
- **Sending needs more.** A workflow with ``oe__send_email_as_me`` can only
  be saved or switched on by its creator, from a session the API knows is
  theirs (``save_refusal``: ``verified.caller_refusal``, the rule Send on a
  reply card uses), and nobody else may change it. At send time the API must
  still be able to tie a save to a person (``handle_it.signing_ok``), and the
  draft goes through ``reply_send.send_for_workflow``, the one send path.
- **First email to a new address asks once.** ``to`` and ``cc`` are write
  targets, so a step's first email to an address waits for the creator's
  approval (``action_step.TargetPolicy``), then is remembered.
- **Capped.** Every draft counts against ``DELEGATION_MAX_DRAFTS_PER_DAY``
  (shared with chat and the inbox, ``caps``), and sends against
  ``DELEGATION_WORKFLOW_MAX_SENDS_PER_DAY`` as well.
- **Private.** The result the step sees (and the run's report) names who it
  is to, the subject and a link, never the body. The audit row is the
  creator's alone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# The composer's model call plus the mailbox calls; longer than a step's
# default so a slow composer doesn't cut a send short.
TIMEOUT_S = 300.0
DRAFT_TOOL = "oe__draft_email_as_me"
SEND_TOOL = "oe__send_email_as_me"
TOOLS = frozenset({DRAFT_TOOL, SEND_TOOL})
MAX_INTENT_CHARS = 4000
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

# The step's report says so, and a step that also has the gateway's mail
# tools can fall back on its own.
_FALL_BACK = " Use the Executive's own email for this instead, if this step has a tool for it."

_WHAT = (
    "Pass `to` (and optionally `cc`) and `intent`: the points, figures and dates "
    "the email should carry, from this run's results. The tool writes the subject "
    "and the words. Works only when they connected their own mailbox under Act as "
    "me; otherwise use the Executive's own email."
)
DRAFT_DESCRIPTION = (
    "Write an email AS the person who created this workflow, in their own voice, "
    "saved as a DRAFT in their own mailbox (Gmail or Outlook) for them to review "
    "and send themselves. It never sends anything. " + _WHAT
)
SEND_DESCRIPTION = (
    "Write an email AS the person who created this workflow, in their own voice, "
    "and SEND it from their own mailbox (Gmail or Outlook). Use only when they "
    "chose to have this workflow send as them. " + _WHAT
)
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses."},
        "cc": {"type": "array", "items": {"type": "string"}, "description": "Optional Cc addresses."},
        "intent": {
            "type": "string",
            "description": "What the email should say: the points, decision, dates and figures, in plain words.",
        },
    },
    "required": ["to", "intent"],
}

# Sends being made right now, per person: counted with today's, so two runs
# at once can't both take the last one.
_SENDING: dict[int, int] = {}


def available_for(person_id: int | None) -> bool:
    """Whether ``person_id``'s workflows may write as them: they may use Act
    as me and turned it on. Their mailbox is checked when one is written.
    Never raises."""
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.people.store import get_person

    if person_id is None:
        return False
    try:
        return can_delegate(get_person(person_id)) and is_enabled(person_id)
    except Exception:
        logger.warning("workflow email: couldn't read the setting", exc_info=True)
        return False


def sends(defn: Any) -> bool:
    """Whether a workflow definition has a step that sends as its creator."""
    return any(SEND_TOOL in (getattr(step, "tools", None) or []) for step in getattr(defn, "steps", []))


def as_creator(defn: Any) -> bool:
    """Whether a workflow definition sends as its creator or reads their mail
    (``workflow_reads``): only its creator, verified, may save or switch it on."""
    from openexecutive.delegation.workflow_reads import reads

    return sends(defn) or reads(defn)


def save_refusal(defn: Any, caller: Any, owner_id: int | None, *, check_tools: bool = True) -> str | None:
    """Why ``caller`` may not save or switch on ``defn`` with its creator
    ``owner_id``, or None. Only a workflow that sends as its creator or reads
    their mail is checked: then the caller must be that creator, and the API
    must know it (``verified.caller_refusal``), and one that reads must keep
    to what a run may do after reading mail (``workflow_reads.definition_refusal``,
    skipped with ``check_tools`` False for the stored copy an edit replaces).
    Never raises; a lookup that fails refuses."""
    from openexecutive.delegation.gmail import normalize_email
    from openexecutive.delegation.verified import NOT_YOURS, caller_refusal
    from openexecutive.delegation.workflow_reads import definition_refusal
    from openexecutive.people.store import get_person

    if not as_creator(defn):
        return None
    try:
        unsafe = definition_refusal(defn, owner_id) if check_tools else None
    except Exception:
        logger.warning("workflow email: couldn't check what the workflow reads", exc_info=True)
        unsafe = "Couldn't check this workflow's tools just now. Try again."
    if unsafe is not None:
        return unsafe
    try:
        owner = get_person(owner_id) if owner_id is not None else None
        refused = caller_refusal(caller, normalize_email(owner.email)) if owner is not None else NOT_YOURS
    except Exception:
        logger.warning("workflow email: couldn't check who is saving", exc_info=True)
        refused = NOT_YOURS
    if refused is None:
        return None
    if refused == NOT_YOURS:
        return (
            "Only the person who created this workflow can save or switch on a workflow "
            "that reads their email or sends email as them."
        )
    if not sends(defn):
        return (
            "A workflow that reads your email needs signed sign-ins on this server, so "
            "nobody else can save one in your name."
        )
    return (
        "A workflow that sends email as you needs signed sign-ins on this server, so "
        "nobody else can save one in your name. Have it save drafts for you to send instead."
    )


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def _addresses(raw: Any, field: str) -> list[str] | str:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    wanted = [str(a).strip().lower() for a in items if isinstance(a, str) and a.strip()]
    malformed = [a for a in wanted if not _EMAIL_RE.fullmatch(a)]
    if malformed:
        return f"Not an email address in `{field}`: {', '.join(malformed[:5])}."
    return list(dict.fromkeys(wanted))


def _someone_else(creator_id: int, chain: tuple[str, ...]) -> bool:
    """Whether someone other than the creator is behind this run: a run they
    started by hand, a speaker the roster can't place, one of the
    Executive's own unattended passes (its context is inbound or stored text
    with nobody watching), or another person's workflow that started this one."""
    from openexecutive.orchestrator.artifact_records import runs_refused_for_nobody, turn_owner
    from openexecutive.workflows.chat_tools import started_by_a_pass
    from openexecutive.workflows.dynamic_store import get_owner

    if runs_refused_for_nobody() or started_by_a_pass():
        return True
    runner = turn_owner()
    if runner is not None and runner != creator_id:
        return True
    return any(get_owner(name) != creator_id for name in chain)


def _roster_by_email(include_contacts: bool) -> dict[str, Any]:
    from openexecutive.people.store import list_people

    people = list_people(include_contacts=include_contacts)
    return {a.lower(): p for p in people for a in [p.email, *p.email_aliases] if a}


def _recipient(email: str, roster: dict[str, Any]) -> Any:
    from openexecutive.delegation.ghostwriter import Recipient

    person = roster.get(email)
    if person is None:
        return Recipient(email=email)
    relation = "on their team" if person.kind == "team" else "one of their contacts"
    if person.role:
        relation = f"{relation}, {person.role}"
    return Recipient(email=email, name=person.full_name, relation=relation)


def _audit(person_id: int, event: str, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(
        event, summary, actor="executive",
        details={"person_id": person_id, "source": "workflow", **details},
        private=True, private_to_person=person_id,
    )


@dataclass
class _Ask:
    person: Any
    email: str
    client: Any
    to: list[str]
    cc: list[str]
    intent: str


async def _ask(tool_input: dict[str, Any], *, mailbox: Any) -> _Ask | str:
    """Who writes, to whom, and what: or the error result."""
    from openexecutive.delegation.gmail import (
        STATUS_MESSAGES,
        gmail_for,
        gmail_status,
        normalize_email,
    )
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.people.store import get_person
    from openexecutive.workflows.chat_tools import running

    chain, creator_id = running()
    if creator_id is None:
        return _error("This workflow has no creator on record to write as." + _FALL_BACK)
    if _someone_else(creator_id, chain):
        return _error(
            "Only the person who created this workflow can have it write as them, "
            "and someone else is behind this run." + _FALL_BACK
        )
    try:
        person = get_person(creator_id)
    except Exception:
        logger.warning("workflow email: roster unreadable", exc_info=True)
        return _error("Couldn't read the People list just now. Try again later.")
    if not can_delegate(person) or not is_enabled(creator_id):
        return _error("Its creator hasn't turned on Act as me (writing email as them)." + _FALL_BACK)
    if person is None or person.id is None:  # can_delegate checked both; for the type checker
        return _error("I can't tell whose mailbox this is.")

    intent = str(tool_input.get("intent") or "").strip()
    if not intent:
        return _error("Pass `intent`: what the email should say.")
    if len(intent) > MAX_INTENT_CHARS:
        return _error(f"`intent` is too long (at most {MAX_INTENT_CHARS} characters).")
    to = _addresses(tool_input.get("to"), "to")
    if isinstance(to, str):
        return _error(to)
    cc = _addresses(tool_input.get("cc"), "cc")
    if isinstance(cc, str):
        return _error(cc)
    cc = [a for a in cc if a not in to]
    if not to:
        return _error("Pass `to`: who the email is for.")
    if len(to) + len(cc) > MAX_RECIPIENTS:
        return _error(f"At most {MAX_RECIPIENTS} recipients.")

    email = normalize_email(person.email)
    try:
        client = mailbox if mailbox is not None else gmail_for(email)
    except Exception:
        logger.warning("workflow email: couldn't open the mailbox", exc_info=True)
        return _error("Couldn't open their mailbox just now." + _FALL_BACK)
    status = await gmail_status(email, gmail=client)
    if status != "connected":
        return _error(STATUS_MESSAGES[status] + _FALL_BACK, status=status)
    return _Ask(person=person, email=email, client=client, to=to, cc=cc, intent=intent)


def _send_slot(person_id: int) -> str | None:
    """Take one of today's sends for ``person_id``, or say why not.
    Synchronous: nothing runs between the check and the take."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import drafts

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        today = drafts.count_since(person_id, start, source=drafts.SOURCE_WORKFLOW_SENT)
    except Exception:
        logger.warning("workflow email: can't count today's sends — refusing", exc_info=True)
        return "Couldn't check today's sending limit just now. Try again later."
    if today + _SENDING.get(person_id, 0) >= get_settings().delegation_workflow_max_sends_per_day:
        return "Today's limit of emails sent as them from workflows is reached." + _FALL_BACK
    _SENDING[person_id] = _SENDING.get(person_id, 0) + 1
    return None


def _send_done(person_id: int) -> None:
    left = _SENDING.get(person_id, 0) - 1
    if left > 0:
        _SENDING[person_id] = left
    else:
        _SENDING.pop(person_id, None)


async def _write(ask: _Ask, *, sending: bool) -> str:
    """Compose, save the draft, and send it when ``sending``. Never raises."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import caps, drafts
    from openexecutive.delegation.ghostwriter import ComposeError, compose
    from openexecutive.delegation.gmail import (
        STATUS_MESSAGES,
        DraftSpec,
        GmailAuthError,
        GmailError,
        mailbox_link,
    )
    from openexecutive.delegation.reply_send import SendRefused, send_for_workflow
    from openexecutive.delegation.training import example_for
    from openexecutive.delegation.voice import composer_model, get_voice, render_voice_block
    from openexecutive.memory.history_drafts import notes_for_draft

    person, settings = ask.person, get_settings()
    if sending:
        refused_send = _send_slot(person.id)
        if refused_send is not None:
            return _error(refused_send)
    refused = caps.reserve(person.id, settings.delegation_max_drafts_per_day)
    if refused is not None:
        if sending:
            _send_done(person.id)
        if refused == caps.UNCOUNTABLE:
            return _error("Couldn't check today's draft limit just now. Try again later.")
        return _error("Today's limit of drafts written as them is reached." + _FALL_BACK)
    saved = False
    try:
        roster = _roster_by_email(include_contacts=bool(person.is_principal))
        recipients = [*ask.to, *ask.cc]
        stored = get_voice(person.id)
        names = (person.full_name or "").split()
        composed = await compose(
            writer_name=" ".join(names) or ask.email,
            voice_block=render_voice_block(stored.profile, first_name=names[0] if names else "them"),
            thread_text=None,
            reply_subject=None,
            writer_noted=notes_for_draft(person.id, recipients),
            intent=ask.intent,
            recipients=[_recipient(a, roster) for a in recipients],
            signature=stored.profile.signature,
            exec_name=settings.exec_display_name,
            model=composer_model(),
            writer_example=example_for(person.id, recipients[0]) if len(recipients) == 1 else None,
        )
        if not composed.subject:
            return _error("The email came back without a subject. Try again with a clearer intent.")
        draft = await ask.client.create_draft(DraftSpec(
            to=ask.to, cc=ask.cc, subject=composed.subject, body=composed.body, thread_id=None,
            in_reply_to=None, references=None, from_name=" ".join(names),
        ))
        saved = True
    except GmailAuthError:
        return _error(STATUS_MESSAGES["needs_reconnect"] + _FALL_BACK, status="needs_reconnect")
    except (GmailError, ComposeError):
        logger.warning("workflow email: drafting failed", exc_info=True)
        return _error("Couldn't write the email just now. Try again later.")
    except Exception:
        # The composer's model call and the voice/notes reads raise their own errors.
        logger.warning("workflow email: drafting failed unexpectedly", exc_info=True)
        return _error("Couldn't write the email just now. Try again later." + _FALL_BACK)
    finally:
        caps.release(person.id, saved=saved)
        if sending and not saved:
            _send_done(person.id)

    source = drafts.SOURCE_WORKFLOW_SENT if sending else drafts.SOURCE_CHAT
    try:
        # A send's row is written before it goes: the daily limit counts it
        # even if the send then fails, never the other way round.
        drafts.record(
            person.id, source=source, thread_id=None,
            draft_id=draft.draft_id, message_id=draft.message_id, now=datetime.now(UTC),
        )
    except Exception:
        logger.warning("workflow email: couldn't record the draft", exc_info=True)
        if sending:
            _send_done(person.id)
            return _error(
                "The email is saved as a draft in their mailbox but wasn't sent, because "
                "today's sends couldn't be counted. Say it is waiting in their Drafts."
            )
    sent_id = ""
    if sending:

        async def send_and_record() -> str:
            try:
                sent = await send_for_workflow(
                    person, ask.client, draft_id=draft.draft_id, recipients=[*ask.to, *ask.cc],
                )
                if sent:
                    try:
                        drafts.mark_sent(person.id, draft.draft_id, sent)
                    except Exception:
                        logger.warning("workflow email: couldn't record the send", exc_info=True)
                return sent
            finally:
                _send_done(person.id)

        try:
            # Shielded: a step timeout must not cancel between the mailbox
            # accepting the send and recording it, or the email goes while
            # the step sees a plain timeout and may write it again.
            sent_id = await asyncio.shield(asyncio.ensure_future(send_and_record()))
        except SendRefused as exc:
            _safe_audit(person.id, "delegation_workflow_send_refused", f"A workflow email as person {person.id} wasn't sent", {
                "draft_id": draft.draft_id, "code": exc.code,
            })
            if exc.code == "send_unconfirmed":
                return _error(
                    "Their mailbox didn't confirm the email was sent. Don't try again: say it "
                    "may have gone and they should check their Sent folder."
                )
            return _error(
                f"The email wasn't sent: {exc.message} It is waiting in their Drafts; say so in your report."
            )

    details = {"draft_id": draft.draft_id, "recipients": len(recipients), "flags": list(composed.flags)}
    if sending:
        _safe_audit(person.id, "delegation_workflow_sent", f"Sent an email as person {person.id} from a workflow", {
            **details, "sent_message_id": sent_id,
        })
    else:
        _safe_audit(person.id, "delegation_drafted", f"Drafted an email as person {person.id} from a workflow", details)
    outside = [a for a in recipients if a not in roster]
    return json.dumps({
        "status": "sent" if sending else "drafted",
        "to": ask.to,
        "cc": ask.cc,
        "subject": composed.subject,
        **({} if sending else {
            "link": mailbox_link(ask.email, message_id=draft.message_id, draft_id=draft.draft_id),
            "open_questions": list(composed.open_questions),
        }),
        **({"not_in_people": outside} if outside else {}),
        "note": (
            "Sent from their own mailbox, as them. Say in your report who it went to and the subject."
            if sending else
            "Saved as a draft in their own mailbox; nothing was sent. Say in your report that it "
            "is waiting in their Drafts for them to review and send."
        ),
    })


def _safe_audit(person_id: int, event: str, summary: str, details: dict[str, Any]) -> None:
    try:
        _audit(person_id, event, summary, details)
    except Exception:
        # The email is written: an error here would only invite a second one.
        logger.warning("workflow email: couldn't audit", exc_info=True)


async def handle_draft_email_as_me(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
    """``oe__draft_email_as_me``. ``mailbox`` replaces the creator's own
    client (tests). Never raises."""
    ask = await _ask(tool_input, mailbox=mailbox)
    return ask if isinstance(ask, str) else await _write(ask, sending=False)


async def handle_send_email_as_me(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
    """``oe__send_email_as_me``. ``mailbox`` replaces the creator's own client
    (tests). Never raises."""
    ask = await _ask(tool_input, mailbox=mailbox)
    return ask if isinstance(ask, str) else await _write(ask, sending=True)
