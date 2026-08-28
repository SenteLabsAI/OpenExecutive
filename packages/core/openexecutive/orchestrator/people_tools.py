"""Anthropic tool definitions + handlers for managing the People roster.

These tools let the Executive add, update, archive, list, and assign
department heads directly from a chat turn — no UI round-trip required.

They sit alongside `create_alert`, the schedule/send tools, and
`consult_specialist` in the Executive's main tool loop.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from openexecutive.memory.honcho_client import directional_chat

logger = logging.getLogger(__name__)


_VALID_PREFERRED_CHANNELS = {"email", "slack", "telegram", "discord", "any"}


def _principal_write_error() -> str | None:
    """Return a safe denial unless this chat turn belongs to the principal."""
    from openexecutive.orchestrator.schedule_tools import (
        current_caller_person_id,
        current_roster_write_authorized,
    )
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    caller_person_id = current_caller_person_id.get()
    if (
        not current_roster_write_authorized.get()
        or principal is None
        or caller_person_id != principal.id
    ):
        return "People roster changes require the authenticated principal."
    return None


LIST_PEOPLE_TOOL: dict[str, Any] = {
    "name": "list_people",
    "description": (
        "List people in the company roster. Use this to resolve a name to a "
        "person_id before calling upsert_person, archive_person, or "
        "set_department_head. Returns a compact JSON list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_archived": {
                "type": "boolean",
                "description": "If true, also include archived (soft-deleted) people. Default false.",
            },
        },
    },
}


UPSERT_PERSON_TOOL: dict[str, Any] = {
    "name": "upsert_person",
    "description": (
        "Create or update a person in the People roster. Pass `person_id` to "
        "update an existing row; omit it to create a new one. When the user "
        "asks you to add someone, fill in everything you know — name, role, "
        "email, department slugs, authority scopes — and call this directly. "
        "Roster writes are allowed only for the authenticated principal; for "
        "any other caller, return the permission error. Call list_people first "
        "if you need to look up an existing person_id by name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "full_name": {"type": "string", "description": "Person's full name."},
            "person_id": {
                "type": "integer",
                "description": "Existing person id to UPDATE. Omit to create a new row.",
            },
            "role": {
                "type": "string",
                "description": "Role/title (e.g. 'Head of Marketing', 'Bookkeeper').",
            },
            "department_slugs": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Department slugs this person belongs to (e.g. ['marketing']). "
                    "Slugs are lowercase and match the department registry."
                ),
            },
            "email": {"type": "string"},
            "slack_user_id": {"type": "string"},
            "telegram_chat_id": {"type": "string"},
            "discord_user_id": {"type": "string"},
            "preferred_channel": {
                "type": "string",
                "enum": ["email", "slack", "telegram", "discord", "any"],
                "description": "Preferred channel for routing messages to this person.",
            },
            "authority_scopes": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "spend_lt_2k",
                        "spend_lt_10k",
                        "spend_gt_10k",
                        "hiring_signoff",
                        "vendor_onboarding",
                        "customer_credit",
                        "legal_sign",
                        "board_comms",
                        "wildcard",
                    ],
                },
                "description": (
                    "Approval scopes this person holds. Pass the full intended "
                    "list — this REPLACES the existing scope set on update."
                ),
            },
            "response_sla_hours": {
                "type": "integer",
                "description": "Expected response time in hours. Default 24.",
            },
            "on_leave_until": {
                "type": "string",
                "description": "ISO date (YYYY-MM-DD) the person is on leave until, or omit if available.",
            },
            "reports_to_person_id": {
                "type": "integer",
                "description": "Person id this person reports to, if any.",
            },
        },
        "required": ["full_name"],
    },
}


ARCHIVE_PERSON_TOOL: dict[str, Any] = {
    "name": "archive_person",
    "description": (
        "Soft-delete a person from the roster by id. Reversible by an admin "
        "via direct DB edit; the person stops appearing in normal listings "
        "and approval lookups. Use when someone leaves the company."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {"type": "integer", "description": "Id of the person to archive."},
        },
        "required": ["person_id"],
    },
}


SET_DEPARTMENT_HEAD_TOOL: dict[str, Any] = {
    "name": "set_department_head",
    "description": (
        "Assign a person as the head of a department (or clear it by passing "
        "person_id=null). The person must already exist in the roster — call "
        "upsert_person first if they don't."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "department_slug": {
                "type": "string",
                "description": "Department slug, e.g. 'marketing'.",
            },
            "person_id": {
                "type": ["integer", "null"],
                "description": "Person id to assign as head, or null to clear the head.",
            },
        },
        "required": ["department_slug"],
    },
}


ASK_ABOUT_PERSON_TOOL: dict[str, Any] = {
    "name": "ask_about_person",
    "description": (
        "Ask Honcho's per-person memory about a specific Person — their preferences, "
        "ongoing concerns, factual claims they've made, or what one peer thinks of "
        "another. Synthesizes across every Honcho session that Person has appeared "
        "in (Slack, Discord, email, etc.). Use when:\n"
        " - You need facts about a Person that go beyond the current turn's "
        "`<peer_memory>` block (which is already injected for free at turn start).\n"
        " - You're advising peer A about peer B and want A's perspective specifically — "
        "set `target_person_id=B` to query A's representation of B "
        "(e.g. person_id=alice, target_person_id=bob, "
        "question=\"What concerns has Alice raised about Bob?\").\n"
        "Do NOT call when the inline `<peer_memory>` block already answers the "
        "question — that block costs nothing extra; this tool spends an extra "
        "Honcho LLM call.\n"
        "Returns the synthesized answer, or an empty string if Honcho is disabled / no data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "person_id": {
                "type": "integer",
                "description": "Person.id of the peer whose memory you're querying.",
            },
            "question": {
                "type": "string",
                "description": "Natural-language question about that peer.",
            },
            "target_person_id": {
                "type": "integer",
                "description": (
                    "Optional Person.id of a second peer. When provided, the answer "
                    "comes from `person_id`'s representation of `target_person_id` — "
                    "i.e. what does this person know/think about that person."
                ),
            },
            "reasoning_level": {
                "type": "string",
                "enum": ["minimal", "low", "medium", "high", "max"],
                "description": (
                    "Trade latency for synthesis depth. Default `medium` is right for "
                    "most tool calls (the model already decided to spend the latency by "
                    "calling). Use `high`/`max` for deep-research questions; `low` if you "
                    "just need a quick refresher and care about latency."
                ),
            },
        },
        "required": ["person_id", "question"],
    },
}


PEOPLE_TOOLS: list[dict[str, Any]] = [
    LIST_PEOPLE_TOOL,
    UPSERT_PERSON_TOOL,
    ARCHIVE_PERSON_TOOL,
    SET_DEPARTMENT_HEAD_TOOL,
    ASK_ABOUT_PERSON_TOOL,
]


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _audit(tool: str, kind: str, ok: bool, summary: str, details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "tool_invocation",
        summary,
        actor="executive",
        details={"tool": tool, "kind": kind, "ok": ok, **details},
    )


async def handle_list_people(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import store as people_store

    include_archived = bool(tool_input.get("include_archived", False))
    try:
        people = people_store.list_people(include_archived=include_archived)
    except Exception as exc:
        logger.exception("list_people: failed")
        _audit("list_people", "read", False, f"list_people failed: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": str(exc)})

    out = [
        {
            "person_id": p.id,
            "full_name": p.full_name,
            "role": p.role,
            "is_principal": p.is_principal,
            "email": p.email,
            "preferred_channel": p.preferred_channel,
            "department_slugs": p.department_slugs,
            "authority_scope": [s.value for s in p.authority_scope],
            "archived": p.archived,
        }
        for p in people
    ]
    _audit(
        "list_people", "read", True,
        f"list_people returned {len(out)}",
        {"count": len(out), "include_archived": include_archived},
    )
    return json.dumps({"people": out, "count": len(out)})


async def handle_upsert_person(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store
    from openexecutive.people.models import AuthorityScope

    def _bad(err: str) -> str:
        _audit("upsert_person", "write", False, f"upsert_person bad input: {err}", {"error": err[:300]})
        return json.dumps({"error": err})

    if denied := _principal_write_error():
        return _bad(denied)

    try:
        full_name = str(tool_input["full_name"]).strip()
        if not full_name:
            raise ValueError("full_name is required and cannot be empty")
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(f"bad arguments: {exc}")

    # The principal flag controls fallback authority and can only be flipped
    # via the HTTP API behind BACKEND_SHARED_SECRET (see people/models.py).
    # The Executive must not be able to promote someone via a chat turn —
    # social-engineering would otherwise bypass the API gate entirely.
    if bool(tool_input.get("is_principal", False)):
        return _bad(
            "is_principal can only be set via the authenticated /people API, not via chat tools"
        )

    person_id = tool_input.get("person_id")
    if person_id is not None:
        try:
            person_id = int(person_id)
        except (TypeError, ValueError):
            return _bad("person_id must be an integer")
        existing = people_store.get_person(person_id)
        if existing is None:
            return _bad(f"person {person_id} not found")
        # Chat turns are available to non-principals, so they must never
        # mutate the canonical principal record (including its email, which is
        # the HTTP authorization binding). The principal can make those edits
        # through the principal-gated /people API instead.
        if existing.is_principal:
            return _bad(
                "the principal record can only be changed via the authenticated /people API"
            )

    preferred_channel = tool_input.get("preferred_channel", "any")
    if preferred_channel not in _VALID_PREFERRED_CHANNELS:
        return _bad(
            f"preferred_channel must be one of {sorted(_VALID_PREFERRED_CHANNELS)}"
        )

    on_leave_until_raw = tool_input.get("on_leave_until")
    on_leave_until: date | None = None
    if on_leave_until_raw:
        try:
            on_leave_until = date.fromisoformat(str(on_leave_until_raw))
        except ValueError as exc:
            return _bad(f"on_leave_until must be YYYY-MM-DD: {exc}")

    authority_scopes_raw = tool_input.get("authority_scopes")
    authority_scopes: list[AuthorityScope] | None = None
    if authority_scopes_raw is not None:
        try:
            authority_scopes = [AuthorityScope(str(s)) for s in authority_scopes_raw]
        except ValueError as exc:
            return _bad(f"invalid authority scope: {exc}")

    department_slugs = tool_input.get("department_slugs")
    if department_slugs is not None:
        department_slugs = [str(s).strip() for s in department_slugs if str(s).strip()]

    # is_principal is preserved on update (we already rejected attempts to flip
    # it above); on insert it is always False.
    preserved_principal = (
        people_store.get_person(person_id).is_principal  # type: ignore[union-attr]
        if person_id is not None
        else False
    )
    kwargs: dict[str, Any] = {
        "full_name": full_name,
        "role": str(tool_input.get("role", "") or ""),
        "is_principal": preserved_principal,
        "department_slugs": department_slugs,
        "email": tool_input.get("email"),
        "slack_user_id": tool_input.get("slack_user_id"),
        "telegram_chat_id": tool_input.get("telegram_chat_id"),
        "discord_user_id": tool_input.get("discord_user_id"),
        "preferred_channel": preferred_channel,
        "response_sla_hours": int(tool_input.get("response_sla_hours", 24)),
        "on_leave_until": on_leave_until,
        "reports_to_person_id": tool_input.get("reports_to_person_id"),
    }
    if person_id is not None:
        kwargs["person_id"] = person_id

    action = "updated" if person_id is not None else "created"
    try:
        new_id = people_store.upsert_person(**kwargs)
        if authority_scopes is not None:
            people_store.set_authority_scope(new_id, authority_scopes)
    except Exception as exc:
        logger.exception("upsert_person: failed")
        # Always invalidate — a partial write (e.g. row inserted but scopes
        # failed) must not leave the registry serving stale data.
        people_registry.invalidate()
        _audit(
            "upsert_person", "write", False,
            f"upsert_person FAILED: {exc}",
            {"error": str(exc)[:300], "action": action, "full_name": full_name},
        )
        return json.dumps({"error": str(exc)})
    people_registry.invalidate()

    _audit(
        "upsert_person", "write", True,
        f"upsert_person {action} id={new_id} name={full_name!r}",
        {
            "person_id": new_id,
            "action": action,
            "full_name": full_name,
            "department_slugs": department_slugs or [],
            "authority_scopes_set": (
                [s.value for s in authority_scopes] if authority_scopes is not None else None
            ),
        },
    )
    return json.dumps({
        "status": "ok",
        "action": action,
        "person_id": new_id,
        "full_name": full_name,
    })


async def handle_archive_person(tool_input: dict[str, Any]) -> str:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    if denied := _principal_write_error():
        _audit("archive_person", "write", False, denied, {"error": denied})
        return json.dumps({"error": denied})

    try:
        person_id = int(tool_input["person_id"])
    except (KeyError, TypeError, ValueError) as exc:
        _audit("archive_person", "write", False, f"archive_person bad input: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": f"bad arguments: {exc}"})

    person = people_store.get_person(person_id)
    if person is not None and person.is_principal:
        detail = "the principal record cannot be archived"
        _audit(
            "archive_person", "write", False, detail,
            {"person_id": person_id, "error": detail},
        )
        return json.dumps({"error": detail})

    try:
        archived = people_store.archive_person(person_id)
        if archived:
            people_registry.invalidate()
    except Exception as exc:
        logger.exception("archive_person: failed")
        _audit("archive_person", "write", False, f"archive_person FAILED id={person_id}: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": str(exc)})

    if not archived:
        _audit(
            "archive_person", "write", False,
            f"archive_person id={person_id} not found or already archived",
            {"person_id": person_id, "archived": False},
        )
        return json.dumps({
            "status": "not_found",
            "person_id": person_id,
            "detail": "no active person with that id",
        })
    _audit(
        "archive_person", "write", True,
        f"archive_person id={person_id}",
        {"person_id": person_id, "archived": True},
    )
    return json.dumps({"status": "archived", "person_id": person_id})


async def handle_set_department_head(tool_input: dict[str, Any]) -> str:
    from openexecutive.departments import registry as dept_registry
    from openexecutive.departments import store as dept_store
    from openexecutive.departments.head_persona import ensure_head_persona_override
    from openexecutive.people import store as people_store

    if denied := _principal_write_error():
        _audit("set_department_head", "write", False, denied, {"error": denied})
        return json.dumps({"error": denied})

    try:
        department_slug = str(tool_input["department_slug"]).strip()
        if not department_slug:
            raise ValueError("department_slug cannot be empty")
    except (KeyError, TypeError, ValueError) as exc:
        _audit("set_department_head", "write", False, f"set_department_head bad input: {exc}", {"error": str(exc)[:300]})
        return json.dumps({"error": f"bad arguments: {exc}"})

    raw_pid = tool_input.get("person_id")
    person_id: int | None
    if raw_pid is None:
        person_id = None
    else:
        try:
            person_id = int(raw_pid)
        except (TypeError, ValueError):
            _audit("set_department_head", "write", False, "person_id not an integer", {"department_slug": department_slug})
            return json.dumps({"error": "person_id must be an integer or null"})

    if person_id is not None and people_store.get_person(person_id) is None:
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: person {person_id} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": f"person {person_id} not found"})

    # Pre-check the department exists so we don't create a head-persona
    # override row for a slug that nobody can reach.
    if dept_store.get_department(department_slug) is None:
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: dept {department_slug!r} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": f"department {department_slug!r} not found"})

    # Create the head persona override row first when assigning a head.
    # `ensure_head_persona_override` is idempotent and only creates a
    # placeholder if none exists, so doing this before the dept UPDATE means
    # we never end up with `dept.head_person_id` set but no matching override
    # row. When clearing the head (person_id is None), there is no override
    # to create.
    try:
        if person_id is not None:
            ensure_head_persona_override(department_slug, person_id)
        ok = dept_store.update_department(department_slug, head_person_id=person_id)
        if ok:
            dept_registry.invalidate()
    except Exception as exc:
        logger.exception("set_department_head: failed")
        _audit(
            "set_department_head", "write", False,
            f"set_department_head FAILED: {exc}",
            {"department_slug": department_slug, "person_id": person_id, "error": str(exc)[:300]},
        )
        return json.dumps({"error": str(exc)})

    if not ok:
        _audit(
            "set_department_head", "write", False,
            f"set_department_head: dept {department_slug!r} not found",
            {"department_slug": department_slug, "person_id": person_id},
        )
        return json.dumps({"error": f"department {department_slug!r} not found"})

    _audit(
        "set_department_head", "write", True,
        f"set_department_head dept={department_slug} person={person_id}",
        {"department_slug": department_slug, "person_id": person_id},
    )
    return json.dumps({
        "status": "ok",
        "department_slug": department_slug,
        "head_person_id": person_id,
    })


async def handle_ask_about_person(input: dict[str, Any]) -> str:
    """Query Honcho's per-person memory via the directional_chat wrapper.

    Lives here (vs. honcho_client) because it's the Anthropic tool
    *handler*: parameter coercion, error JSON shape, and clamping the
    reasoning_level enum to the one Honcho accepts. The actual memory
    call is delegated to :func:`honcho_client.directional_chat`.
    """
    try:
        person_id = int(input["person_id"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"error": "missing or invalid required field: person_id"})
    question = input.get("question", "")
    if not question:
        return json.dumps({"error": "missing required field: question"})
    target_raw = input.get("target_person_id")
    target_person_id: int | None
    if target_raw is None:
        target_person_id = None
    else:
        try:
            target_person_id = int(target_raw)
        except (TypeError, ValueError):
            return json.dumps({"error": "invalid target_person_id"})
    reasoning_level = input.get("reasoning_level", "medium")
    if reasoning_level not in ("minimal", "low", "medium", "high", "max"):
        reasoning_level = "medium"
    answer = await directional_chat(
        person_id,
        question,
        target_person_id=target_person_id,
        reasoning_level=reasoning_level,
    )
    # Empty answer is a normal outcome (Honcho disabled, no data, error
    # swallowed by the wrapper) — surface `found: false` rather than a
    # raw "" so the model knows it's an empty result, not a tool failure.
    return json.dumps(
        {
            "person_id": person_id,
            "target_person_id": target_person_id,
            "answer": answer,
            "found": bool(answer),
        },
        ensure_ascii=False,
    )


PEOPLE_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "list_people": handle_list_people,
    "upsert_person": handle_upsert_person,
    "archive_person": handle_archive_person,
    "set_department_head": handle_set_department_head,
    "ask_about_person": handle_ask_about_person,
}
