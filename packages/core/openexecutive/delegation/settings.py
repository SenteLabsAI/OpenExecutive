"""Who has Act as me on, and whether this turn may use it.

One row per person in ``delegation_settings``: a person turns it on for
themselves (``PUT /delegation``), and nobody turns it on for anyone else.
Absent, unreadable or off all mean off.

**Who may have it.** ``can_delegate`` is the single rule. Phase 1 answers yes
for the principal only: the safety rails a teammate's mailbox would need
(audit rows private to *that* person, guarded persona edits, per-seat
credentials) do not exist yet. Everything else here is keyed by person, so
widening that one function is what the teammate phase changes.

**Per turn.** ``pin_turn_delegation`` runs at the start of every chat turn
(``Executive.stream_chat`` and the committee path), next to the workspace-mode
pin, and records a ``TurnDelegation``: whether the speaker has it on, whether
``ghostwrite_email`` is offered this turn, and — set by the tool — whether the
turn has touched the speaker's mailbox (every audit row the turn writes after
that is private). The pin is held in a context variable for the turn's task as
well as on the session, so two turns running at once on one session (a second
browser tab) each read their own. It is offered only when all of these hold:

- the speaker may have it (``can_delegate``) and turned it on;
- the speaker is the principal on a surface that verified it is them
  (``people_tools.is_principal_on_verified_surface``), and a web turn also
  carried a signed-in caller (``x-caller-email``) or runs under local login.
  A request with no caller header is not a sign-in and never gets it. Whoever
  holds ``BACKEND_SHARED_SECRET`` is trusted as the UI proxy that stamps that
  header, as on every principal-only route;
- the conversation is private to the speaker: the web chat, a Slack or
  Discord DM, or a verified private Telegram chat — never a shared channel or
  thread, where a draft's preview or the matching threads would be posted for
  everyone and other people's messages sit in the model's context;
- the turn is not unattended and not private to the principal (the email
  poller's turns), so inbound text can never reach it.

The Gmail connection is deliberately NOT part of the offer: the handler checks
it on every call and says how to fix it, and the tool list (a cached prefix)
does not flip when a token lapses.

**The system prompt.** ``block0_delegation_on`` is the install-level flag the
cached persona block keys its constant ``DELEGATION_ADDENDUM`` on — whether
anyone here has it on (Phase 1: the principal) — so it changes only when the
setting does, never per turn or per speaker.
"""
from __future__ import annotations

import contextvars
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openexecutive.delegation.schema import SETTINGS_TABLE, ensure_schema
from openexecutive.utils.deployment import is_local_login as local_login

if TYPE_CHECKING:
    from openexecutive.people.models import Person

logger = logging.getLogger(__name__)


@dataclass
class DelegationOverride:
    """Evals and tests only: run a turn as if ``person`` had Act as me set to
    ``enabled``, against ``gmail`` — a fake mailbox, never a real one. With an
    override the surface checks are skipped (an eval has no web sign-in), so
    the tool is offered only when a fake mailbox is supplied."""

    enabled: bool
    gmail: Any = None
    person: Person | None = None


@dataclass
class TurnDelegation:
    """Act as me for the turn in progress (``Session.turn_delegation``)."""

    enabled: bool = False
    offered: bool = False
    # Set by ``ghostwrite_email`` before it reads the mailbox: from then on
    # every audit row the turn writes is private (``people_tools``).
    touched_mail: bool = False
    # Drafts made this turn, against the per-turn cap.
    drafts: int = 0
    # The speaker's own words this turn (``executive._speaker_text``): a new
    # email may go to an address they typed. Pinned here because the turn's
    # message joins the session history only once the turn ends.
    speaker_text: str = ""
    person_id: int | None = None
    # The session this pin was made for, so a pin left in a task's context
    # never answers for another session.
    session_id: str | None = None


# The pin for the turn this task is running. Concurrent turns on one session
# run in separate tasks, so each reads its own even though the session object
# (and its ``turn_delegation`` attribute) is shared.
_TURN: contextvars.ContextVar[TurnDelegation | None] = contextvars.ContextVar(
    "delegation_turn", default=None
)


def can_delegate(person: Person | None) -> bool:
    """Whether ``person`` may turn Act as me on for themselves. Phase 1: the
    principal (a non-archived team member flagged ``is_principal``) only."""
    return bool(
        person is not None
        and person.id is not None
        and person.is_principal
        and not person.archived
        and person.kind == "team"
    )


def _resolve_db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def is_enabled(person_id: int | None, *, db_path: Path | None = None) -> bool:
    """Whether ``person_id`` turned it on. Never raises: a missing file, table
    or row, or any read error, is off (logged)."""
    if person_id is None:
        return False
    path = _resolve_db_path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                f"SELECT enabled FROM {SETTINGS_TABLE} WHERE person_id = ?",  # noqa: S608 — constant table name
                (person_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            logger.warning("delegation: could not read the setting (%s) — treating it as off", exc)
        return False
    except Exception:
        logger.warning("delegation: could not read the setting — treating it as off", exc_info=True)
        return False
    return bool(row is not None and row[0])


def set_enabled(
    person_id: int, enabled: bool, *, updated_by: str, db_path: Path | None = None
) -> None:
    """Store ``person_id``'s setting. Callers authorize first (the route)."""
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    try:
        ensure_schema(conn)
        conn.execute(
            f"INSERT INTO {SETTINGS_TABLE} (person_id, enabled, updated_at, updated_by) "  # noqa: S608 — constant table name
            "VALUES (?, ?, ?, ?) ON CONFLICT(person_id) DO UPDATE SET "
            "enabled = excluded.enabled, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (person_id, 1 if enabled else 0, now, updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def _principal() -> Person | None:
    try:
        from openexecutive.people.store import find_principal_person

        return find_principal_person()
    except Exception:
        logger.warning("delegation: principal lookup failed — treating it as off", exc_info=True)
        return None


def enabled_for_install() -> bool:
    """Whether anyone on this install has it on (Phase 1: the principal).
    Never raises."""
    principal = _principal()
    return can_delegate(principal) and is_enabled(principal.id if principal else None)


def block0_delegation_on(session: Any) -> bool:
    """The flag the cached persona block keys ``DELEGATION_ADDENDUM`` on: the
    session's override when it carries one, else ``enabled_for_install``."""
    override = getattr(session, "delegation_override", None)
    if isinstance(override, DelegationOverride):
        return override.enabled
    return enabled_for_install()


def _speaker(session: Any) -> Person | None:
    person_id = getattr(session, "caller_person_id", None)
    if not isinstance(person_id, int):
        return None
    try:
        from openexecutive.people.store import get_person

        return get_person(person_id)
    except Exception:
        logger.warning("delegation: speaker lookup failed — not offering it", exc_info=True)
        return None


# Slack and Discord sessions for a direct message (the adapters' session ids).
_DM_SESSION_PREFIXES = {"slack": "slack:dm:", "discord": "discord:dm:"}


def _private_conversation(session: Any) -> bool:
    """Whether only the speaker (and the Executive) can read this
    conversation: the web chat, a Slack or Discord DM, or a Telegram chat
    (``is_principal_on_verified_surface`` already accepts only a private one)."""
    if getattr(session, "from_web_chat", False) is True:
        return True
    channel = str(getattr(session, "origin_channel", "") or "")
    if channel == "telegram":
        return True
    prefix = _DM_SESSION_PREFIXES.get(channel)
    session_id = str(getattr(session, "session_id", "") or "")
    return prefix is not None and session_id.startswith(prefix)


def speaker_surface_ok(session: Any, person: Person | None) -> bool:
    """Whether ``session``'s turn comes from ``person`` themselves, verified,
    in a conversation private to them — the surface half of the offer, which
    ``ghostwrite_email`` checks again on every call. Never raises."""
    if not can_delegate(person):
        return False
    try:
        from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface

        if not is_principal_on_verified_surface(session):
            return False
    except Exception:
        logger.warning("delegation: surface check failed — not offering it", exc_info=True)
        return False
    if not _private_conversation(session):
        return False
    if getattr(session, "from_web_chat", False) is True:
        return getattr(session, "web_caller_signed_in", False) is True or local_login()
    return True


def _offered(session: Any, person: Person | None, enabled: bool) -> bool:
    if not enabled:
        return False
    if getattr(session, "unattended", False) is True:
        return False
    if getattr(session, "private_to_principal", False) is True:
        return False
    override = getattr(session, "delegation_override", None)
    if isinstance(override, DelegationOverride):
        return override.gmail is not None
    return speaker_surface_ok(session, person)


def pin_turn_delegation(session: Any, speaker_text: str) -> TurnDelegation:
    """Resolve Act as me for a NEW turn and pin it on the session.

    Read fresh every turn — never the previous turn's pin — so turning it off
    applies from the very next message. Never raises: anything that cannot be
    read leaves it off for the turn."""
    override = getattr(session, "delegation_override", None)
    person: Person | None
    if isinstance(override, DelegationOverride):
        person = override.person
        enabled = bool(override.enabled)
    else:
        person = _speaker(session)
        enabled = can_delegate(person) and is_enabled(person.id if person else None)
    pinned = TurnDelegation(
        enabled=enabled,
        offered=_offered(session, person, enabled),
        speaker_text=speaker_text,
        person_id=person.id if person is not None else None,
        session_id=getattr(session, "session_id", None),
    )
    session.turn_delegation = pinned
    _TURN.set(pinned)
    return pinned


def turn_delegation(session: Any) -> TurnDelegation | None:
    """The pinned state for ``session``'s current turn, or None: this task's
    own pin when it was made for ``session``, else the one on the session."""
    pinned = _TURN.get()
    if (
        isinstance(pinned, TurnDelegation)
        and session is not None
        and pinned.session_id == getattr(session, "session_id", None)
    ):
        return pinned
    stored = getattr(session, "turn_delegation", None)
    return stored if isinstance(stored, TurnDelegation) else None


# Tag blocks the adapters add around the speaker's words (e.g. the
# ``<outbound_reply_context>`` backstory hydrated into a Slack or Discord DM).
_INJECTED_BLOCK = re.compile(r"<([a-z_]+)\b[^>]*>.*?</\1\s*>", re.DOTALL)
_ADDRESS = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


# The label an adapter puts before a document's extracted text
# (``integrations.attachments``). It has no closing mark, so a message that
# carries one can't be split into the speaker's words and the document's.
_ATTACHED_TEXT = re.compile(r"(?m)^\[Attached: ")


def typed_addresses(speaker_text: str) -> set[str]:
    """Email addresses the speaker typed this turn — outside any tag block an
    adapter added (quoted backstory is not something they typed). None at all
    when a document's text was inlined into the message: its addresses can't
    be told apart from theirs, so they give the address in a message of its
    own (or add the person as a contact)."""
    text = speaker_text or ""
    if _ATTACHED_TEXT.search(text):
        return set()
    own_words = _INJECTED_BLOCK.sub(" ", text)
    return {m.group(0).lower() for m in _ADDRESS.finditer(own_words)}


def turn_touched_delegate_mail(session: Any = None) -> bool:
    """Whether the current turn has touched the speaker's mailbox (the tool
    sets it before its first read). ``session`` defaults to the bound one."""
    if session is None:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
    pinned = turn_delegation(session)
    return pinned is not None and pinned.touched_mail
