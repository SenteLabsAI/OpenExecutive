"""The `<channel>` block every chat adapter puts in the user turn.

Issue #136: talking through Slack, the Executive solicited an approval it had
no way to complete, then failed with a generic error. Part of that was broken
plumbing (fixed elsewhere); part was that the model had no idea it was on
Slack rather than in the web app, and no idea which kinds of approval can
actually be completed from a chat channel.

This module is the single place that answer lives, so a fifth adapter gets it
for free instead of growing its own prompt hack. The web app deliberately does
NOT set this block — there the model is already in the surface that owns the
buttons.

Caching note: the caller puts the returned text in the **user turn**. It must
never go in a `cache_control` system block — it varies per request and would
invalidate the prompt prefix on every turn (see `## Prompt Caching` in
CLAUDE.md).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Display names for the inbound channel keys the adapters use.
_CHANNEL_LABELS = {
    "slack": "Slack",
    "discord": "Discord",
    "telegram": "Telegram",
    "google_chat": "Google Chat",
    "email": "email",
}

# Channels whose adapter actually calls the wait-for-human inbound resolver.
# Google Chat and email do not, so telling the model a sign-off "can be
# answered here" on those would be exactly the overclaim this module exists to
# stop. Keep this in step with the `resolve_and_acknowledge` call sites.
_RESOLVER_CHANNELS = frozenset({"slack", "discord", "telegram"})


def build_channel_context_block(
    channel: str, *, ui_base_url: str | None = None
) -> str:
    """Render the `<channel>` body for an inbound message on *channel*.

    Returns "" for an unknown channel so a new adapter can't accidentally
    assert something false about where approvals live.

    ``ui_base_url`` defaults to the configured one. Resolving it here rather
    than at each call site keeps the adapters to a single argument and means a
    settings problem degrades to a block with no link — never an exception on
    the reply path, which is exactly the failure mode #136 was reported for.
    """
    label = _CHANNEL_LABELS.get(channel)
    if label is None:
        return ""

    if ui_base_url is None:
        try:
            from openexecutive.config import get_settings

            ui_base_url = str(getattr(get_settings(), "ui_base_url", "") or "")
        except Exception:  # pragma: no cover - settings should always load
            logger.warning("channel_context: could not resolve ui_base_url", exc_info=True)
            ui_base_url = ""

    briefing_url = f"{ui_base_url.rstrip('/')}/" if ui_base_url else ""
    where = (
        f"the briefing page ({briefing_url})" if briefing_url else "the briefing page"
    )

    if channel in _RESOLVER_CHANNELS:
        signoff = (
            "- A workflow sign-off you are explicitly waiting on CAN be "
            "answered here: their reply in this conversation is recorded "
            "against it.\n"
        )
    else:
        signoff = (
            f"- You cannot record a workflow sign-off from {label}. If one is "
            "waiting on them, say where it is waiting and do not ask them to "
            "approve it here.\n"
        )

    return (
        f"You are talking to this person over {label}, not in the Open "
        "Executive web app. They cannot see the app's cards, buttons, or "
        "panels in this conversation — only the text you send back.\n"
        "\n"
        "What that means for approvals:\n"
        f"- Briefing proposals and alerts are approved or dismissed on {where}. "
        "You may clear one from here only when you are acting on an alert_id "
        "the system gave you in a <briefing> block or a "
        "[Discuss mode — alert_id=N] primer. If you do not have a trustworthy "
        "alert_id, say the proposal needs to be actioned on the briefing page "
        "and point them there — do not imply that replying here approves it.\n"
        "- Proposals that book something (a meeting, a calendar hold) can ONLY "
        f"be approved on {where}. You have no tool for those. Say so plainly.\n"
        f"{signoff}"
        "\n"
        "Never ask for a confirmation you cannot act on. If the action needs "
        "the web app, say that in the same breath as asking — don't invite a "
        "'yes' you will have to walk back."
    )


def attach_briefing_context(session: object, *, is_dm: bool, person: object) -> str:
    """Return the open-alert digest for this turn, and record its ids.

    Gated to the principal's DMs on every channel: the board is company-wide,
    so pulling it into a shared channel would leak every open item to everyone
    in it. The live board the server derived is recorded on the session, which
    is what lets `ack_alert` refuse any id outside it — prompt wording alone is
    not a control. Note that is the whole live board, not only the ids this
    block printed; an unprinted live card is still ackable.

    Shared rather than repeated per adapter: `ack_alert`'s guard trips on
    EVERY session, so an adapter that renders this block without populating the
    trusted set would have the tool permanently refuse every id. The rendering
    and the recording therefore happen together, in
    `briefing.context.render_and_trust`, which the web chat route also uses.

    A non-DM or non-principal turn returns "" WITHOUT recording anything, so
    the trusted set stays empty and `ack_alert` refuses — correct, since the
    board is company-wide and was never shown to them.

    Synchronous SQLite — call it via ``asyncio.to_thread``. Never raises.
    """
    if not is_dm or not getattr(person, "is_principal", False):
        return ""
    try:
        from openexecutive.briefing.context import render_and_trust

        return render_and_trust(session)
    except Exception:
        logger.exception("channel_context: could not build the briefing block")
        return ""


__all__ = ["attach_briefing_context", "build_channel_context_block"]
