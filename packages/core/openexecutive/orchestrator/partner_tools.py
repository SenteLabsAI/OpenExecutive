"""Anthropic tool definitions + handlers for partner-seat introductions.

``begin_partner_introduction`` hands the Executive the knowledge snapshot and
the interview agenda for the seat the speaker partners with;
``save_working_agreement`` persists what the person confirmed (or records a
deferral). Both refuse to act for anyone but the human the seat supports (the
principal may act for any seat). The speaker is read from the chat session.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openexecutive.partners.store import (
    DEFAULT_REVIEW_DAYS,
    MAX_AGREEMENT_CHARS,
    PARTNER_SEATS,
)

logger = logging.getLogger(__name__)

BEGIN_PARTNER_INTRODUCTION_TOOL: dict[str, Any] = {
    "name": "begin_partner_introduction",
    "description": (
        "Start (or review) the introduction between a partner seat and the person it supports: "
        "returns an inventory of what the seat has consumed and cannot see yet, the interview "
        "agenda, and any existing working agreement. Call it when the person agrees to set up how "
        "you will work with them, or when an agreement review is due. Then run the interview "
        "conversationally, two or three questions at a time. Do not call it for anyone other than "
        "the seat's own human."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "seat": {
                "type": "string",
                "enum": list(PARTNER_SEATS),
                "description": "Which partner seat: cmo (marketing), cpo (product), ceo_advisor (CEO).",
            }
        },
        "required": ["seat"],
    },
}

SAVE_WORKING_AGREEMENT_TOOL: dict[str, Any] = {
    "name": "save_working_agreement",
    "description": (
        "Persist the working agreement after you have read it back and the person explicitly "
        "confirmed it, or record that they want to defer the introduction. The agreement text is "
        "shown to the partner seat on every future consult, so write it as clear, concrete "
        "instructions in the second person ('Lead with the recommendation', 'Never send anything "
        "without asking') under 300 words, including what stays private. Use status='active' only "
        "with confirmed=true. Use status='deferred' when they say not now; that never erases an "
        "agreement already in force."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "seat": {"type": "string", "enum": list(PARTNER_SEATS)},
            "status": {"type": "string", "enum": ["active", "deferred"], "default": "active"},
            "confirmed": {
                "type": "boolean",
                "description": "True only if the person explicitly said the read-back is right.",
            },
            "agreement_md": {
                "type": "string",
                "description": f"The agreement, max {MAX_AGREEMENT_CHARS} characters. Required for active.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things the seat still wants to learn; asked one at a time as it becomes natural.",
            },
            "review_in_days": {
                "type": "integer",
                "minimum": 7,
                "maximum": 90,
                "default": DEFAULT_REVIEW_DAYS,
                "description": "When to offer a check-in on whether the agreement still fits.",
            },
        },
        "required": ["seat"],
    },
}

PARTNER_TOOLS: list[dict[str, Any]] = [
    BEGIN_PARTNER_INTRODUCTION_TOOL,
    SAVE_WORKING_AGREEMENT_TOOL,
]


def _audit(tool: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    try:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "tool_invocation",
            summary,
            actor="executive",
            details={"tool": tool, "kind": "write", "ok": ok, **details},
        )
    except Exception:  # noqa: BLE001 - audit must never break the tool path.
        logger.warning("partner_tools: audit log failed", exc_info=True)


def _resolve(seat: str) -> tuple[Any | None, str | None]:
    """Return (owner, error). Enforces seat validity and speaker authorization."""
    from openexecutive.partners.intro import owner_for_seat

    if seat not in PARTNER_SEATS:
        return None, f"unknown partner seat {seat!r}; valid: {list(PARTNER_SEATS)}"
    owner = owner_for_seat(seat)
    if owner is None or owner.id is None:
        return None, (
            f"no human is assigned as head of the department the {seat!r} seat serves; "
            "set the department head first"
        )
    session = _session()
    speaker = getattr(session, "speaker_person_id", None) if session is not None else None
    if speaker is None:
        # Fail closed: the agreement is injected into the seat's prompt on every
        # consult, so it must only be written by an identified person.
        return None, "the speaker is not identified, so I cannot set up a working agreement in this conversation"
    if speaker != owner.id:
        from openexecutive.people.store import get_person

        who = get_person(speaker)
        if who is None or not who.is_principal:
            return None, "only the person this seat supports (or the principal) can run its introduction"
    return owner, None


def _session() -> Any | None:
    try:
        from openexecutive.orchestrator.schedule_tools import current_session

        return current_session.get()
    except Exception:  # noqa: BLE001
        return None


async def handle_begin_partner_introduction(tool_input: dict[str, Any]) -> str:
    from openexecutive.partners import intro, store

    seat = str(tool_input.get("seat", "")).strip()
    owner, err = _resolve(seat)
    if err or owner is None:
        _audit("begin_partner_introduction", False, f"begin_partner_introduction refused: {err}", {"seat": seat})
        return json.dumps({"error": err})
    try:
        existing = store.get_agreement(seat)
        review = existing is not None and existing.status == "active"
        store.begin_introduction(seat, owner.id)
        payload = {
            "seat": seat,
            "person": {"id": owner.id, "name": owner.full_name, "role": owner.role},
            "knowledge_snapshot": intro.build_knowledge_snapshot(seat),
            "interview_guide": intro.build_interview_guide(seat, review=review),
            "existing_agreement": existing.agreement_md if review and existing else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("begin_partner_introduction failed seat=%s", seat)
        _audit("begin_partner_introduction", False, f"begin_partner_introduction failed: {exc}", {"seat": seat})
        return json.dumps({"error": str(exc)})
    _audit(
        "begin_partner_introduction", True,
        f"begin_partner_introduction {seat} for person {owner.id} ({'review' if review else 'first'})",
        {"seat": seat, "person_id": owner.id, "review": review},
    )
    return json.dumps(payload)


async def handle_save_working_agreement(tool_input: dict[str, Any]) -> str:
    from openexecutive.partners import store

    seat = str(tool_input.get("seat", "")).strip()
    status = str(tool_input.get("status", "active")).strip() or "active"
    owner, err = _resolve(seat)
    if err or owner is None:
        _audit("save_working_agreement", False, f"save_working_agreement refused: {err}", {"seat": seat})
        return json.dumps({"error": err})
    if status not in ("active", "deferred"):
        return json.dumps({"error": "status must be 'active' or 'deferred'"})

    agreement_md = str(tool_input.get("agreement_md", "") or "").strip()
    if status == "active":
        if tool_input.get("confirmed") is not True:
            return json.dumps({"error": "an active agreement requires confirmed=true after the read-back"})
        if len(agreement_md) < 40:
            return json.dumps({"error": "agreement_md is missing or too short to be useful"})
        if len(agreement_md) > MAX_AGREEMENT_CHARS:
            return json.dumps({"error": f"agreement_md exceeds {MAX_AGREEMENT_CHARS} characters; tighten it"})

    questions_raw = tool_input.get("open_questions") or []
    questions = [str(q) for q in questions_raw] if isinstance(questions_raw, list) else []
    try:
        days = int(tool_input.get("review_in_days", DEFAULT_REVIEW_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_REVIEW_DAYS
    days = max(7, min(90, days))

    try:
        saved = store.save_agreement(
            seat, owner.id,
            agreement_md=agreement_md,
            open_questions=questions,
            status=status,  # type: ignore[arg-type]
            review_in_days=days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("save_working_agreement failed seat=%s", seat)
        _audit("save_working_agreement", False, f"save_working_agreement failed: {exc}", {"seat": seat})
        return json.dumps({"error": str(exc)})
    _audit(
        "save_working_agreement", True,
        f"save_working_agreement {seat}: {saved.status} v{saved.version}",
        {"seat": seat, "person_id": owner.id, "status": saved.status, "version": saved.version,
         "chars": len(agreement_md)},
    )
    return json.dumps({
        "status": "ok", "seat": seat, "agreement_status": saved.status,
        "version": saved.version, "review_after": saved.review_after,
    })


PARTNER_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "begin_partner_introduction": handle_begin_partner_introduction,
    "save_working_agreement": handle_save_working_agreement,
}
