"""Deliver a wait-for-human gate's question, and record where to look for the answer.

Before this module, a workflow that paused at an approval gate was a dead end
(#136). ``dynamic.py`` built the ``WaitForHumanEvent`` with no ``channel``,
``channel_ref`` or ``outbound_message_id``, and the two checkpoint sites wrote
that empty state straight to ``workflow_runs.state_json``. Consequences:

* Nobody was ever *asked*. The run sat at ``awaiting_human`` until it timed
  out, and the only place it surfaced was the /today dashboard.
* Even if the approver had guessed, the inbound resolver could not match
  their reply: tier 2 filters candidates on ``state["channel"] == channel``,
  which was always ``""``.

So the primitive existed on paper and resolved nothing on any channel. This
module fills in the routing fields and — when the approver is not the person
already in the conversation — actually sends the question.

Two modes:

1. **Chat-origin self-approval.** A person launched the workflow from a chat
   channel and is themselves the approver. Sending them a separate DM would be
   absurd — the Executive is about to put the question in its own reply. Record
   the session and channel so their next message in that conversation resolves
   the gate, and send nothing.

2. **Out-of-band.** A web ``/jobs`` run, a scheduled run, or a gate addressed
   to someone other than the launcher. Delegate to
   ``schedule_tools.handle_message_person``, which already does
   preferred-channel selection, per-channel id validation, fall-through on a
   failed send, and an alert fallback when nothing is reachable. Do not
   reimplement any of that here.

Delivery can legitimately not happen — the anti-spam guard suppresses
duplicates, rate-cap breaches and quiet-hours sends, and an unreachable person
falls back to an alert. In both cases the question is NOT sitting somewhere the
approver can reply to, so the routing fields stay empty and the caller reports
the real ``delivery`` state rather than implying someone was asked.

Never raises: a delivery problem must not fail the run that is trying to pause.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from openexecutive.workflows.wait_for_human import WaitForHumanEvent, normalize_channel

logger = logging.getLogger(__name__)

DeliveryStatus = Literal["self", "sent", "suppressed", "alerted", "failed"]


async def deliver_gate_question(
    event: WaitForHumanEvent,
    *,
    run_id: str,
    workflow_title: str = "",
) -> tuple[WaitForHumanEvent, DeliveryStatus]:
    """Return a routed copy of *event* plus how its question was delivered.

    The returned event is what the caller should checkpoint — it carries the
    ``channel`` / ``channel_ref`` / ``outbound_message_id`` / ``origin_session_id``
    the inbound resolver needs. The original is never mutated.
    """
    # --- mode 1: the approver is already in this conversation ---------- #
    try:
        from openexecutive.orchestrator.schedule_tools import current_session

        session = current_session.get()
    except Exception:  # pragma: no cover - defensive, contextvar lookup
        session = None

    origin_channel = str(getattr(session, "origin_channel", None) or "")
    if (
        session is not None
        and origin_channel
        and session_caller_id(session) == event.person_id
    ):
        return (
            event.model_copy(
                update={
                    "channel": normalize_channel(origin_channel),
                    "channel_ref": str(
                        getattr(session, "origin_channel_ref", None) or ""
                    ),
                    "origin_session_id": str(
                        getattr(session, "session_id", None) or ""
                    ),
                    # No outbound message to reference — the question rides in
                    # the Executive's own reply, so tier 2 does the matching.
                    "outbound_message_id": "",
                    "delivery": "self",
                }
            ),
            "self",
        )

    # --- mode 2: ask them wherever they actually are -------------------- #
    question = _compose_question(
        event,
        workflow_title=workflow_title,
        launched_by=await _launcher_name(session),
    )
    try:
        from openexecutive.orchestrator.schedule_tools import handle_message_person

        raw = await handle_message_person(
            {"person_id": event.person_id, "text": question}
        )
        parsed = json.loads(raw)
    except Exception:
        logger.exception(
            "gate_delivery: could not ask person %s about run %s",
            event.person_id,
            run_id,
        )
        return event.model_copy(update={"delivery": "failed"}), "failed"

    status = str(parsed.get("status") or "")
    if status == "sent":
        sent_channel = normalize_channel(str(parsed.get("channel") or ""))
        sent_ref = str(parsed.get("channel_ref") or "")
        return (
            event.model_copy(
                update={
                    "channel": sent_channel,
                    "channel_ref": sent_ref,
                    "outbound_message_id": str(parsed.get("message_id") or ""),
                    "delivery": "sent",
                    # `handle_message_person` always sends a 1:1 DM, so the
                    # answer belongs in that DM. Without this the gate had no
                    # session at all and matched on (person, channel) alone —
                    # so an unrelated @mention in a public channel resolved it
                    # AND got the acknowledgement, workflow title and all,
                    # posted where everyone could read it.
                    "origin_session_id": _dm_session_id(sent_channel, sent_ref),
                }
            ),
            "sent",
        )

    if status == "alerted":
        # The question is on their briefing board, not in a conversation they
        # can reply to. Leaving the routing fields empty is deliberate: a
        # channel we did not actually send on must not be advertised as one
        # where a reply will be matched.
        logger.info(
            "gate_delivery: run %s fell back to an alert for person %s",
            run_id,
            event.person_id,
        )
        return event.model_copy(update={"delivery": "alerted"}), "alerted"

    if status == "suppressed":
        logger.info(
            "gate_delivery: run %s send suppressed (%s) for person %s",
            run_id,
            parsed.get("reason", "unknown"),
            event.person_id,
        )
        return event.model_copy(update={"delivery": "suppressed"}), "suppressed"

    logger.warning(
        "gate_delivery: run %s delivery returned %s for person %s",
        run_id,
        parsed.get("error") or status or "no status",
        event.person_id,
    )
    return event.model_copy(update={"delivery": "failed"}), "failed"


# How each adapter names the 1:1 conversation with a person. Must stay in step
# with the adapters' own session-id schemes (`_slack_session_id`,
# `discord_bot._compute_session_id`, telegram's `telegram:{chat_id}`).
_DM_SESSION_PATTERNS = {
    "slack": "slack:dm:{ref}",
    "discord": "discord:dm:{ref}",
    "telegram": "telegram:{ref}",
}


def _dm_session_id(channel: str, channel_ref: str) -> str:
    """The session id of the DM a gate question was delivered into, if known.

    Returns "" for a channel we have no pattern for, which leaves the gate
    matching on channel alone — the previous behaviour, not a regression.
    """
    pattern = _DM_SESSION_PATTERNS.get(channel)
    if not pattern or not channel_ref:
        return ""
    return pattern.format(ref=channel_ref)


def session_caller_id(session: object) -> int | None:
    """The person id behind the current chat session, if it is known."""
    for attr in ("caller_person_id", "person_id"):
        value = getattr(session, attr, None)
        # `bool` is an `int` subclass, and True would compare equal to
        # person_id 1.
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


async def _launcher_name(session: object) -> str:
    """Who set this workflow running, when we can tell.

    Any rostered chat user can launch a workflow, so an approval question can
    arrive at the principal on someone else's initiative. Naming the launcher
    is the difference between a sign-off request and an anonymous one that
    merely looks official.
    """
    person_id = session_caller_id(session)
    if person_id is None:
        return ""
    try:
        from openexecutive.people.store import get_person

        # Offloaded: this runs on the application event loop, and every other
        # SQLite touch on this path is already off it.
        person = await asyncio.to_thread(get_person, person_id)
    except Exception:
        logger.exception("gate_delivery: could not resolve launcher %s", person_id)
        return ""
    return getattr(person, "full_name", "") or "" if person else ""


def _compose_question(
    event: WaitForHumanEvent, *, workflow_title: str, launched_by: str = ""
) -> str:
    """The message the approver receives. Plain text — no channel has buttons."""
    lines = [event.question.strip() or "A workflow needs your sign-off."]
    if workflow_title and launched_by:
        lines.append(
            f"\n(From the '{workflow_title}' workflow, started by {launched_by}.)"
        )
    elif workflow_title:
        lines.append(f"\n(From the '{workflow_title}' workflow.)")
    elif launched_by:
        lines.append(f"\n(Started by {launched_by}.)")
    if event.context_summary:
        lines.append(f"\n{event.context_summary.strip()}")
    if event.expected_reply_shape == "approve_reject":
        lines.append("\nReply here to approve or decline.")
    else:
        lines.append("\nReply here with your answer.")
    return "\n".join(lines)


__all__ = ["DeliveryStatus", "deliver_gate_question"]
