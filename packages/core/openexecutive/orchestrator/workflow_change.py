"""A change to a saved workflow that waits for its person's tap.

Chat can't change a workflow a person already approved the tools of (one
switched on with action steps), nor one that reads or sends email as its
creator, by saving over it: the save would put tools nobody looked at under a
familiar name. So ``save_workflow`` stages such a change as a card instead
(decision class ``app_action``, hidden action ``change_workflow``) that spells
out every step it adds, removes or changes, tools included. Tapping Approve is
the person seeing the change, as turning it on from its review card is: the
change is saved and the workflow keeps its on/off state.

The card goes where the chat is: under the reply in the web chat, and with
Approve and Skip buttons in a chat app that proves who taps
(``integrations.channel_cards``). A chat that can show neither gets no card:
the person is asked to ask again where one shows. On the tap:

- the workflow must still be the version the card was made from, else
  nothing changes and the person is asked to look again;
- the new definition is validated again, tools included;
- one that reads or sends as its creator is saved only for that creator,
  verified (``workflow_email.save_refusal``): a signed web sign-in, or a tap
  a chat app proved was theirs.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

ACTION = "change_workflow"
# The card names at most this many changed steps, then "and N more".
_MAX_LINES = 8
_GOAL_CHARS = 220


def needs_card(existing: Any, defn: Any) -> bool:
    """Whether replacing ``existing`` with ``defn`` from chat waits for a tap:
    a switched-on workflow with action steps, or one that reads or sends as
    its creator (before or after)."""
    from openexecutive.delegation.workflow_email import as_creator
    from openexecutive.workflows.dynamic_models import ActionStepSpec

    if existing is None:
        return False
    approved = existing.is_active and any(isinstance(s, ActionStepSpec) for s in existing.steps)
    return bool(approved or as_creator(existing) or as_creator(defn))


def _tools(step: Any) -> list[str]:
    from openexecutive.workflows.action_step import plain_tool_name

    return [plain_tool_name(t) for t in getattr(step, "tools", None) or []]


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _what(step: Any) -> str:
    """What a step does, for the card."""
    kind = getattr(step, "kind", "")
    if kind == "action":
        return f"{_clip(step.goal, _GOAL_CHARS)} Tools: {', '.join(_tools(step)) or 'none'}."
    if kind == "specialist":
        return _clip(step.goal, _GOAL_CHARS)
    if kind == "approval_gate":
        return f"Asks person {step.person_id}: {_clip(step.question, _GOAL_CHARS)}"
    return _clip(getattr(step, "instructions", "") or "Writes the result.", _GOAL_CHARS)


def change_lines(old: Any, new: Any) -> list[str]:
    """One line per difference between two versions of a workflow, written
    from the definitions, so the card says exactly what a tap saves."""
    lines: list[str] = []
    before = {s.id: s for s in old.steps}
    after = {s.id: s for s in new.steps}
    for sid, step in after.items():
        was = before.get(sid)
        if was is None:
            lines.append(f"New step “{_clip(step.title, 80)}”: {_what(step)}")
            continue
        if was.model_dump() == step.model_dump():
            continue
        parts: list[str] = []
        if _tools(was) != _tools(step):
            added = [t for t in _tools(step) if t not in _tools(was)]
            removed = [t for t in _tools(was) if t not in _tools(step)]
            if added:
                parts.append("now uses " + ", ".join(added))
            if removed:
                parts.append("no longer uses " + ", ".join(removed))
        other = {k: v for k, v in step.model_dump().items() if k != "tools"}
        if other != {k: v for k, v in was.model_dump().items() if k != "tools"}:
            parts.append("now: " + _what(step).split(" Tools: ")[0])
        lines.append(f"Step “{_clip(step.title, 80)}”: " + "; ".join(parts))
    for sid, step in before.items():
        if sid not in after:
            lines.append(f"Removed step “{_clip(step.title, 80)}”")
    if [s.id for s in old.steps if s.id in after] != [s.id for s in new.steps if s.id in before]:
        lines.append("Steps run in a new order")
    if (old.cadence, old.cadence_person_id) != (new.cadence, new.cadence_person_id):
        lines.append(
            f"Schedule: {new.cadence or 'none'}"
            + (f", delivered to person {new.cadence_person_id}" if new.cadence else "")
        )
    if old.title != new.title:
        lines.append(f"Renamed to “{_clip(new.title, 80)}”")
    if not lines and old.model_dump(exclude={"is_active", "created_at", "updated_at"}) != new.model_dump(
        exclude={"is_active", "created_at", "updated_at"}
    ):
        lines.append("Its description or form fields change")
    if len(lines) > _MAX_LINES:
        lines = [*lines[:_MAX_LINES], f"and {len(lines) - _MAX_LINES} more changes"]
    return lines


def card_text(old: Any, new: Any) -> str:
    head = f"Change the workflow “{_clip(old.title, 80)}”"
    lines = change_lines(old, new)
    if not lines:
        return head + "\nNothing else changes."
    return head + "\n" + "\n".join(f"- {line}" for line in lines)


def _speaker() -> Any:
    from openexecutive.orchestrator.correction_tools import _speaker as speaker

    return speaker()


def may_read(name: str) -> str | None:
    """Why this turn may not read or change workflow ``name``, or None: it
    needs a verified speaker, in a conversation only they read, who is the
    account owner or the workflow's creator."""
    from openexecutive.orchestrator.app_control import _NOT_PRIVATE, _private_here
    from openexecutive.workflows.dynamic_store import get_owner

    person = _speaker()
    if person is None or getattr(person, "id", None) is None:
        return (
            "I can only do this with a saved workflow when I know who is asking: in the web "
            "app signed in, or your own chat app."
        )
    if not _private_here():
        return _NOT_PRIVATE
    if not getattr(person, "is_principal", False) and get_owner(name) != person.id:
        return "Only the account owner or the person who created this workflow can do that."
    return None


def may_stage(name: str) -> str | None:
    """Why this turn can't leave a change card for workflow ``name``, or None:
    as ``may_read``, in a chat that shows the card."""
    from openexecutive.integrations.channel_cards import card_here
    from openexecutive.orchestrator.app_control import _session

    refused = may_read(name)
    if refused is not None:
        return refused
    if not card_here(_session()):
        return (
            "I can't show the change for your OK in this chat. Ask me in the web app, or in "
            "your own chat with me on Telegram, Slack or Discord, and I'll leave it there for "
            "you to approve."
        )
    return None


def stage(defn: Any, existing: Any) -> dict[str, Any]:
    """Leave the card for changing ``existing`` into ``defn``. Returns the
    tool result for save_workflow."""
    from openexecutive.orchestrator import app_control, app_control_catalog

    app_control_catalog.install()  # where the hidden action is registered
    refused = may_stage(defn.name)
    if refused is not None:
        return {"error": refused}
    person = _speaker()
    text = card_text(existing, defn)
    value = json.dumps({
        "definition": defn.model_dump(mode="json", exclude={"is_active", "created_at", "updated_at"}),
        "base": existing.model_dump(mode="json", exclude={"is_active", "created_at", "updated_at"}),
    })
    decision_id = app_control.leave_card(person.id, ACTION, defn.name, value, existing.title, text)
    return {
        "status": "waiting_for_approval",
        "decision_id": decision_id,
        "card": text,
        "note": (
            "Nothing has changed yet. A card showing the change goes with this reply, with "
            "Approve and Skip: it is saved when they tap Approve, keeping the workflow on or "
            "off as it is. Tell them it is waiting for their OK."
        ),
    }


def _tapper(request: Any) -> Any:
    """Who tapped: a chat app's proven tap, else the request's own caller."""
    from openexecutive.api import caller as api_caller

    tap = getattr(getattr(request, "state", None), "chat_tap", None)
    return tap if tap is not None else api_caller.caller(request)


async def run(request: Any, name: str, value: str) -> str:
    """Carry out a change card on its person's tap (``app_control.carry_out``)."""
    from openexecutive.api.routes.workflows import apply_chat_change
    from openexecutive.orchestrator.app_control import Refused

    try:
        staged = json.loads(value)
    except ValueError:
        staged = None
    if not isinstance(staged, dict) or not isinstance(staged.get("definition"), dict):
        raise Refused("This card can't be read any more. Ask again in chat.")
    stored = await apply_chat_change(name, staged["definition"], staged.get("base"), _tapper(request))
    return f"Changed the workflow “{stored.title}”"


__all__ = ["ACTION", "card_text", "change_lines", "may_read", "may_stage", "needs_card", "run", "stage"]
