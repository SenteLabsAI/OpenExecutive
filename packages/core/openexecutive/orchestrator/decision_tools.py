"""Chat tool that records how a past decision turned out.

The weekly review asks "how did this turn out?" about up to three decisions
older than 30 days with no outcome recorded (``[decision N]`` in the review).
When the principal answers, ``record_decision_outcome`` writes the outcome to
that ``decisions`` row (``episodic.update_decision``) and audits the write
with the rationale — the same ``outcome`` column the Memories page edits.

Only the principal, on a surface that verified it is them (the web chat,
their own Slack or Discord, a private Telegram chat with a valid webhook
secret), may record one — the create_goal rule: an outcome renders in every
later turn's memory block as the principal's own account of what happened, so
one from an inbound email, a teammate or a run nobody is watching would be
text carrying the principal's authority. No unattended run is offered the
tool at all (``schedule_tools.UNATTENDED_WITHHELD_TOOLS``). The schema is
static, so the cached tool prefix never moves.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_OUTCOME_MAX = 1000
_RATIONALE_MAX = 280
# Decisions listed back when the id is missing or unknown, so the model can
# retry with the right one.
_CANDIDATES_MAX = 5


RECORD_DECISION_OUTCOME_TOOL: dict[str, Any] = {
    "name": "record_decision_outcome",
    "description": (
        "Record how a past decision turned out, when the principal tells you — "
        "for example in answer to the weekly review's 'how did these turn out?' "
        "list, where each decision shows as '[decision N]'. Pass that N as "
        "decision_id, the outcome in their words, and a one-sentence rationale "
        "naming what they said. Only the principal can record an outcome, and "
        "only from a conversation that confirms it is them; for anyone else it "
        "is refused. Record only what the principal actually reported — never "
        "your own assessment. If you do not know the id, call it without one: "
        "the reply lists the decisions still waiting on an outcome."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_id": {
                "type": "integer",
                "description": "The decision's id — the N in '[decision N]'.",
            },
            "outcome": {
                "type": "string",
                "description": (
                    "How it turned out, in the principal's words, e.g. 'Worked — "
                    "3 of 3 new clients took the sprint packages; margin held.'"
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "One short sentence: what the principal said that records "
                    "this outcome. Stored in the audit log."
                ),
            },
        },
        "required": ["decision_id", "outcome", "rationale"],
    },
}

DECISION_TOOLS: list[dict[str, Any]] = [RECORD_DECISION_OUTCOME_TOOL]


def _audit(ok: bool, summary: str, details: dict[str, Any]) -> None:
    """A ``tool_invocation`` audit row, like the other chat tools. Never
    breaks the tool path."""
    try:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "tool_invocation",
            summary,
            actor="executive",
            details={"tool": "record_decision_outcome", "kind": "write", "ok": ok, **details},
        )
    except Exception:  # noqa: BLE001 - audit must never break the tool path.
        logger.warning("decision_tools: audit log failed", exc_info=True)


def _principal_asked() -> bool:
    """The principal on a verified surface (people_tools' rule). Fails closed."""
    from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface
    from openexecutive.orchestrator.schedule_tools import current_session

    return is_principal_on_verified_surface(current_session.get())


def _caller_context() -> dict[str, Any]:
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    return {
        "caller_person_id": getattr(session, "caller_person_id", None),
        "origin_channel": getattr(session, "origin_channel", "") or None,
        "from_web_chat": bool(getattr(session, "from_web_chat", False)),
        "unattended": bool(getattr(session, "unattended", False)),
    }


def _text(tool_input: dict[str, Any], name: str) -> str:
    raw = tool_input.get(name)
    return "" if raw is None else " ".join(str(raw).split())


def _awaiting_outcome() -> list[dict[str, Any]]:
    """Decisions with no outcome yet (the newest first), for the model to
    pick an id from. Includes recent ones: the principal may report early."""
    from openexecutive.memory.episodic import decisions_awaiting_outcome

    try:
        rows = decisions_awaiting_outcome(
            datetime.now(UTC) + timedelta(seconds=1), limit=_CANDIDATES_MAX
        )
    except Exception:
        logger.warning("decision_tools: decisions unreadable", exc_info=True)
        return []
    return [
        {"decision_id": d.id, "date": str(d.timestamp)[:10], "summary": d.summary[:200]}
        for d in rows
    ]


def _bad(error: str, **details: Any) -> str:
    _audit(False, f"record_decision_outcome bad input: {error[:120]}", {"error": error[:300], **details})
    return json.dumps({"error": error})


async def handle_record_decision_outcome(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory import episodic

    # ---- Who is asking ----
    if not _principal_asked():
        return _bad(
            "refused: only the principal can record how a decision turned out, "
            "and this request did not come from a conversation that confirms it "
            "is them. Tell whoever asked that the principal needs to record it — "
            "in the web app, or by telling you there or in their own Slack or "
            "Discord.",
            refused=True, **_caller_context(),
        )

    # ---- Input validation ----
    outcome = _text(tool_input, "outcome")
    rationale = _text(tool_input, "rationale")
    if not outcome:
        return _bad("outcome is required (how the decision turned out, in the principal's words)")
    if len(outcome) > _OUTCOME_MAX:
        return _bad(f"outcome must be at most {_OUTCOME_MAX} characters")
    if not rationale:
        return _bad("rationale is required (one sentence: what the principal said)")

    raw_id = tool_input.get("decision_id")
    try:
        decision_id = int(raw_id)  # type: ignore[arg-type]
        if isinstance(raw_id, bool) or decision_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return json.dumps({
            "error": "decision_id is required: the N in '[decision N]'. Pick it from "
                     "awaiting_outcome and call again.",
            "awaiting_outcome": _awaiting_outcome(),
        })

    # ---- Write ----
    db_path = episodic._resolve_db_path(None)
    try:
        decision = episodic.get_decision(decision_id, db_path=db_path)
        if decision is None:
            return json.dumps({
                "error": f"no decision {decision_id}. Pick the id from awaiting_outcome "
                         "and call again.",
                "awaiting_outcome": _awaiting_outcome(),
            })
        updated = episodic.update_decision(decision_id, outcome=outcome, db_path=db_path)
    except Exception as exc:
        logger.exception("record_decision_outcome: write failed id=%s", decision_id)
        _audit(False, f"record_decision_outcome FAILED id={decision_id}: {type(exc).__name__}",
               {"decision_id": decision_id, "error": repr(exc)[:300]})
        # The type only: an exception's text can carry paths or echo input.
        return json.dumps({
            "error": (
                f"record_decision_outcome failed with {type(exc).__name__}. The "
                "failure is recorded; do not retry the same call unchanged."
            )
        })
    if not updated:
        return _bad(f"decision {decision_id} could not be updated", decision_id=decision_id)

    _audit(
        True,
        f"record_decision_outcome {decision_id}: {outcome[:80]}",
        {
            "decision_id": decision_id,
            "decision": decision.summary[:200],
            "previous_outcome": decision.outcome[:300],
            "outcome": outcome[:_OUTCOME_MAX],
            "rationale": rationale[:_RATIONALE_MAX],
        },
    )
    return json.dumps({
        "status": "ok",
        "decision_id": decision_id,
        "decision": decision.summary[:200],
        "outcome": outcome,
        "replaced_previous": bool(decision.outcome.strip()),
    })


DECISION_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "record_decision_outcome": handle_record_decision_outcome,
}
