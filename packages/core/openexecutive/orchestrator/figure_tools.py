"""``keep_figure``: the Executive keeps a figure it worked out for the person.

The Executive decides, during the turn, which of the numbers it worked out
are lasting knowledge about the company, the way it decides everything else
in the turn: it knows what was asked, what the company does and what it read.
The tool only notes the figure. Once the reply is written,
``memory.worked_out.keep_turn_figures`` checks each noted figure against the
reply and what the turn read, and keeps what passes for this person alone.

Only a turn that is the speaker's own, in a conversation nobody else reads
(``worked_out._their_own_turn``), may keep one; the tool says so up front, so
the reply never claims a figure was kept when it can't be. It is never
offered to an unattended run, another app's turn or a turn private to the
principal by email (``schedule_tools``' withheld sets). Its dispatch audit row
is private (``executive._private_tool_row``): the figure may come from the
speaker's own mail.

The schema is static, so the cached tool prefix never moves.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

KEEP_FIGURE_TOOL: dict[str, Any] = {
    "name": "keep_figure",
    "description": (
        "Keep a figure you worked out for this person from what you read this "
        "turn, so a later conversation can start from it. Only lasting knowledge "
        "about their company that they will need again: its totals, balances and "
        "rates, prices and plans, targets and budgets, deal or contract terms, a "
        "customer's, supplier's or project's numbers, a date it committed to, an "
        "identifier it was issued. Judge by what they asked and what the company "
        "does; most turns keep nothing. Never for a personal errand or one-off "
        "lookup (their own shopping or orders, a price check, trivia), a live "
        "count you would look up again (sign-ups, a waitlist, open issues, "
        "followers), the status of your own follow-ups, tasks or approvals, a "
        "number repeated from today's brief or from figures you worked out "
        "before, something the person told you, an example or a guess. One "
        "figure per call. It is kept only if your reply states it, for this "
        "person alone; they can see and remove it under Memories."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": (
                    "What it is, in a few plain words, e.g. 'Pine View II net to owner'. "
                    "Never tool names or record ids."
                ),
            },
            "figure": {
                "type": "string",
                "description": "The value exactly as your reply writes it, e.g. '$330,241'.",
            },
            "source": {
                "type": "string",
                "description": (
                    "The document, email, report or system it came from, as the material "
                    "names it, e.g. 'DCA statement, March'."
                ),
            },
            "as_of": {
                "type": "string",
                "description": "YYYY-MM-DD the source is dated, when it says.",
            },
        },
        "required": ["subject", "figure", "source"],
    },
}

WORKED_OUT_TOOLS: list[dict[str, Any]] = [KEEP_FIGURE_TOOL]


def _keep_figure(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory import worked_out
    from openexecutive.orchestrator.schedule_tools import current_session

    refusal = worked_out.keep_refusal(current_session.get())
    if refusal is not None:
        return json.dumps({"error": refusal})
    fig = worked_out.check_figure(tool_input, reply=None, read=None, today=worked_out._now().date())
    if fig is None:
        return json.dumps({"error": (
            "Not kept: give a short subject in plain words, the figure with its number, and the "
            "document or system it came from, with no links, record ids, meeting codes or clock times."
        )})
    return json.dumps({
        "status": "noted",
        "figure": {"subject": fig.subject, "figure": fig.figure, "source": fig.source, "as_of": fig.as_of},
        "message": "Noted. It is kept once your reply states this figure.",
    })


async def handle_keep_figure(tool_input: dict[str, Any]) -> str:
    import asyncio

    # Off the event loop: the own-turn check reads the People list.
    return await asyncio.to_thread(_keep_figure, tool_input)


WORKED_OUT_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "keep_figure": handle_keep_figure,
}
