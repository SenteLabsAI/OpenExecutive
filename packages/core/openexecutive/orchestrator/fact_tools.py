"""Chat tools that make a correction stick everywhere.

``remember_fact`` stores a fact or a correction the principal states in the
standing-facts store (``memory/facts.py``), which renders into every prompt
that produces output — chat turns, scheduled runs, the /today header and the
morning brief, the alert review, the specialists and the review workflows.
``forget_fact`` retires one. ``update_company_profile`` changes a field of the
company profile (``company/profile.yaml``) from chat, the same file the
Company page edits.

All three are the principal's alone, on a surface that verified it is them —
the ``record_decision_outcome`` rule: a standing fact is read by every later
prompt as the principal's own account, so one from an inbound email, a
teammate or a run nobody is watching would be text carrying the principal's
authority. No unattended run is offered them
(``schedule_tools.UNATTENDED_WITHHELD_TOOLS``), and a turn private to the
principal is not offered the two that write
(``schedule_tools.PRIVATE_TURN_WITHHELD_TOOLS``): what they write is read on
everyone's turns.

A write also needs ``source_quote``: the principal's exact words from this
message, checked against what they actually typed this turn. It is the
provenance the Pulse page shows, and it keeps the model from storing its own
inference, or a figure from a document, as something the principal said.
The schemas are static, so the cached tool prefix never moves.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_RATIONALE_MAX = 280


REMEMBER_FACT_TOOL: dict[str, Any] = {
    "name": "remember_fact",
    "description": (
        "Keep a fact or a correction the principal states about the business so it "
        "holds everywhere from now on — every later conversation, the briefs, "
        "scheduled runs and the alert review all see it. Call it when the "
        "principal corrects a figure, name, date or status you or a document got "
        "wrong ('St. Albans is 48 units, not 52'), or states one they clearly "
        "want kept ('remember that Riverside Court is fully let'). One fact per "
        "call. A correction of a fact already listed under STANDING FACTS passes "
        "its id as replaces_fact_id; the same subject also replaces the old one. "
        "Only for facts about the business — never someone's pay, health, "
        "performance or other personal matters, which every teammate's "
        "conversation would then see. Only the principal can record one, from a "
        "conversation that confirms it is them. source_quote must be their exact "
        "words from this message. Never record your own inference, a figure from "
        "a document, or something a third party said. For a company-profile field "
        "(industry, headcount, ARR, burn, runway, priorities, ...) use "
        "update_company_profile instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": (
                    "A short, stable label for what the fact is about, e.g. "
                    "'St. Albans unit count'. Reuse the listed subject when correcting."
                ),
            },
            "statement": {
                "type": "string",
                "description": "The fact as it now stands, one sentence, e.g. 'St. Albans has 48 units.'",
            },
            "previous_value": {
                "type": "string",
                "description": "What was wrong, when the principal says so, e.g. '52 units'. Optional.",
            },
            "replaces_fact_id": {
                "type": "integer",
                "description": "The N of '[fact N]' this corrects, when it corrects a listed standing fact.",
            },
            "source_quote": {
                "type": "string",
                "description": "The principal's exact words from this message that state the fact.",
            },
        },
        "required": ["subject", "statement", "source_quote"],
    },
}

FORGET_FACT_TOOL: dict[str, Any] = {
    "name": "forget_fact",
    "description": (
        "Stop using a standing fact, when the principal says it no longer holds "
        "or asks you to forget it and gives no replacement (for a replacement, "
        "call remember_fact with replaces_fact_id instead). Pass the N of "
        "'[fact N]' from STANDING FACTS and a one-sentence rationale naming what "
        "they said. Only the principal can do this, from a conversation that "
        "confirms it is them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fact_id": {"type": "integer", "description": "The N of '[fact N]'."},
            "rationale": {
                "type": "string",
                "description": "One sentence: what the principal said. Stored with the fact.",
            },
        },
        "required": ["fact_id", "rationale"],
    },
}


# field → (label, type). "text" / "int" / "number" set a value; "list" fields
# take add / remove; key_metrics takes a metric name.
_PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "industry": ("Industry", "text"),
    "stage": ("Stage", "text"),
    "mission": ("Mission", "text"),
    "vision": ("Vision", "text"),
    "founding_year": ("Founded", "int"),
    "headcount": ("Headcount", "int"),
    "annual_revenue_arr": ("ARR", "number"),
    "target_customer.profile": ("Target customer", "text"),
    "strategic_priorities.north_star_metric": ("North Star metric", "text"),
    "financials.burn_rate_monthly": ("Monthly burn", "number"),
    "financials.runway_months": ("Runway (months)", "number"),
    "financials.key_metrics": ("Key metric", "metric"),
    "strategic_priorities.current_year": ("Strategic priorities", "list"),
    "competitive_landscape.primary_competitors": ("Competitors", "list"),
    "competitive_landscape.competitive_advantages": ("Competitive advantages", "list"),
    "target_customer.pain_points": ("Customer pain points", "list"),
    "org_structure.leadership_team": ("Leadership team", "list"),
    "culture.values": ("Values", "list"),
    "vendors": ("Vendors", "list"),
    "tickers": ("Tracked tickers", "list"),
}
_TEXT_MAX = 1000
_LIST_ITEM_MAX = 200
_LIST_MAX_ITEMS = 50

UPDATE_COMPANY_PROFILE_TOOL: dict[str, Any] = {
    "name": "update_company_profile",
    "description": (
        "Change one field of the company profile — the company context every "
        "conversation starts from — when the principal states a new value or "
        "corrects an old one ('we're 42 people now', 'burn is down to $180k a "
        "month', 'add Acme to our competitors'). One field per call. For a text "
        "or number field pass value with operation 'set'. For a list field pass "
        "one item with operation 'add' or 'remove'. For financials.key_metrics "
        "pass metric (its name) and value, or operation 'remove' to drop it. "
        "Only the principal can change it, from a conversation that confirms it "
        "is them; source_quote must be their exact words from this message. "
        "Never change a field on your own estimate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "enum": sorted(_PROFILE_FIELDS)},
            "operation": {
                "type": "string",
                "enum": ["set", "add", "remove"],
                "description": "'set' for text/number fields and key metrics; 'add'/'remove' for list fields.",
            },
            "value": {
                "type": "string",
                "description": "The new value (numbers as plain digits, e.g. '180000'), or the list item.",
            },
            "metric": {
                "type": "string",
                "description": "financials.key_metrics only: the metric's name, e.g. 'NRR'.",
            },
            "source_quote": {
                "type": "string",
                "description": "The principal's exact words from this message that state the change.",
            },
        },
        "required": ["field", "operation", "source_quote"],
    },
}

FACT_TOOLS: list[dict[str, Any]] = [
    REMEMBER_FACT_TOOL,
    FORGET_FACT_TOOL,
    UPDATE_COMPANY_PROFILE_TOOL,
]


# --------------------------------------------------------------------------- #
# Shared checks
# --------------------------------------------------------------------------- #


def _session() -> Any:
    from openexecutive.orchestrator.schedule_tools import current_session

    return current_session.get()


def _audit(tool: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    """A ``tool_invocation`` audit row. Never breaks the tool path."""
    try:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "tool_invocation",
            summary,
            actor="executive",
            details={"tool": tool, "kind": "write", "ok": ok, **details},
        )
    except Exception:  # noqa: BLE001 - audit must never break the tool path.
        logger.warning("fact_tools: audit log failed", exc_info=True)


def _caller_context(session: Any) -> dict[str, Any]:
    return {
        "caller_person_id": getattr(session, "caller_person_id", None),
        "origin_channel": getattr(session, "origin_channel", "") or None,
        "from_web_chat": bool(getattr(session, "from_web_chat", False)),
        "unattended": bool(getattr(session, "unattended", False)),
    }


def _bad(tool: str, error: str, **details: Any) -> str:
    _audit(tool, False, f"{tool} bad input: {error[:120]}", {"error": error[:300], **details})
    return json.dumps({"error": error})


def _refusal(tool: str) -> str | None:
    """The refusal result unless the principal asked on a verified surface."""
    from openexecutive.orchestrator.people_tools import is_principal_on_verified_surface

    session = _session()
    if is_principal_on_verified_surface(session) and not getattr(session, "unattended", False):
        return None
    return _bad(
        tool,
        "refused: only the principal can change what the Executive keeps as fact, "
        "and this request did not come from a conversation that confirms it is "
        "them. Tell whoever asked that the principal needs to tell you — in the "
        "web app, or in their own Slack or Discord.",
        refused=True, **_caller_context(session),
    )


def _speaker_text(session: Any) -> str:
    from openexecutive.delegation.settings import turn_delegation

    pin = turn_delegation(session)
    return pin.speaker_text if pin is not None else ""


def _quote_error(quote: str, session: Any) -> str | None:
    """None when ``quote`` is really the principal's words this turn."""
    from openexecutive.memory.episodic import _normalize_for_quote_match

    if not quote:
        return "source_quote is required: the principal's exact words from this message."
    spoken = _speaker_text(session)
    nq = _normalize_for_quote_match(quote)
    if not nq or nq not in _normalize_for_quote_match(spoken):
        return (
            "source_quote is not in what the principal wrote this turn. Quote their "
            "exact words, or — if they did not state it — do not record it."
        )
    return None


def _text(tool_input: dict[str, Any], name: str) -> str:
    raw = tool_input.get(name)
    return "" if raw is None else " ".join(str(raw).split())


def _positive_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _provenance(session: Any) -> dict[str, Any]:
    from openexecutive.audit.context import get_active_turn_id

    channel = getattr(session, "origin_channel", "") or (
        "web" if getattr(session, "from_web_chat", False) else ""
    )
    return {
        "source_channel": channel,
        "session_id": getattr(session, "session_id", None),
        "turn_id": get_active_turn_id(),
        "recorded_by_person_id": getattr(session, "caller_person_id", None),
    }


# --------------------------------------------------------------------------- #
# remember_fact / forget_fact
# --------------------------------------------------------------------------- #


async def handle_remember_fact(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory import facts

    tool = "remember_fact"
    refused = _refusal(tool)
    if refused is not None:
        return refused
    session = _session()
    subject = _text(tool_input, "subject")
    statement = _text(tool_input, "statement")
    quote = _text(tool_input, "source_quote")
    previous = _text(tool_input, "previous_value")
    if not subject:
        return _bad(tool, "subject is required (a short label, e.g. 'St. Albans unit count')")
    if not statement:
        return _bad(tool, "statement is required (the fact as it now stands, one sentence)")
    if len(subject) > facts.SUBJECT_MAX:
        return _bad(tool, f"subject must be at most {facts.SUBJECT_MAX} characters")
    if len(statement) > facts.STATEMENT_MAX:
        return _bad(tool, f"statement must be at most {facts.STATEMENT_MAX} characters")
    quote_error = _quote_error(quote, session)
    if quote_error:
        return _bad(tool, quote_error, subject=subject[:120])
    replaces = _positive_int(tool_input.get("replaces_fact_id"))
    if tool_input.get("replaces_fact_id") is not None and replaces is None:
        return _bad(tool, "replaces_fact_id must be the N of a listed '[fact N]'")
    if replaces is not None:
        existing = facts.get_fact(replaces)
        if existing is None or existing.status != "active" or existing.kind == "profile":
            return _bad(tool, f"no standing fact {replaces}. Check the '[fact N]' ids and call again.")

    try:
        fact, superseded = facts.record_fact(
            subject=subject, statement=statement, source_quote=quote,
            previous_statement=previous, replaces_fact_id=replaces,
            **_provenance(session),
        )
    except Exception as exc:
        logger.exception("remember_fact: write failed")
        _audit(tool, False, f"remember_fact FAILED: {type(exc).__name__}", {"error": repr(exc)[:300]})
        return json.dumps({
            "error": f"remember_fact failed with {type(exc).__name__}. The failure is "
                     "recorded; do not retry the same call unchanged."
        })

    _audit(
        tool, True, f"remember_fact {fact.id}: {subject[:60]} — {statement[:80]}",
        {
            "fact_id": fact.id, "kind": fact.kind, "subject": subject,
            "statement": statement, "previous": fact.previous_statement[:300],
            "superseded_ids": [f.id for f in superseded], "source_quote": quote[:500],
        },
    )
    return json.dumps({
        "status": "ok",
        "fact_id": fact.id,
        "kind": fact.kind,
        "subject": fact.subject,
        "statement": fact.statement,
        "replaced": [{"fact_id": f.id, "statement": f.statement} for f in superseded],
    })


async def handle_forget_fact(tool_input: dict[str, Any]) -> str:
    from openexecutive.memory import facts

    tool = "forget_fact"
    refused = _refusal(tool)
    if refused is not None:
        return refused
    fact_id = _positive_int(tool_input.get("fact_id"))
    rationale = _text(tool_input, "rationale")
    if fact_id is None:
        return _bad(tool, "fact_id is required: the N of a listed '[fact N]'")
    if not rationale:
        return _bad(tool, "rationale is required (one sentence: what the principal said)")
    existing = facts.get_fact(fact_id)
    if existing is None or existing.kind == "profile":
        return _bad(tool, f"no standing fact {fact_id}")
    retired = facts.retire_fact(fact_id, reason=rationale[:_RATIONALE_MAX])
    if retired is None:
        return _bad(tool, f"fact {fact_id} is not active (already replaced or forgotten)")
    _audit(
        tool, True, f"forget_fact {fact_id}: {existing.subject[:60]}",
        {"fact_id": fact_id, "subject": existing.subject, "statement": existing.statement,
         "rationale": rationale[:_RATIONALE_MAX]},
    )
    return json.dumps({"status": "ok", "fact_id": fact_id, "forgotten": existing.statement})


# --------------------------------------------------------------------------- #
# update_company_profile
# --------------------------------------------------------------------------- #


def _parse_number(raw: str, *, integer: bool) -> float | int | None:
    cleaned = raw.replace(",", "").replace("$", "").replace("_", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    if integer:
        return int(value) if value == int(value) else None
    return value


def _display(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "(empty)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value == int(value):
        return f"{int(value):,}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


async def handle_update_company_profile(tool_input: dict[str, Any]) -> str:
    from openexecutive.config import get_settings
    from openexecutive.memory import facts
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    tool = "update_company_profile"
    refused = _refusal(tool)
    if refused is not None:
        return refused
    session = _session()
    field = _text(tool_input, "field")
    op = _text(tool_input, "operation").lower()
    value = _text(tool_input, "value")
    metric = _text(tool_input, "metric")
    quote = _text(tool_input, "source_quote")
    if field not in _PROFILE_FIELDS:
        return _bad(tool, f"field must be one of: {', '.join(sorted(_PROFILE_FIELDS))}")
    label, ftype = _PROFILE_FIELDS[field]
    if op not in ("set", "add", "remove"):
        return _bad(tool, "operation must be 'set', 'add' or 'remove'")
    if ftype == "list" and op == "set":
        return _bad(tool, f"{field} is a list: use 'add' or 'remove' with one item")
    if ftype not in ("list", "metric") and op != "set":
        return _bad(tool, f"{field} takes operation 'set'")
    if ftype == "metric" and op == "add":
        return _bad(tool, "financials.key_metrics takes 'set' (with metric and value) or 'remove'")
    if ftype == "metric" and not metric:
        return _bad(tool, "metric is required for financials.key_metrics (its name, e.g. 'NRR')")
    if not value and not (ftype == "metric" and op == "remove"):
        return _bad(tool, "value is required")
    quote_error = _quote_error(quote, session)
    if quote_error:
        return _bad(tool, quote_error, field=field)

    profile_path = get_settings().company_profile_path
    profile = load_or_create_profile(profile_path)
    if profile.is_empty():
        return _bad(tool, "there is no company profile yet — the principal completes onboarding first")
    data = profile.model_dump()
    parent_key, _, leaf = field.rpartition(".")
    container = data[parent_key] if parent_key else data
    if ftype == "metric":
        metric = metric[:80]
        old = container[leaf].get(metric)
        if op == "remove":
            if metric not in container[leaf]:
                return _bad(tool, f"there is no key metric named {metric!r}")
            container[leaf].pop(metric)
            new: Any = None
        else:
            new = value[:_TEXT_MAX]
            container[leaf][metric] = new
        subject_label = f"{label}: {metric}"
    elif ftype == "list":
        old = list(container[leaf])
        item = value[:_LIST_ITEM_MAX]
        folded = [str(v).casefold() for v in old]
        if op == "add":
            if item.casefold() in folded:
                return json.dumps({
                    "status": "unchanged", "noop": True, "field": field, "value": _display(old),
                })
            if len(old) >= _LIST_MAX_ITEMS:
                return _bad(tool, f"{field} already has {_LIST_MAX_ITEMS} items; remove one first")
            container[leaf] = [*old, item]
        else:
            if item.casefold() not in folded:
                return _bad(tool, f"{item!r} is not in {field}: {_display(old)}")
            idx = folded.index(item.casefold())
            container[leaf] = old[:idx] + old[idx + 1:]
        new = container[leaf]
        subject_label = label
    else:
        old = container[leaf]
        if ftype == "text":
            new = value[:_TEXT_MAX]
        else:
            new = _parse_number(value, integer=ftype == "int")
            if new is None:
                return _bad(tool, f"{field} takes a non-negative {'whole ' if ftype == 'int' else ''}number, e.g. '42'")
        container[leaf] = new
        subject_label = label

    try:
        updated = CompanyProfile.model_validate(data)
        updated.save_to_yaml(profile_path)
    except Exception as exc:
        logger.exception("update_company_profile: write failed field=%s", field)
        _audit(tool, False, f"update_company_profile FAILED {field}: {type(exc).__name__}",
               {"field": field, "error": repr(exc)[:300]})
        return json.dumps({
            "error": f"update_company_profile failed with {type(exc).__name__}. The failure "
                     "is recorded; do not retry the same call unchanged."
        })

    # The rest of this turn keeps the profile it started with (its system
    # block is already built); the next turn on this session reads the new one.
    if session is not None and getattr(session, "company_profile", None) is not None:
        session.company_profile = updated

    if ftype == "list":
        verb = "added" if op == "add" else "removed"
        statement = f"{label}: {verb} {value[:_LIST_ITEM_MAX]}"
    elif new is None:
        statement = f"{subject_label}: removed"
    else:
        statement = f"{subject_label} set to {_display(new)}"
    fact_id: int | None = None
    try:
        row, _ = facts.record_fact(
            kind="profile",
            subject=f"Company profile — {subject_label}",
            statement=statement,
            previous_statement="" if ftype == "list" else _display(old),
            source_quote=quote,
            **_provenance(session),
        )
        fact_id = row.id
    except Exception:
        # The profile write already landed; the Pulse page just misses a line.
        logger.warning("update_company_profile: provenance row failed", exc_info=True)

    _audit(
        tool, True, f"update_company_profile {field}: {statement[:100]}",
        {"field": field, "operation": op, "metric": metric or None,
         "previous": _display(old)[:300], "value": _display(new)[:300],
         "source_quote": quote[:500], "fact_id": fact_id},
    )
    return json.dumps({
        "status": "ok",
        "field": field,
        "label": subject_label,
        "previous": _display(old),
        "value": _display(new),
    })


FACT_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "remember_fact": handle_remember_fact,
    "forget_fact": handle_forget_fact,
    "update_company_profile": handle_update_company_profile,
}
