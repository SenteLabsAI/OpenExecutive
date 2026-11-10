"""What it does for you: everything that goes out without asking, and why.

What it does for you, beside each Delegate tab, is read-only. It answers one
question for the person looking at it, with their settings right now: what
will the Executive send or change without asking anyone, who does it go out
as, and which setting allows it. Each row's ``what`` is one sentence of what
it does; ``why`` says which setting decides it. Every answer is
read from the same code the gates use, so the page can't say something the
gates don't do:

- **As you** (the caller's own, ``can_delegate``): Write drafts as me
  (``settings.is_enabled``), Draft replies to my inbox (``inbox.get_watch``,
  which is also what lets replies go on their own, ``handle_it.get``),
  their own In training (``training.in_training``: everything waits until
  they allow each person) and
  the Approvals level (``handle_it.approvals_hold``), and the cards it
  suggests from their mail
  (``action_cards.on_its_own_refusal``). The checks in ``handle_it.refusal``
  and ``follow_up_refusal`` are the source of each row.
- **As the Executive** (the principal's alone): what it does on its own,
  which Approvals decides (``take_the_lead._gated``): Hands-off goes,
  otherwise an Approve card; In training (``take_the_lead.get``) holds
  everything not yet allowed, on any level. Meetings asked for in chat
  (``calendar_tools.handle_create_calendar_event``) book when the owner
  asks.
- **Departments**: the Approvals level each department's work runs at
  (``departments.authority.department_level``), covering department-scoped
  scheduled work, check-ins, alert review and team members' chat sends
  (``orchestrator.department_gate``).

The page also says when one setting rides on another (``notes``), when the
Executive is paused (``scheduler.pause``), and when nothing can be sent as
anyone because this server can't tie a switch to a person
(``handle_it.signing_ok``).

Nothing here writes, and nothing here reads anyone's mail.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Outcome = Literal["goes", "waits", "always_waits", "drafts", "off"]

# Where each row's setting lives: the UI turns these into links.
SETTING_ACT_AS_ME = "act_as_me"
SETTING_TAKE_THE_LEAD = "take_the_lead"
SETTING_PAUSE = "pause"
# Approvals (Careful / Balanced / Hands-off), on Settings → Your Executive.
SETTING_APPROVALS = "approvals"
SETTING_NONE = ""
_DEPARTMENT_PREFIX = "department:"

SECTION_AS_YOU = "as_you"
SECTION_EXECUTIVE = "executive"
SECTION_DEPARTMENTS = "departments"

# The Approvals level each department authority level stands for.
DEPARTMENT_LEVEL_NAMES: dict[str, str] = {
    "auto_execute": "Hands-off",
    "propose_only": "Balanced",
    "escalate": "Careful",
}


def department_setting(slug: str) -> str:
    return f"{_DEPARTMENT_PREFIX}{slug}"


@dataclass(frozen=True)
class Row:
    """One thing that can go out, what happens to it, and why."""

    id: str
    what: str
    outcome: Outcome
    # The outcome in a few words: "Goes out", "Waits for you".
    then: str
    # Which setting decides it, in a sentence.
    why: str
    # Where that setting lives (``SETTING_*``, or ``department:<slug>``).
    setting: str = SETTING_NONE
    # Things the row lists: the rules that hold it, say.
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    rows: list[Row] = field(default_factory=list)
    # Sentences shown above the rows: one setting riding on another, a
    # switch that is off for everyone here.
    notes: list[str] = field(default_factory=list)
    # A sentence shown under the rows.
    footer: str = ""
    # False when the viewer can't see this section; ``unavailable`` says why.
    available: bool = True
    unavailable: str = ""

    @property
    def goes(self) -> int:
        """How many of its rows go out (or act) without asking."""
        return sum(1 for r in self.rows if r.outcome == "goes")


@dataclass(frozen=True)
class MayDo:
    paused: bool
    sections: list[Section]


# --------------------------------------------------------------------------- #
# As you
# --------------------------------------------------------------------------- #

def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def as_you(person_id: int, *, can_have_it: bool, signing: bool, is_principal: bool = False) -> Section:
    """What goes out as ``person_id``, from their own mailbox: what it
    writes as them, and Approvals, decide."""
    from openexecutive.delegation import inbox
    from openexecutive.delegation.settings import is_enabled
    from openexecutive.orchestrator import outside_mode

    title = "As you, from your own mailbox"
    if not can_have_it:
        return Section(
            SECTION_AS_YOU, title, available=False,
            unavailable="As you isn't available to you here. The owner of this Open Executive can turn it on "
            "for team members.",
        )

    drafts_on = is_enabled(person_id)
    watching = drafts_on and inbox.get_watch(person_id).enabled
    level = _level()
    level_word = _LEVEL_WORDS.get(level, "Careful")
    careful = level == outside_mode.ASK
    in_training = _in_training(person_id)
    sends = watching and signing and not careful and not in_training
    training_why = "In training: each one waits on a card until you allow that person."

    rows: list[Row] = []
    notes: list[str] = []

    # Emails it writes when asked in chat: always a draft, sent only on the
    # person's own tap (Send under it in the chat, or from their mailbox).
    if not drafts_on:
        chat_why = "Write drafts as me is off, so it writes as itself."
    elif signing:
        chat_why = "Nothing goes until you tap Send under it in the chat, or send it from your Drafts."
    else:
        chat_why = (
            "They wait in your Drafts for you to send. Send in the chat needs signed sign-ins, "
            "which are off on this server."
        )
    rows.append(Row(
        "chat_drafts", "Emails you ask for in chat.",
        "drafts" if drafts_on else "off",
        "Drafts, sent on your tap" if drafts_on else "Off",
        chat_why,
        SETTING_ACT_AS_ME,
    ))

    def mail_row(row_id: str, what: str, goes_why: str) -> Row:
        if not watching:
            why = (
                "Draft replies to your inbox is off." if drafts_on
                else "Write drafts as me is off. Replies to your inbox need it."
            )
            return Row(row_id, what, "off", "Off", why, SETTING_ACT_AS_ME)
        if not signing:
            return Row(
                row_id, what, "waits", "Waits for you",
                "Signed sign-ins are off on this server, so nothing goes out as you.",
                SETTING_ACT_AS_ME,
            )
        if careful:
            return Row(
                row_id, what, "waits", "Waits for you",
                "Approvals is on Careful, so each one waits on a card for you.",
                SETTING_APPROVALS,
            )
        if in_training:
            return Row(row_id, what, "waits", "Waits for you", training_why, SETTING_ACT_AS_ME)
        return Row(row_id, what, "goes", "Goes out", f"{goes_why} Approvals is on {level_word}.", SETTING_ACT_AS_ME)

    rows.append(mail_row(
        "replies", "Replies to your email.",
        "When the email needs a simple answer and it's sure, to the people already on it.",
    ))
    rows.append(mail_row(
        "follow_ups", "Follow-ups when your email goes unanswered.",
        "To the people it went to.",
    ))
    rows.append(mail_row(
        "invites", "Meeting invites.",
        "Accepted when nothing clashes"
        + (", from anyone." if level == outside_mode.TRUST else ", from your team and contacts."),
    ))

    # Messages and meetings it suggests from their mail.
    if drafts_on:
        what = "Messages and invites it suggests from your mail."
        if not watching:
            rows.append(Row(
                "actions", what, "waits", "Waits for you",
                "Each one waits on a card. Draft replies to your inbox is off.", SETTING_ACT_AS_ME,
            ))
        elif not signing:
            rows.append(Row(
                "actions", what, "waits", "Waits for you",
                "Signed sign-ins are off on this server, so nothing goes out as you.", SETTING_ACT_AS_ME,
            ))
        elif careful:
            rows.append(Row(
                "actions", what, "waits", "Waits for you",
                "Approvals is on Careful, so each one waits on a card for you.", SETTING_APPROVALS,
            ))
        elif in_training:
            rows.append(Row("actions", what, "waits", "Waits for you", training_why, SETTING_ACT_AS_ME))
        elif level == outside_mode.TRUST:
            rows.append(Row(
                "actions", what, "goes", "Goes out", "Approvals is on Hands-off.", SETTING_APPROVALS,
            ))
        else:
            rows.append(Row(
                "actions", what, "goes", "Goes out",
                "To people on your People list. New contacts and anyone added in the last week wait. "
                "Approvals is on Balanced.",
                SETTING_APPROVALS,
            ))

    if watching and not signing:
        notes.append(
            "Signed sign-ins are off on this server, so nothing goes out as you until they're on."
        )

    footer = ""
    if sends:
        try:
            from openexecutive.config import get_settings

            cap = int(get_settings().delegation_handle_it_max_sends_per_day)
            footer = f"Up to {_plural(cap, 'reply', 'replies')} a day go on their own, one per conversation."
        except Exception:
            footer = ""
    return Section(SECTION_AS_YOU, title, rows=rows, notes=notes, footer=footer)


# --------------------------------------------------------------------------- #
# As the Executive
# --------------------------------------------------------------------------- #


_LEVEL_WORDS: dict[str, str] = {"ask": "Careful", "outsiders": "Balanced", "trust": "Hands-off"}


def _in_training(person_id: int) -> bool:
    """Whether ``person_id``'s own In training is on (``delegation.training``).
    Unreadable is off, as the gate reads it."""
    from openexecutive.delegation import training

    try:
        return training.in_training(person_id)
    except Exception:
        logger.warning("may_do: couldn't read In training", exc_info=True)
        return False


def _lead_in_training() -> bool:
    """Whether the Executive's own In training is on (``take_the_lead.get``)."""
    from openexecutive.orchestrator import take_the_lead

    try:
        return take_the_lead.get(take_the_lead.SCOPE_EXECUTIVE).training
    except Exception:
        logger.warning("may_do: couldn't read As me's In training", exc_info=True)
        return False


def _level() -> str:
    """The company's Approvals level: ``outside_mode``'s token. Unreadable
    is Careful's."""
    from openexecutive.orchestrator import outside_mode

    try:
        return outside_mode.get_mode()
    except Exception:
        logger.warning("may_do: couldn't read the Approvals level", exc_info=True)
        return outside_mode.ASK


def as_the_executive(*, is_principal: bool) -> Section:
    """What the Executive does in its own name, on its own: Approvals
    decides each step (``take_the_lead._gated``)."""
    title = "As the Executive, on its own"
    if not is_principal:
        return Section(
            SECTION_EXECUTIVE, title, available=False,
            unavailable="Only the account owner sees what the Executive does on its own.",
        )
    level = _level()
    words = _LEVEL_WORDS.get(level, "Careful")
    hands_off = level == "trust"
    what = "Messages, bookings, workflows and changes in connected apps it thinks of."
    if _lead_in_training():
        lead = Row(
            "lead", what, "waits", "Waits for you",
            "In training: each one comes to you as a card first, until you allow it.", SETTING_TAKE_THE_LEAD,
        )
    else:
        lead = Row(
            "lead", what, "goes" if hands_off else "waits", "Goes out" if hands_off else "Waits for you",
            f"Approvals is on {words}." + ("" if hands_off else " Each one comes to you as an Approve card."),
            SETTING_APPROVALS,
        )
    rows: list[Row] = [
        lead,
        Row("to_you", "Messages to you.", "goes", "Goes out", "You're who it would ask.", SETTING_NONE),
        _meetings_in_chat(),
    ]
    return Section(SECTION_EXECUTIVE, title, rows=rows)


def _meetings_in_chat() -> Row:
    """A meeting someone asks for in chat
    (``calendar_tools.handle_create_calendar_event``): the owner asking books
    it; a teammate's goes by Approvals, like their other sends."""
    from openexecutive.memory.workspace_settings import get_workspace

    what = "Meetings you or a teammate ask it to book in chat."
    level = _level()
    words = _LEVEL_WORDS.get(level, "Careful")
    if get_workspace().mode == "solo" or level == "trust":
        return Row(
            "meetings_chat", what, "goes", "Books it",
            "It books what you ask for." if level != "trust" else "Approvals is on Hands-off.",
            SETTING_APPROVALS,
        )
    return Row(
        "meetings_chat", what, "waits", "Books yours, a teammate's waits",
        f"It books what you ask for. Approvals is on {words}, so a teammate's waits for approval.",
        SETTING_APPROVALS,
    )


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #


def departments() -> Section:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments.authority import department_level
    from openexecutive.memory.workspace_settings import get_workspace

    title = "For each department"
    try:
        states = dept_registry.list_states()
    except Exception:
        logger.warning("may_do: couldn't read the departments", exc_info=True)
        states = []
    rows: list[Row] = []
    for state in states:
        level = str(department_level(state.config.slug))
        outcome: Outcome = "goes" if level == "auto_execute" else "waits"
        then = {
            "auto_execute": "Acts on its own",
            "propose_only": "Waits for its approver",
            "escalate": "Waits, marked urgent",
        }.get(level, "Waits for its approver")
        rows.append(Row(
            f"department:{state.config.slug}", state.config.title, outcome, then,
            f"Approvals is on {DEPARTMENT_LEVEL_NAMES.get(level, level)}.", SETTING_APPROVALS,
        ))
    solo = get_workspace().mode == "solo"
    footer = (
        "Approvals covers each department's scheduled messages and alerts"
        + (". Check-ins don't run in a personal workspace" if solo else ", its check-ins, and what its team members ask for in chat")
        + "."
    )
    return Section(SECTION_DEPARTMENTS, title, rows=rows, footer=footer)


# --------------------------------------------------------------------------- #
# All of it
# --------------------------------------------------------------------------- #


def summary(person_id: int | None, *, can_have_it: bool, is_principal: bool) -> MayDo:
    """The whole page for one viewer."""
    from openexecutive.delegation.handle_it import signing_ok
    from openexecutive.scheduler.pause import is_paused

    try:
        paused = is_paused()
    except Exception:
        paused = True
    signing = signing_ok()
    you = (
        as_you(person_id, can_have_it=can_have_it, signing=signing, is_principal=is_principal)
        if person_id is not None
        else as_you(0, can_have_it=False, signing=signing)
    )
    return MayDo(paused=paused, sections=[you, as_the_executive(is_principal=is_principal), departments()])
