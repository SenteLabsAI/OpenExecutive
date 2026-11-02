"""The plan a long chat turn shows before it starts: every step, up front.

On work that takes several steps, the Executive calls ``plan_steps`` with the
whole plan in plain words and which step it is on, alongside the first calls
of that work, and calls it again whenever it moves to the next step (or the
plan changes). The loop answers the call itself (nothing runs) and sends the
web chat a ``plan`` event, which lists the steps with the done ones ticked,
the current one running and the rest still to do.

A quick answer never calls it, and keeps the one-line activity status. The
turn's per-round ``activity`` labels go on as before: the web chat shows the
newest under the current step, as what it is doing right now.
"""

from __future__ import annotations

import json
from typing import Any

PLAN_STEPS = "plan_steps"

# A plan longer than this is a list of calls, not of steps.
MAX_STEPS = 8
# One line in the step list.
MAX_STEP_CHARS = 80

PLAN_STEPS_TOOL: dict[str, Any] = {
    "name": PLAN_STEPS,
    "description": (
        "Show the person your plan when a request will take several steps "
        "(roughly three or more rounds of tools): call this with the whole plan, "
        "3 to 6 short steps in plain words the person would use (\"Read the last "
        "month of emails with Northwind\", \"Write the summary\"), never tool or "
        "system names, and `current` set to the step you are starting. Call it in "
        "the same response as that step's first tool calls, never on its own. Each "
        "time you move on to the next step, call it again with the same steps and "
        "the new `current`, alongside that step's calls; if what you found changes "
        "the plan, send the changed list. Skip it for a quick answer, a single "
        "lookup or a plain reply. Nothing runs: it only updates what the person sees."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The whole plan, in order, each step one short line.",
            },
            "current": {
                "type": "integer",
                "description": "Index (from 0) of the step you are working on now.",
            },
        },
        "required": ["steps", "current"],
    },
}


def _clean_steps(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    steps: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        line = " ".join(item.split())
        if not line:
            continue
        if len(line) > MAX_STEP_CHARS:
            line = line[: MAX_STEP_CHARS - 1].rstrip() + "…"
        steps.append(line)
    return steps[:MAX_STEPS]


# Said when the plan call came on its own: that cost a round with no work in it.
ALONE_NOTE = " Next time, make this call in the same response as the step's own calls."


def apply(
    tool_input: Any, previous: list[str] | None, *, alone: bool = False
) -> tuple[str, dict[str, Any] | None, list[str] | None]:
    """Answer one ``plan_steps`` call.

    Returns (the tool result for the model, the ``plan`` event for the chat or
    None, the turn's plan from now on). A call without usable steps keeps the
    plan the turn already showed, so moving on can't wipe it; with no plan yet
    it shows nothing and says why. ``alone`` (no other call in the round)
    adds a reminder to send it with the step's own calls.
    """
    data = tool_input if isinstance(tool_input, dict) else {}
    steps = _clean_steps(data.get("steps")) or (previous or [])
    if not steps:
        return json.dumps({"error": "Give the plan as a list of short steps."}), None, previous
    current = data.get("current")
    if isinstance(current, bool) or not isinstance(current, int):
        current = 0
    current = max(0, min(current, len(steps) - 1))
    event = {"type": "plan", "steps": steps, "current": current}
    result = f"Shown: step {current + 1} of {len(steps)}."
    return result + (ALONE_NOTE if alone else ""), event, steps
