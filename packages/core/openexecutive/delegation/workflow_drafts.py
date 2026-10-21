"""``oe__draft_email_as_me``: a workflow step writes an email as the person
who created the workflow, in their voice, as a draft in their own mailbox
(Gmail or Outlook, Act as me).

A workflow that emails from the Executive's own address uses the gateway's
mail tools, as before. This built-in is the other choice, for a workflow
whose emails should come from its creator, and it is fenced in code:

- **Whose mailbox.** Always the workflow's creator (``dynamic_store.get_owner``),
  never an address the step names. A run someone else started by hand, or one
  started by a speaker who is not on the roster, writes nothing: the draft
  would be in the creator's name on someone else's say-so. A scheduled run
  (nobody's) writes as the creator.
- **Only when they turned it on.** The creator may use Act as me
  (``settings.can_delegate``), turned it on (``settings.is_enabled``), and
  their own mailbox is connected (``gmail_status``). Otherwise the call fails
  with a message telling the step to use the Executive's own email instead,
  and nothing is written.
- **Drafts only.** It saves a draft and sends nothing: the person reviews it
  and sends it themselves. Nothing in a workflow can reach the send path
  (``reply_send``).
- **Capped** by ``DELEGATION_MAX_DRAFTS_PER_DAY`` a person, shared with chat
  and the inbox watcher (``caps``), and recorded like a chat draft
  (``drafts.SOURCE_CHAT``): a draft they asked for, so the briefs name it
  while it waits.
- **Recipients** are any well-formed addresses, at most ``MAX_RECIPIENTS``.
  The step's first write to an address asks the creator first, as for any
  workflow write (``action_step.TargetPolicy``); addresses not on the roster
  come back in ``not_in_people`` for the report.
- **Private.** The result the step sees (and the run's report, which a team
  run shows everyone) holds who it is to, the subject and a link, never the
  body: the voice, notes and examples it was written from are the creator's.
  The audit row is private to them.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

TOOL_NAME = "oe__draft_email_as_me"
MAX_INTENT_CHARS = 4000
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

# Use the Executive's own email instead: the step's report says so, and a
# step that also has the gateway's mail tools can fall back on its own.
_FALL_BACK = " Use the Executive's own email for this instead, if this step has a tool for it."

TOOL_DESCRIPTION = (
    "Write an email AS the person who created this workflow, in their own voice, "
    "saved as a DRAFT in their own mailbox (Gmail or Outlook) for them to review "
    "and send themselves. It never sends anything. Works only when they connected "
    "their own mailbox under Act as me; otherwise send from the Executive's own "
    "email. Pass `to` (and optionally `cc`) and `intent`: the points, figures and "
    "dates the email should carry, from this run's results. The tool writes the "
    "subject and the words."
)

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Recipient email addresses.",
        },
        "cc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional Cc addresses.",
        },
        "intent": {
            "type": "string",
            "description": (
                "What the email should say: the points, decision, dates and "
                "figures, in plain words."
            ),
        },
    },
    "required": ["to", "intent"],
}

# The creator of the workflow whose step is running (``action_step._dispatch``).
_creator: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "workflow_draft_creator", default=None
)


@contextlib.contextmanager
def writing_for(person_id: int | None) -> Iterator[None]:
    """Run the block as a step of a workflow ``person_id`` created."""
    token = _creator.set(person_id)
    try:
        yield
    finally:
        _creator.reset(token)


def available_for(person_id: int | None) -> bool:
    """Whether ``person_id``'s workflows may draft as them: they may use Act
    as me and turned it on. Their mailbox is checked when a draft is written.
    Never raises."""
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.people.store import get_person

    if person_id is None:
        return False
    try:
        return can_delegate(get_person(person_id)) and is_enabled(person_id)
    except Exception:
        logger.warning("workflow draft: couldn't read the setting", exc_info=True)
        return False


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


def _started_by_someone_else(creator_id: int) -> bool:
    """Whether the run was started by someone other than the creator, or by
    a speaker the roster can't place."""
    from openexecutive.orchestrator.artifact_records import runs_refused_for_nobody, turn_owner

    if runs_refused_for_nobody():
        return True
    runner = turn_owner()
    return runner is not None and runner != creator_id


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


def _audit(person_id: int, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(
        "delegation_drafted",
        f"Drafted an email as person {person_id} in their own mailbox from a workflow",
        actor="executive",
        details={"person_id": person_id, "source": "workflow", **details},
        private=True,
        private_to_person=person_id,
    )


async def handle_draft_email_as_me(tool_input: dict[str, Any], *, mailbox: Any = None) -> str:
    """The built-in's handler. ``mailbox`` replaces the creator's own client
    (tests). Never raises."""
    from openexecutive.config import get_settings
    from openexecutive.delegation import caps, drafts
    from openexecutive.delegation.ghostwriter import ComposeError, compose
    from openexecutive.delegation.gmail import (
        STATUS_MESSAGES,
        DraftSpec,
        GmailAuthError,
        GmailError,
        gmail_for,
        gmail_status,
        mailbox_link,
        normalize_email,
    )
    from openexecutive.delegation.settings import can_delegate, is_enabled
    from openexecutive.delegation.threads import MAX_RECIPIENTS
    from openexecutive.delegation.training import example_for
    from openexecutive.delegation.voice import composer_model, get_voice, render_voice_block
    from openexecutive.memory.history_drafts import notes_for_draft
    from openexecutive.people.store import get_person

    creator_id = _creator.get()
    if creator_id is None:
        return _error("This workflow has no creator on record to write as." + _FALL_BACK)
    if _started_by_someone_else(creator_id):
        return _error(
            "Only the person who created this workflow can have it write as them, "
            "and someone else started this run." + _FALL_BACK
        )
    try:
        person = get_person(creator_id)
    except Exception:
        logger.warning("workflow draft: roster unreadable", exc_info=True)
        return _error("Couldn't read the People list just now. Try again later.")
    if not can_delegate(person) or not is_enabled(creator_id):
        return _error(
            "Its creator hasn't turned on Act as me (writing drafts as them)." + _FALL_BACK
        )
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
        logger.warning("workflow draft: couldn't open the mailbox", exc_info=True)
        return _error("Couldn't open their mailbox just now." + _FALL_BACK)
    status = await gmail_status(email, gmail=client)
    if status != "connected":
        return _error(STATUS_MESSAGES[status] + _FALL_BACK, status=status)

    settings = get_settings()
    refused = caps.reserve(person.id, settings.delegation_max_drafts_per_day)
    if refused == caps.UNCOUNTABLE:
        return _error("Couldn't check today's draft limit just now. Try again later.")
    if refused == caps.REACHED:
        return _error("Today's limit of drafts written as them is reached." + _FALL_BACK)
    saved = False
    try:
        roster = _roster_by_email(include_contacts=bool(person.is_principal))
        recipients = [*to, *cc]
        stored = get_voice(person.id)
        names = (person.full_name or "").split()
        composed = await compose(
            writer_name=" ".join(names) or email,
            voice_block=render_voice_block(stored.profile, first_name=names[0] if names else "them"),
            thread_text=None,
            reply_subject=None,
            writer_noted=notes_for_draft(person.id, recipients),
            intent=intent,
            recipients=[_recipient(a, roster) for a in recipients],
            signature=stored.profile.signature,
            exec_name=settings.exec_display_name,
            model=composer_model(),
            writer_example=example_for(person.id, recipients[0]) if len(recipients) == 1 else None,
        )
        if not composed.subject:
            return _error("The draft came back without a subject. Try again with a clearer intent.")
        draft = await client.create_draft(DraftSpec(
            to=to, cc=cc, subject=composed.subject, body=composed.body, thread_id=None,
            in_reply_to=None, references=None, from_name=" ".join(names),
        ))
        saved = True
    except GmailAuthError:
        return _error(STATUS_MESSAGES["needs_reconnect"] + _FALL_BACK, status="needs_reconnect")
    except (GmailError, ComposeError):
        logger.warning("workflow draft: drafting failed", exc_info=True)
        return _error("Couldn't write the draft just now. Try again later.")
    except Exception:
        # The composer's model call and the voice/notes reads raise their own errors.
        logger.warning("workflow draft: drafting failed unexpectedly", exc_info=True)
        return _error("Couldn't write the draft just now. Try again later." + _FALL_BACK)
    finally:
        caps.release(person.id, saved=saved)

    try:
        drafts.record(
            person.id, source=drafts.SOURCE_CHAT, thread_id=None,
            draft_id=draft.draft_id, message_id=draft.message_id, now=datetime.now(UTC),
        )
    except Exception:
        logger.warning("workflow draft: couldn't record the draft", exc_info=True)
    flags = list(composed.flags)
    try:
        _audit(person.id, {"draft_id": draft.draft_id, "recipients": len(recipients), "flags": flags})
    except Exception:
        # The draft is saved: an error here would only invite a second one.
        logger.warning("workflow draft: couldn't audit the draft", exc_info=True)
    outside = [a for a in recipients if a not in roster]
    return json.dumps({
        "status": "drafted",
        "to": to,
        "cc": cc,
        "subject": composed.subject,
        "link": mailbox_link(email, message_id=draft.message_id, draft_id=draft.draft_id),
        "open_questions": list(composed.open_questions),
        **({"not_in_people": outside} if outside else {}),
        "note": (
            "Saved as a draft in their own mailbox; nothing was sent. Say in your "
            "report that it is waiting in their Drafts for them to review and send."
        ),
    })
