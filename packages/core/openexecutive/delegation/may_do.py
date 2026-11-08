"""What it does for you: everything that goes out without asking, and why.

What it does for you, beside each Delegate tab, is read-only. It answers one
question for the person looking at it, with their settings right now: what
will the Executive send or change without asking anyone, who does it go out
as, and which setting allows it. Each row's ``what`` is one sentence of what
it does; ``why`` says which setting decides it. Every answer is
read from the same code the gates use, so the page can't say something the
gates don't do:

- **As you** (the caller's own, ``can_delegate``): Write drafts as me
  (``settings.is_enabled``), Draft replies to my inbox (``inbox.get_watch``),
  Handle it for me and its level (``handle_it.get``, ``handle_it.RULES``),
  Everything (``handle_it.leading``, Take the lead as you), what each
  setting has in training and what it has learned (``training``), and the
  person's own rules (``take_the_lead.list_rules``). The checks in
  ``handle_it.refusal`` and ``follow_up_refusal`` are the source of each row.
- **As the Executive** (the principal's alone): what it does on its own,
  which Approvals decides (``take_the_lead._gated``): Hands-off goes,
  otherwise an Approve card. Meetings asked for in chat
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
SETTING_HANDLE_IT = "handle_it"
SETTING_TAKE_THE_LEAD = "take_the_lead"
SETTING_PAUSE = "pause"
# Approvals (Careful / Balanced / Hands-off), on Settings → Your Executive.
SETTING_APPROVALS = "approvals"
SETTING_NONE = ""
_DEPARTMENT_PREFIX = "department:"

SECTION_AS_YOU = "as_you"
SECTION_EXECUTIVE = "executive"
SECTION_DEPARTMENTS = "departments"

# Handle it's levels, by the names on its card (``HandleItCard.tsx``).
LEVEL_NAMES: dict[str, str] = {
    "careful": "Easy ones",
    "balanced": "People I know",
    "bold": "Most mail",
    "lead": "Everything",
}

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

_SENSITIVE_WORDS = "money, contracts, legal, hiring, pay, the board, the press, health or passwords"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def as_you(person_id: int, *, can_have_it: bool, signing: bool, is_principal: bool = False) -> Section:
    """What goes out as ``person_id``, from their own mailbox. The company's
    rules are named only to the principal, as ``GET /take-the-lead`` does;
    anyone else is told only how many there are."""
    from openexecutive.delegation import handle_it, inbox, training
    from openexecutive.delegation.settings import is_enabled
    from openexecutive.orchestrator import take_the_lead

    title = "As you, from your own mailbox"
    if not can_have_it:
        return Section(
            SECTION_AS_YOU, title, available=False,
            unavailable="Act as me isn't available to you here. The owner of this Open Executive can turn it on "
            "for team members.",
        )

    drafts_on = is_enabled(person_id)
    watching = drafts_on and inbox.get_watch(person_id).enabled
    settings = handle_it.get(person_id)
    handling = watching and settings.enabled
    trained = training.get(person_id)
    learned = training.learned(person_id)
    allowed: dict[str, int] = {}
    for a in learned:
        setting = training.setting_of(a.key)
        allowed[setting] = allowed.get(setting, 0) + 1
    replies_trained = handling and trained.replies
    follow_ups_trained = handling and trained.follow_ups
    lead = handling and not trained.replies and handle_it.leading(person_id)
    follow_lead = handling and not trained.follow_ups and handle_it.leading(person_id)
    rules = handle_it.RULES.get(settings.mode, handle_it.RULES[handle_it.DEFAULT_MODE])
    level = LEVEL_NAMES["lead" if lead else settings.mode]
    sends = handling and signing

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

    def reply_row(row_id: str, what: str, stranger: bool) -> Row:
        if not watching:
            why = (
                "Draft replies to my inbox is off." if drafts_on
                else "Write drafts as me is off. Replies to your inbox need it."
            )
            return Row(row_id, what, "off", "Off", why, SETTING_ACT_AS_ME)
        if not settings.enabled:
            return Row(
                row_id, what, "waits", "Waits for you",
                "Each one waits on a card for you to send. Handle it for me is off.",
                SETTING_HANDLE_IT,
            )
        if not signing:
            return Row(
                row_id, what, "waits", "Waits for you",
                "Signed sign-ins are off on this server, so nothing goes out as you.",
                SETTING_HANDLE_IT,
            )
        if replies_trained:
            n = allowed.get(training.REPLIES, 0)
            return Row(
                row_id, what, "waits", "Waits for you",
                "Handle it for me is in training. "
                + (f"It answers the {_plural(n, 'sender')} you ticked on its own." if n
                   else "Tick “on its own next time” on a reply and it answers that sender on its own."),
                SETTING_HANDLE_IT,
            )
        if lead:
            return Row(
                row_id, what, "goes", "Goes out",
                "Handle it for me is at Everything. It decides, within the limits below.",
                SETTING_HANDLE_IT,
            )
        if stranger and not rules.strangers:
            return Row(
                row_id, what, "waits", "Waits for you",
                f"Handle it for me is at {level}. Most mail sends these.",
                SETTING_HANDLE_IT,
            )
        if settings.mode == handle_it.MODE_CAREFUL:
            why = f"Short ones, when it's very sure. Handle it for me is at {level}."
        else:
            why = f"When the email needs a simple answer. Handle it for me is at {level}."
        return Row(row_id, what, "goes", "Goes out", why, SETTING_HANDLE_IT)

    rows.append(reply_row("replies_known", "Replies to people you know.", stranger=False))
    rows.append(reply_row("replies_strangers", "Replies to people you don't know yet.", stranger=True))

    # Follow-ups on their own unanswered emails.
    what = "Follow-ups when your email goes unanswered."
    if not handling:
        rows.append(Row(
            "follow_ups", what, "off" if not watching else "waits",
            "Off" if not watching else "Waits for you",
            "Draft replies to my inbox is off." if not watching
            else "Each one waits for you to send. Handle it for me is off.",
            SETTING_HANDLE_IT if watching else SETTING_ACT_AS_ME,
        ))
    elif not signing:
        rows.append(Row(
            "follow_ups", what, "waits", "Waits for you",
            "Signed sign-ins are off on this server, so nothing goes out as you.", SETTING_HANDLE_IT,
        ))
    elif follow_ups_trained:
        n = allowed.get(training.FOLLOW_UPS, 0)
        rows.append(Row(
            "follow_ups", what, "waits", "Waits for you",
            "Handle it for me is in training. "
            + (f"It follows up on its own with the {_plural(n, 'group')} of people you ticked." if n
               else "Tick “on its own next time” on a follow-up and it sends those on its own."),
            SETTING_HANDLE_IT,
        ))
    elif follow_lead:
        rows.append(Row(
            "follow_ups", what, "goes", "Goes out",
            "Handle it for me is at Everything.", SETTING_HANDLE_IT,
        ))
    elif not rules.follow_up_to:
        rows.append(Row(
            "follow_ups", what, "waits", "Waits for you",
            f"It writes them and you send them. Handle it for me is at {level}.", SETTING_HANDLE_IT,
        ))
    else:
        who = "your team, your contacts and anyone you've written to" if "correspondent" in rules.follow_up_to \
            else "your team and your contacts"
        rows.append(Row(
            "follow_ups", what, "goes", "Goes out",
            f"To {who}. Handle it for me is at {level}.", SETTING_HANDLE_IT,
        ))

    # Links and amounts.
    if sends and not replies_trained:
        if lead:
            rows.append(Row(
                "links", "Replies with a link in them.", "waits", "Waits for you",
                "These wait at Everything, because a link is what a phishing email carries.",
                SETTING_HANDLE_IT,
            ))
        elif rules.details_confidence is not None:
            rows.append(Row(
                "links", "Replies with a link or an amount in them.", "goes", "Goes out",
                f"When it's very sure. Handle it for me is at {level}.", SETTING_HANDLE_IT,
            ))
        else:
            rows.append(Row(
                "links", "Replies with a link or an amount in them.", "waits", "Waits for you",
                f"Handle it for me is at {level}. Most mail sends these when it's very sure.",
                SETTING_HANDLE_IT,
            ))

    # The topics that always wait.
    if sends:
        rows.append(Row(
            "sensitive", f"Anything about {_SENSITIVE_WORDS}.", "always_waits", "Always waits",
            "This one stays on. Replies go to people already on the email.",
        ))

    # Their own rules, which hold only at Everything.
    if lead or follow_lead:
        try:
            own = take_the_lead.list_rules([take_the_lead.person_scope(person_id)])
            company = take_the_lead.list_rules([take_the_lead.SCOPE_COMPANY])
        except Exception:
            logger.warning("may_do: couldn't read the rules", exc_info=True)
            own, company = [], []
        if own or company:
            shown = [*own, *company] if is_principal else own
            why = "These always wait at Everything."
            if company and not is_principal:
                why += f" Your company has {_plural(len(company), 'rule')}; the owner can see them."
            rows.append(Row(
                "rules", "Replies that match your rules or your company's.", "always_waits", "Waits for you",
                why, SETTING_HANDLE_IT,
                items=tuple(_rule_label(r.kind, r.value) for r in shown),
            ))

    # Messages and meetings it suggests from their mail.
    if drafts_on:
        if trained.actions:
            n = allowed.get(training.ACTIONS, 0)
            why = (
                "Suggested actions are in training. "
                + (f"The {_plural(n, 'kind')} you ticked go on their own."
                   if n else "Tick “on its own next time” on a card and that kind goes on its own.")
            )
            rows.append(Row(
                "actions", "Messages and invites it suggests from your mail.",
                "goes" if n and signing else "waits", "Goes out when allowed" if n and signing else "Waits for you",
                why, SETTING_ACT_AS_ME,
            ))
        elif trained.actions_without_asking:
            rows.append(Row(
                "actions", "Messages and invites it suggests from your mail.",
                "goes" if signing else "waits", "Goes out" if signing else "Waits for you",
                "Suggested actions are On. New contacts, anyone added in the last week, money and links "
                "wait for you." if signing
                else "Signed sign-ins are off on this server, so nothing goes out as you.",
                SETTING_ACT_AS_ME,
            ))
        else:
            rows.append(Row(
                "actions", "Messages and invites it suggests from your mail.", "waits", "Waits for you",
                "Each one waits on a card. Suggested actions are set to Always ask.", SETTING_ACT_AS_ME,
            ))

    if lead or follow_lead:
        notes.append(
            "Everything uses Take the lead as you. It needs Draft replies to my inbox and Write drafts as me. "
            "Turning either off, or putting Handle it for me in training, turns Everything off."
        )
    elif handling:
        notes.append(
            "Handle it for me needs Draft replies to my inbox and Write drafts as me. Turning either off "
            "turns Handle it for me off too."
        )
    if handling and not signing:
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


def _rule_label(kind: str, value: str) -> str:
    from openexecutive.orchestrator import take_the_lead

    if kind == take_the_lead.RULE_DOMAIN:
        return f"Anyone at {value}"
    if kind == take_the_lead.RULE_WORDS:
        return f"Words: {value}"
    if kind == take_the_lead.RULE_AMOUNT:
        return f"Amounts of {value} or more"
    return value


# --------------------------------------------------------------------------- #
# As the Executive
# --------------------------------------------------------------------------- #


_LEVEL_WORDS: dict[str, str] = {"ask": "Careful", "outsiders": "Balanced", "trust": "Hands-off"}


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
    rows: list[Row] = [
        Row(
            "lead", "Messages, bookings, workflows and changes in connected apps it thinks of.",
            "goes" if hands_off else "waits", "Goes out" if hands_off else "Waits for you",
            f"Approvals is on {words}."
            + ("" if hands_off else " Each one comes to you as an Approve card."),
            SETTING_APPROVALS,
        ),
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
