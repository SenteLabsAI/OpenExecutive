"""WaitForHuman workflow primitive — pause/resume across async human replies.

A workflow yields a ``WaitForHumanEvent`` to pause itself at an approval
gate. The caller (scheduler cadence runner or workflow HTTP runner) calls
``save_checkpoint`` and exits; the run sits in ``status='awaiting_human'``.

The ``run_resumer`` background loop watches for timeouts.  The inbound
resolver (Slack / Telegram / email hooks) calls ``apply_resolution`` when
a human replies, which stores the ``WaitForHumanResolution`` and advances
the run to ``status='resolved'``.

Resume
------
A gate that carries a :class:`WorkflowResumeState` is *resumable*: once the
decision is recorded, ``resumer._execute_resume`` re-enters the workflow at
the step after the gate and drives it to an artifact.  Nothing serialises a
Python generator frame — the state is the small, plain-JSON payload below,
and the engine replays its step loop from an index.

A gate with no resume state is *pause-only*: the decision is recorded and
the run stops there.  That is what every caller got before resume existed,
and it is still what a workflow yielding a bare ``WaitForHumanEvent`` gets.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.85


class HeldCall(BaseModel):
    """A tool call an action step held because it writes to a new target.

    Stored in the resume payload so that, if the owner approves, exactly this
    call — the tool and arguments the model chose — runs, rather than a fresh
    model turn that might choose differently.
    """

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # (argument key, value) pairs that were not yet approved.
    targets: list[tuple[str, str]] = Field(default_factory=list)


class WorkflowResumeState(BaseModel):
    """Everything needed to restart a paused workflow at the step after its gate.

    Deliberately NOT a serialised generator frame. The only values alive
    across a dynamic workflow's steps are its inputs (already persisted in
    ``workflow_runs.inputs``), a company-profile block that is recomputed on
    resume, the completed steps' outputs, and the loop cursor — all plain
    JSON, which is why re-entering a paused run needs no frame surgery.

    ``version`` and ``engine`` are checked on load so a payload written by an
    older build, or by a different engine, is REJECTED rather than
    misinterpreted into resuming at the wrong step.
    """

    version: Literal[1] = 1
    engine: str = "dynamic"
    workflow_name: str
    gate_step_id: str
    # Index of the gate itself, re-checked against the live definition on
    # resume — a definition edited during the pause can move it. The index to
    # continue AT is derived from this on resume, never stored: two copies of
    # the cursor could disagree, and the one an attacker controls would win.
    gate_step_index: int
    # Fingerprint of the ENTIRE step list as it stood when the gate was raised.
    #
    # Pinning only the gate is not enough. `upsert_definition` overwrites by
    # name, and any rostered chat user can call `save_workflow` (or
    # `PUT /workflows/custom/{name}`), so while a run sits parked someone can
    # keep the gate exactly where it is and replace every step AFTER it. The
    # approver then answers the question they were asked, and their sign-off is
    # recorded against work they never saw — which defeats the one thing an
    # approval gate exists to do. A mismatch refuses the resume.
    steps_fingerprint: str = ""
    # step_id -> (step title, output text) for every step completed before the
    # gate. Round-trips through JSON as a 2-array and back to a tuple.
    outputs: dict[str, tuple[str, str]] = Field(default_factory=dict)
    # What paused the run. "gate": an approval-gate step at gate_step_index.
    # "held_writes": the action step at gate_step_index held writes to new
    # targets (``held``); the step itself has run, and the owner's answer
    # decides whether those exact calls run before the next step. Payloads
    # written before this field existed parse as "gate".
    kind: Literal["gate", "held_writes"] = "gate"
    held: list[HeldCall] = Field(default_factory=list)
    # Scheduled runs DM their artifact on completion; a run that paused
    # completes in the resumer instead, which delivers it to this person.
    deliver_to_person_id: int | None = None
    # Values the run itself created before pausing (ids a write returned), so
    # its target check still trusts them after resuming.
    run_created: list[str] = Field(default_factory=list)


class WaitForHumanEvent(BaseModel):
    """Yielded by a workflow step that requires human approval or input.

    The workflow runner serialises this to ``state_json`` in ``workflow_runs``
    and sets ``status='awaiting_human'``.
    """

    person_id: int
    question: str
    timeout_hours: int = 48
    on_timeout: Literal["escalate", "auto_proceed", "fail"] = "escalate"
    context_summary: str = ""
    expected_reply_shape: Literal[
        "approve_reject", "free_text", "numeric", "document"
    ] = "approve_reject"
    # Optional: department slug for escalation routing.
    department: str = ""
    # The outbound message id sent to the person — used by the inbound resolver
    # to match replies via explicit reference (tier 1).
    outbound_message_id: str = ""
    # Channel the question was sent on (e.g. "slack", "email", "telegram").
    channel: str = ""
    # Channel-specific address used (Slack user id, email address, chat_id str).
    channel_ref: str = ""
    # How the question actually reached the approver, set by
    # `gate_delivery.deliver_gate_question`: self / sent / suppressed /
    # alerted / failed. Its PRESENCE also dates the checkpoint — a row
    # written before gate delivery existed has no `delivery` key at all,
    # which is how the resolver tells a legacy row (safe to match loosely)
    # from one whose delivery genuinely failed (must not be).
    delivery: str = ""
    # Chat session the gate was raised from, when a person launched the
    # workflow conversationally and is themselves the approver. The inbound
    # resolver matches such a gate ONLY against replies in that same session,
    # so an open gate in one Slack DM cannot swallow an unrelated message in
    # another thread. Empty for web/scheduler-originated runs, which fall back
    # to channel matching.
    origin_session_id: str = ""
    # Engine payload for continuing the run after the gate is answered.
    # `exclude=True` is load-bearing: `state_json` is the checkpoint the
    # inbound resolver reads, and it branches on which keys are PRESENT (a row
    # with no `delivery` key is a pre-delivery legacy row). Excluding at the
    # FIELD level rather than per-call means no serialisation site can leak
    # the payload into that JSON by forgetting to. The payload has its own
    # column, `workflow_runs.resume_state_json`.
    # None means pause-only: the decision is recorded and the run stops.
    resume_state: WorkflowResumeState | None = Field(
        default=None, exclude=True, repr=False
    )


# Outbound channel vocabulary (`slack_dm`, `discord_dm`) differs from the
# inbound vocabulary the adapters use when resolving a reply (`slack`,
# `discord`). Canonicalise on WRITE so `state_json` only ever holds inbound
# keys — normalising at read time instead would leave two conventions in the
# database.
_CHANNEL_ALIASES = {
    "slack_dm": "slack",
    "discord_dm": "discord",
}


def normalize_channel(channel: str) -> str:
    """Map an outbound channel key onto its inbound equivalent."""
    key = (channel or "").strip().lower()
    return _CHANNEL_ALIASES.get(key, key)


# The decision vocabulary, owned here because three places render it: the
# acknowledgement the approver gets back (`resumer.resolution_acknowledgement`),
# the section a resumed run writes into its artifact
# (`dynamic._format_resolution`), and the branch that decides whether the run
# continues. Two copies would drift, and a run whose artifact says "Approved"
# while its acknowledgement says "Declined" is worse than either alone.
DECISION_VERBS: dict[str, str] = {
    "approve": "Approved",
    "reject": "Declined",
    "defer": "Deferred",
    "auto_proceed": "Auto-approved on timeout",
}

# Decisions that let a resumable run CONTINUE past an approve/reject gate.
#
# An allowlist, deliberately. `decision` is whatever a fast model extracted
# from free-form chat text, and models drift: "rejected", "Reject", "decline",
# "no" are all things it plausibly emits for a refusal. Under a denylist
# ("stop only on these words") every one of those continues the run — the
# approver says no and the declined deliverable is produced anyway. An
# approval gate must fail CLOSED: anything that is not recognisably a yes
# stops the run.
#
# `auto_proceed` is here because the timeout policy synthesises it, and
# `on_timeout='auto_proceed'` is an explicit instruction from the workflow's
# author to proceed unattended.
CONTINUE_DECISIONS = frozenset({"approve", "auto_proceed"})

# Reply shapes that ask a question rather than seek permission. These carry no
# `decision` at all — the answer IS the value — so there is nothing to fail
# closed on and the run always continues.
NON_APPROVAL_SHAPES = frozenset({"free_text", "numeric", "document"})


class WaitForHumanResolution(BaseModel):
    """Recorded when a human successfully replies to a WaitForHumanEvent."""

    run_id: str = ""
    reply_text: str
    source_channel: str
    source_message_id: str = ""
    parsed_decision: dict[str, Any] = Field(default_factory=dict)
    person_id: int
    resolved_at: str = ""


# ---------------------------------------------------------------------------
# Decision parser
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a structured reply parser. "
    "Extract the human's decision from their message and return JSON only. "
    "No markdown, no explanation — pure JSON object on a single line."
)

_SHAPE_PROMPTS: dict[str, str] = {
    "approve_reject": (
        'Return: {"decision": "approve|reject|defer|unrelated", '
        '"note": "<brief reason, max 100 chars>"}\n'
        "Rules: approve = yes/ok/agreed/sounds good/LGTM; reject = no/denied/decline; "
        "defer = maybe later/need more info/not now. "
        "unrelated = the message is not a response to this question at all "
        "(a new request, a different topic, small talk) — use it whenever the "
        "message does not read as an answer to THIS question, even loosely. "
        "When the message IS an answer but its verdict is ambiguous, choose defer."
    ),
    "free_text": (
        'Return: {"text": "<exact reply text, max 500 chars>"}'
    ),
    "numeric": (
        'Return: {"value": <number or null>, "unit": "<unit string or empty>"}\n'
        "Extract the primary numeric value. Null if no number is present."
    ),
    "document": (
        'Return: {"received": true, "text_preview": "<first 200 chars of content>"}'
    ),
}

# Appended to every shape. Without it only `approve_reject` could decline to
# answer, and the three other shapes had NO relevance check at all: an open
# free_text gate turned the person's next unrelated message into its answer
# and closed the sign-off ("what's on my calendar?" recorded verbatim), and a
# numeric gate swallowed any message containing a number.
_UNRELATED_CLAUSE = (
    '\n\nIF the message is not a response to the question at all — a new '
    "request, a different topic, small talk, or a reply meant for someone "
    'else — ignore the shape above and return exactly: {"decision": '
    '"unrelated"}. Prefer this whenever the message does not read as an '
    "answer to THIS question."
)

# Marks a parse_decision result as the fallback rather than a real verdict.
# The previous sentinel was `note == "parse_error"`, which the model itself
# can emit — a human replying "no, your parser threw a parse_error" could
# produce it, and a genuine rejection would then be silently discarded.
# A dunder-ish key under our own namespace is not something the shape prompts
# ask for, so the model has no reason to produce it.
PARSE_FAILED_KEY = "__oe_parse_failed__"

_FALLBACKS: dict[str, dict[str, Any]] = {
    "approve_reject": {"decision": "defer", "note": "parse_error"},
    "free_text": {"text": ""},
    "numeric": {"value": None, "unit": ""},
    "document": {"received": False, "text_preview": ""},
}


async def parse_decision(
    text: str, expected_shape: str, question: str = ""
) -> dict[str, Any]:
    """Parse a human reply into a structured decision dict.

    ``question`` is the gate's own question. Without it the parser sees only
    the reply, so a bare "yes" — which may have been answering the Executive
    about something else entirely — can never be judged ``unrelated``.

    Uses the Council-configurable ``utility_fast`` model (default
    ``settings.routing_model``) for low-latency parsing. Returns a safe
    fallback dict on API errors so callers never see None.
    """
    import json as _json

    from openexecutive.agents.utility_fast import get_fast_model

    shape_prompt = (
        _SHAPE_PROMPTS.get(expected_shape, _SHAPE_PROMPTS["free_text"])
        + _UNRELATED_CLAUSE
    )
    fallback = _FALLBACKS.get(expected_shape, {"text": ""})

    try:
        from openexecutive.audit.usage import log_model_usage
        from openexecutive.config import get_settings
        from openexecutive.providers import get_provider

        settings = get_settings()
        model = get_fast_model()
        response = await get_provider(model).messages_create(
            model=model,
            max_tokens=256,
            timeout=settings.utility_fast_timeout_s,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Parse this reply (expected shape: {expected_shape}):\n\n"
                        f"{shape_prompt}\n\n"
                        + (
                            f"The question it should be answering:\n"
                            f"{question[:500]}\n\n"
                            if question
                            else ""
                        )
                        + f"Reply to parse:\n{text[:1000]}"
                    ),
                }
            ],
        )
        log_model_usage(response, model=model, actor="reply_parser")
        # The SDK only emits text blocks for this prompt (no tools, no
        # thinking). The union-attr complaint mypy raises here is a false
        # positive at runtime; suppress rather than narrowing because the
        # existing tests rely on duck-typed block stubs that wouldn't pass
        # an isinstance(TextBlock) check.
        raw = response.content[0].text.strip() if response.content else ""  # type: ignore[union-attr]
        # Strip any accidental markdown fences.
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        return _json.loads(raw)
    except Exception:
        logger.exception("parse_decision: failed for shape=%r text=%r", expected_shape, text[:80])
        return {**fallback, PARSE_FAILED_KEY: True}
