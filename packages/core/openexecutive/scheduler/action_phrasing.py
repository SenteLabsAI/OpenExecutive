"""Phrase a proposed scheduled action as a concrete, first-person executive action.

The briefing UI renders this string directly after the label "If you
approve:" (see ``packages/ui/src/components/Briefing.tsx``), so the value is a verb
phrase describing what the Executive will *do* on approval — the delivery
(verb + what + to whom + how). It deliberately complements the card's
headline/body (which already carry the *content*) rather than restating them, and
it never reads as a circular "review and approve this action" instruction.

Every argument degrades gracefully: an unknown ``kind`` or ``channel``, or a
missing ``target_name``/``department``, still yields a sensible, non-empty
sentence so a newly-added ``ScheduledAction`` taxonomy can't produce empty UI.
"""
from __future__ import annotations

# kind → the noun the Executive is sending/doing. Mirrors the taxonomy in
# ``openexecutive.memory.episodic.ScheduledAction``. Unknown kinds fall back to
# "action".
KIND_LABEL: dict[str, str] = {
    "dept_cadence": "check-in",
    "proactive_nudge": "nudge",
    "ad_hoc": "follow-up",
    "awaiting_human": "workflow step",
    "principal_brief_morning": "morning brief",
    "principal_brief_eod": "end-of-day digest",
    "principal_weekly_review": "weekly review",
}

# channel → trailing "how / to whom" phrase. ``department_channel`` is handled
# separately because it folds in the department name; channels not listed here
# (e.g. an internal/unknown channel) simply contribute no destination phrase.
CHANNEL_PHRASE: dict[str, str] = {
    "slack_dm": "via Slack DM",
    "discord_dm": "via Discord DM",
    "telegram": "via Telegram",
    "email": "by email",
    "company_broadcast": "to the whole company",
}


def describe_executive_action(
    *,
    kind: str,
    channel: str,
    department: str | None = None,
    target_name: str | None = None,
    urgent: bool = False,
) -> str:
    """Return a concrete action phrase for the "If you approve:" block.

    Examples::

        describe_executive_action(kind="dept_cadence", channel="department_channel",
                                  department="sales")
        # -> "Send this check-in to the sales channel."

        describe_executive_action(kind="proactive_nudge", channel="slack_dm",
                                  target_name="Dana Lopez")
        # -> "Send this nudge to Dana Lopez via Slack DM."

        describe_executive_action(kind="ad_hoc", channel="email",
                                  target_name="Dana Lopez", urgent=True)
        # -> "Send this follow-up to Dana Lopez by email right away."
    """
    noun = KIND_LABEL.get(kind, "action")

    # Where it goes: prefer a named person (with the medium, when it's a DM /
    # email), then the department channel, then a bare channel phrase, then
    # nothing.
    dest = ""
    if target_name:
        dest = f"to {target_name}"
        medium = CHANNEL_PHRASE.get(channel, "")
        if medium.startswith(("via", "by")):
            dest = f"{dest} {medium}"
    elif channel == "department_channel":
        dept = (department or "").replace("_", " ").strip()
        dest = f"to the {dept} channel" if dept else "to the department channel"
    else:
        dest = CHANNEL_PHRASE.get(channel, "")

    sentence = f"Send this {noun}"
    if dest:
        sentence += f" {dest}"
    if urgent:
        sentence += " right away"
    return sentence + "."
