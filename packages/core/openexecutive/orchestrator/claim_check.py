"""Do what it says: a reply may not claim a change this turn didn't make.

The Executive sometimes answers "Done", "Removed it" or "I'll treat it as
closed" without calling the tool that would make it so, and the person only
finds out later, when the brief still lists the thing. So before a turn ends,
its last reply is checked against the tool calls the turn made:

1. A cheap word check (``CLAIM_RE``): a reply that claims no change ("Here are
   the numbers…") ends the turn as before, with no model call.
2. Otherwise one small forced-tool call on ``ROUTING_MODEL`` reads the
   person's message, the reply and the turn's tool calls (name, what each was
   given, and the start of each result) and says whether the reply claims a change this turn that
   none of those calls made. A claim about an earlier turn, a plan for later
   that the reply doesn't present as set up, or a change a call did make (or
   put up for approval) is not one.
3. When it finds one, the claim must appear word for word in the reply (code
   check), or it is ignored. The loop then gets one more round with
   ``nudge(claim)``: do it now with the right tool, or tell the person
   plainly it couldn't be done. Once a turn, so a loop can't form.

Never raises and never blocks a reply: a failure, a timeout or a missing
setting ends the turn as before. ``CLAIM_CHECK_ENABLED`` turns it off.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Words a reply uses to say something changed. Lexical on purpose: it only
# decides whether the model call runs, so a miss costs a check, not a reply.
CLAIM_RE = re.compile(
    r"\b(?:done|did it|removed|deleted|dropped|closed|cancel+ed|marked|"
    r"updated|changed|saved|noted|added|set (?:up|it|a)|scheduled|booked|"
    r"sent|moved|archived|cleared|fixed|taken care of|handled|treat(?:ed|ing)? "
    r"(?:it|this|that|the)|i'?ve|i have|i'?ll remind|will remind|reminder (?:is )?set|"
    r"won'?t (?:show|appear|include)|no longer|from now on|going forward)\b",
    re.IGNORECASE,
)

MAX_REPLY_CHARS = 3000
MAX_MESSAGE_CHARS = 2000
MAX_CALLS = 20
CALL_RESULT_CHARS = 400
# What each call was asked to do, shown next to its name: "moved the total
# to row 10" is in what a sheet write was given, not in its one-line result.
CALL_INPUT_CHARS = 600
CLAIM_MAX_CHARS = 200
_MAX_TOKENS = 300
_TIMEOUT_S = 20.0

_SYSTEM = (
    "You check an assistant's reply against what it actually did. You get the "
    "person's message, the assistant's reply, and every tool call the "
    "assistant made while writing that reply, with what each call was given "
    "and the start of each result. What a call wrote (cells, rows, formulas, "
    "text) is a change it made.\n\n"
    "Report an unbacked claim when the reply says or implies that it made a "
    "change during this reply (removed, closed, saved, scheduled, sent, "
    "updated, cancelled, set a reminder, will stop doing something, will "
    "treat something as done) and no tool call in the list made that change "
    "or put it up for approval. A tool call that failed or was refused does "
    "not back a claim.\n\n"
    "Not unbacked: a change one of the calls made; something it says was "
    "done earlier or by someone else; an offer or a question ('want me to "
    "remove it?'); advice; a plain answer to a question; saying it can't do "
    "something; a promise about how it will write or talk (shorter, more "
    "formal), which needs no tool.\n\n"
    "When there is one, copy the claim word for word from the reply (one "
    "short phrase). Record your verdict with the tool."
)

_TOOL: dict[str, Any] = {
    "name": "record_claim_check",
    "description": "Record whether the reply claims a change no tool call made.",
    "input_schema": {
        "type": "object",
        "properties": {
            "unbacked": {
                "type": "boolean",
                "description": "True when the reply claims a change this turn that no listed call made.",
            },
            "claim": {
                "type": "string",
                "description": "The claim, copied word for word from the reply. Empty when unbacked is false.",
            },
        },
        "required": ["unbacked", "claim"],
    },
}


def enabled() -> bool:
    try:
        from openexecutive.config import get_settings

        return bool(get_settings().claim_check_enabled)
    except Exception:
        return False


def may_claim(reply: str) -> bool:
    """Whether the reply uses a word that can claim a change."""
    return bool(reply.strip()) and CLAIM_RE.search(reply) is not None


def call_label(name: str, tool_input: Any) -> str:
    """``name`` with what the call was given, for ``render_calls``. A
    connected app's call is named by the tool it ran."""
    import json

    if name == "call_tool" and isinstance(tool_input, dict):
        name = str(tool_input.get("name") or name)
        tool_input = tool_input.get("arguments")
    try:
        given = json.dumps(tool_input, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        given = str(tool_input)
    given = " ".join(given.split())
    if len(given) > CALL_INPUT_CHARS:
        given = given[:CALL_INPUT_CHARS] + "…"
    return f"{name} {given}" if given not in ("", "null", "{}") else name


def render_calls(calls: list[tuple[str, str]]) -> str:
    if not calls:
        return "(no tool calls)"
    lines = []
    for name, result in calls[-MAX_CALLS:]:
        preview = " ".join(str(result or "").split())[:CALL_RESULT_CHARS]
        lines.append(f"- {name}: {preview}")
    return "\n".join(lines)


def _render_turn(message: str, reply: str, calls: list[tuple[str, str]]) -> str:
    return (
        f"<person_message>\n{message[-MAX_MESSAGE_CHARS:]}\n</person_message>\n\n"
        f"<assistant_reply>\n{reply[:MAX_REPLY_CHARS]}\n</assistant_reply>\n\n"
        f"<tool_calls>\n{render_calls(calls)}\n</tool_calls>"
    )


def _normalize(text: str) -> str:
    return " ".join(text.replace("’", "'").split()).lower()


def accept_claim(payload: dict[str, Any], reply: str) -> str | None:
    """The model's claim when it reported one and it is in the reply word
    for word (whitespace, case and curly quotes aside); None otherwise."""
    if payload.get("unbacked") is not True:
        return None
    claim = " ".join(str(payload.get("claim") or "").split())
    if not claim or len(claim) > CLAIM_MAX_CHARS:
        return None
    if _normalize(claim) not in _normalize(reply):
        return None
    return claim


async def _call_model(model: str, turn: str) -> dict[str, Any]:
    from openexecutive.audit.usage import log_model_usage
    from openexecutive.providers import get_provider

    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[{"role": "user", "content": turn}],
    )
    log_model_usage(response, model=model, actor="claim_check")
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == _TOOL["name"]:
            return block.input if isinstance(block.input, dict) else {}
    return {}


async def unbacked_claim(message: str, reply: str, calls: list[tuple[str, str]]) -> str | None:
    """The reply's claim of a change no call in ``calls`` made, or None.
    Never raises."""
    if not enabled() or not may_claim(reply):
        return None
    try:
        from openexecutive.config import get_settings

        model = get_settings().routing_model
        payload = await asyncio.wait_for(
            _call_model(model, _render_turn(message, reply, calls)), timeout=_TIMEOUT_S
        )
    except Exception:
        logger.warning("claim_check: check failed, reply goes out as is", exc_info=True)
        return None
    return accept_claim(payload, reply)


def nudge(claim: str) -> str:
    """The user-turn note for the extra round (never a cached block)."""
    return (
        "[Check before you finish] Your reply says “" + claim + "”, but no "
        "tool call in this reply made that change. Make it now with the right "
        "tool and say so in one short line, or, if you can't, tell the person "
        "plainly that it isn't done and why. Don't repeat the rest of your reply."
    )


def message_text(content: Any) -> str:
    """The text of a user message's content (a string, or text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def last_person_message(messages: list[dict[str, Any]]) -> str:
    """The last user message that the person wrote (not a tool-results turn)."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            continue
        text = message_text(content)
        if text.strip():
            return text
    return ""
