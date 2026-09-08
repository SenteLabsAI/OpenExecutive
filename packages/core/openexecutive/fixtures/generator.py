"""LLM generation + validation for user-authored company fixtures.

Given a natural-language scenario, asks Claude (via the provider registry, so
OpenRouter overrides still apply) to emit a full fixture bundle through a
forced tool call, then validates it against the same Pydantic models the
curated fixtures use and enforces cross-file referential integrity.

The bundle mirrors what the curated fixtures seed: a company profile, a
people roster, departments (+goals), and episodic memory (decisions,
initiatives, advice, briefing alerts), plus a handful of company docs that
get indexed into ChromaDB on load.

Safety by construction
----------------------
Generated people carry **no contact handles** (no email / Slack / Discord /
Telegram). The curated fixtures deliberately reuse the real team's handles so
demo DMs deliver; a *generated* company must never invent a real-looking
address that a scheduled action could message. We also do not generate
``scheduled_actions`` — past-dated rows fire on the next scheduler tick. The
result is a rich but inert company: history + briefing cards, nothing that
reaches outward.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people.models import AuthorityScope

# Org-facing specialist keys a department may map to. Anything else (or None)
# renders as an informational department. Mirrors orchestrator.router's
# user-facing specialists; board_comms/triage are internal and excluded.
ALLOWED_SPECIALIST_KEYS = {"cso", "cfo", "chro", "gc", "coo", "cmo", "cpo"}

_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_VALID_SCOPES = {s.value for s in AuthorityScope}

PreferredChannel = Literal["email", "slack", "telegram", "discord", "any"]


class GenerationError(RuntimeError):
    """Raised when generation or validation of a fixture bundle fails."""


# ── Bundle model ────────────────────────────────────────────────────────────
# Lean specs (no DB ids, no contact handles) for the parts the model emits.
# ``profile`` reuses CompanyProfile directly — it has no dangerous fields.


class AvailabilitySpec(BaseModel):
    weekdays: list[int] = Field(default_factory=list)
    start_local: str = "09:00"
    end_local: str = "17:00"
    timezone: str = "UTC"


class PersonSpec(BaseModel):
    full_name: str
    role: str = ""
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    preferred_channel: PreferredChannel = "any"
    response_sla_hours: int = 24
    authority_scope: list[str] = Field(default_factory=list)
    availability: list[AvailabilitySpec] = Field(default_factory=list)


class CharterSpec(BaseModel):
    mission: str = ""
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class GoalSpec(BaseModel):
    period_type: str = "quarter"
    period_value: str = ""
    key_result: str = ""
    target: str = ""
    current: str = ""
    status: str = "on_track"


class DepartmentSpec(BaseModel):
    slug: str
    title: str
    specialist_key: str | None = None
    head_person_name: str | None = None
    authority_level: str = "propose_only"
    charter: CharterSpec = Field(default_factory=CharterSpec)
    cadences: dict[str, str] = Field(default_factory=dict)
    headcount: int | None = None
    budget_usd: float | None = None
    goals: list[GoalSpec] = Field(default_factory=list)


class DecisionSpec(BaseModel):
    timestamp: str = ""
    domain: str = "general"
    summary: str = ""
    rationale: str = ""
    outcome: str = ""
    tags: str = ""
    department: str = ""


class InitiativeSpec(BaseModel):
    title: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    summary: str = ""
    department: str = ""


class AdviceSpec(BaseModel):
    timestamp: str = ""
    domain: str = "general"
    query_summary: str = ""
    advice_summary: str = ""
    department: str = ""


class AlertSpec(BaseModel):
    external_id: str = ""
    source: str = "internal"
    severity: str = "medium"
    headline: str = ""
    body: str = ""
    suggested_action: str = ""
    topic_tags: list[str] = Field(default_factory=list)
    dedup_key: str = ""
    status: str = "unread"
    created_at: str = ""
    routed_to_person: str | None = None


class MemorySpec(BaseModel):
    decisions: list[DecisionSpec] = Field(default_factory=list)
    initiatives: list[InitiativeSpec] = Field(default_factory=list)
    advice_given: list[AdviceSpec] = Field(default_factory=list)
    alerts: list[AlertSpec] = Field(default_factory=list)


class DocSpec(BaseModel):
    filename: str
    content: str = ""


class FixtureBundle(BaseModel):
    profile: CompanyProfile
    people: list[PersonSpec] = Field(default_factory=list)
    departments: list[DepartmentSpec] = Field(default_factory=list)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    docs: list[DocSpec] = Field(default_factory=list)


# ── Tool schema for the forced emit call ──────────────────────────────────────

_EMIT_TOOL: dict[str, Any] = {
    "name": "emit_fixture",
    "description": (
        "Emit a complete, internally consistent company fixture for the Open "
        "Executive simulator. Every department head and every alert recipient "
        "MUST be one of the people in `people` (match by full_name exactly). "
        "Generate 5-7 short company docs. Do NOT invent email addresses or "
        "chat handles for anyone."
    ),
    "input_schema": {
        "type": "object",
        "required": ["profile", "people", "departments", "memory", "docs"],
        "properties": {
            "profile": {
                "type": "object",
                "description": "Company profile.",
                "required": ["name", "industry", "stage", "mission"],
                "properties": {
                    "name": {"type": "string"},
                    "industry": {"type": "string"},
                    "stage": {
                        "type": "string",
                        "description": "e.g. 'Seed', 'Series A', 'Series C', 'Private / PE-backed'.",
                    },
                    "founding_year": {"type": "integer"},
                    "headcount": {"type": "integer"},
                    "annual_revenue_arr": {
                        "type": "number",
                        "description": "Annual revenue / ARR in USD (a number, not a string).",
                    },
                    "mission": {"type": "string"},
                    "vision": {"type": "string"},
                    "target_customer": {
                        "type": "object",
                        "properties": {
                            "profile": {"type": "string"},
                            "pain_points": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "competitive_landscape": {
                        "type": "object",
                        "properties": {
                            "primary_competitors": {"type": "array", "items": {"type": "string"}},
                            "competitive_advantages": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "strategic_priorities": {
                        "type": "object",
                        "properties": {
                            "current_year": {"type": "array", "items": {"type": "string"}},
                            "north_star_metric": {"type": "string"},
                        },
                    },
                    "culture": {
                        "type": "object",
                        "properties": {
                            "values": {"type": "array", "items": {"type": "string"}},
                            "operating_principles": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "financials": {
                        "type": "object",
                        "properties": {
                            "burn_rate_monthly": {"type": "number"},
                            "runway_months": {"type": "number"},
                            "key_metrics": {
                                "type": "object",
                                "description": "Flat map of metric name -> value.",
                            },
                        },
                    },
                },
            },
            "people": {
                "type": "array",
                "description": (
                    "Leadership roster (4-6 people). Exactly one should have "
                    "is_principal=true (the founder/CEO). Do NOT include email "
                    "or chat handles."
                ),
                "items": {
                    "type": "object",
                    "required": ["full_name", "role"],
                    "properties": {
                        "full_name": {"type": "string"},
                        "role": {"type": "string"},
                        "is_principal": {"type": "boolean"},
                        "department_slugs": {"type": "array", "items": {"type": "string"}},
                        "preferred_channel": {
                            "type": "string",
                            "enum": ["email", "slack", "telegram", "discord", "any"],
                        },
                        "response_sla_hours": {"type": "integer"},
                        "authority_scope": {
                            "type": "array",
                            "items": {"type": "string", "enum": sorted(_VALID_SCOPES)},
                        },
                    },
                },
            },
            "departments": {
                "type": "array",
                "description": "Org departments (5-8). head_person_name must match a person's full_name.",
                "items": {
                    "type": "object",
                    "required": ["slug", "title", "charter"],
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "lowercase, e.g. 'strategy', 'finance', 'operations'.",
                        },
                        "title": {"type": "string"},
                        "specialist_key": {
                            "type": "string",
                            "enum": sorted(ALLOWED_SPECIALIST_KEYS),
                            "description": (
                                "Maps the department to a specialist agent. Omit for "
                                "informational-only departments."
                            ),
                        },
                        "head_person_name": {"type": "string"},
                        "authority_level": {
                            "type": "string",
                            "enum": ["auto_execute", "propose_only", "escalate"],
                        },
                        "charter": {
                            "type": "object",
                            "required": ["mission"],
                            "properties": {
                                "mission": {"type": "string"},
                                "scope": {"type": "array", "items": {"type": "string"}},
                                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "headcount": {"type": "integer"},
                        "budget_usd": {"type": "number"},
                        "goals": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "period_type": {
                                        "type": "string",
                                        "enum": ["week", "month", "quarter", "year", "ongoing"],
                                    },
                                    "period_value": {"type": "string"},
                                    "key_result": {"type": "string"},
                                    "target": {"type": "string"},
                                    "current": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["on_track", "at_risk", "off_track"],
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "memory": {
                "type": "object",
                "description": "Seeded company history. Use ISO timestamps for decisions/initiatives.",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "timestamp": {"type": "string"},
                                "domain": {"type": "string"},
                                "summary": {"type": "string"},
                                "rationale": {"type": "string"},
                                "outcome": {"type": "string"},
                                "tags": {"type": "string"},
                                "department": {"type": "string"},
                            },
                        },
                    },
                    "initiatives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "status": {"type": "string"},
                                "created_at": {"type": "string"},
                                "updated_at": {"type": "string"},
                                "summary": {"type": "string"},
                                "department": {"type": "string"},
                            },
                        },
                    },
                    "advice_given": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "timestamp": {
                                    "type": "string",
                                    "description": "Relative sentinel like '-1d' or '-2d' reads as recent.",
                                },
                                "domain": {"type": "string"},
                                "query_summary": {"type": "string"},
                                "advice_summary": {"type": "string"},
                                "department": {"type": "string"},
                            },
                        },
                    },
                    "alerts": {
                        "type": "array",
                        "description": "Briefing cards. routed_to_person must match a person's full_name.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "external_id": {"type": "string"},
                                "source": {"type": "string", "enum": ["internal", "external"]},
                                "severity": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high", "urgent"],
                                },
                                "headline": {"type": "string"},
                                "body": {"type": "string"},
                                "suggested_action": {"type": "string"},
                                "topic_tags": {"type": "array", "items": {"type": "string"}},
                                "dedup_key": {"type": "string"},
                                "created_at": {
                                    "type": "string",
                                    "description": "Relative sentinel like '-3h' reads as recent.",
                                },
                                "routed_to_person": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "docs": {
                "type": "array",
                "description": (
                    "5-7 company knowledge docs (company overview, competitive "
                    "landscape, financials, GTM, product roadmap, regulatory, "
                    "operations). Each ~200-400 words of Markdown."
                ),
                "items": {
                    "type": "object",
                    "required": ["filename", "content"],
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "snake_case ending in .md, e.g. 'company_overview.md'.",
                        },
                        "content": {"type": "string", "description": "Markdown body."},
                    },
                },
            },
        },
    },
}

def derive_slug(name: str, exists: Any = None) -> str:
    """Derive a filesystem-safe, unique slug from a company display name.

    ``exists`` is an optional callable ``(slug) -> bool`` reporting whether a
    slug is already taken (curated dir OR generated row); on collision we
    append ``_2``, ``_3``, … Falls back to ``company`` for empty input.
    """
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    base = re.sub(r"_+", "_", base) or "company"
    if not _SAFE_NAME_RE.match(base):
        base = "company"
    if exists is None:
        return base
    candidate = base
    n = 2
    while exists(candidate):
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def validate_bundle(bundle: FixtureBundle) -> list[str]:
    """Return a list of human-readable referential-integrity errors (empty = ok).

    Field-level validation already happened during ``model_validate``; this
    layer enforces the cross-file invariants the seeders rely on.
    """
    errors: list[str] = []

    if not bundle.profile.name.strip():
        errors.append("profile.name is required")
    if not bundle.people:
        errors.append("at least one person is required")

    all_names = [p.full_name for p in bundle.people]
    names = set(all_names)
    if len(names) != len(all_names):
        # Duplicate full_names break head/alert resolution — _seed_people caches
        # name -> id last-wins, so a department head would resolve to whichever
        # duplicate was inserted last rather than the intended person.
        dupes = sorted({n for n in all_names if all_names.count(n) > 1})
        errors.append(f"duplicate full_name(s) in roster: {dupes}")
    principals = [p for p in bundle.people if p.is_principal]
    if len(principals) != 1:
        errors.append(
            f"exactly one person must have is_principal=true (got {len(principals)})"
        )

    dept_slugs = {d.slug for d in bundle.departments}
    for d in bundle.departments:
        if d.head_person_name and d.head_person_name not in names:
            errors.append(
                f"department '{d.slug}' head_person_name "
                f"'{d.head_person_name}' is not in the roster"
            )
    for i, a in enumerate(bundle.memory.alerts):
        if a.routed_to_person and a.routed_to_person not in names:
            errors.append(
                f"alert[{i}] routed_to_person '{a.routed_to_person}' is not in the roster"
            )
    # Department references in memory/people are advisory (the seeders tolerate
    # blanks), so we warn-by-dropping rather than hard-fail. But a person whose
    # entire department_slugs set is unknown usually signals a real mismatch.
    for p in bundle.people:
        unknown = [s for s in p.department_slugs if s and s not in dept_slugs]
        if unknown and len(unknown) == len(p.department_slugs):
            errors.append(
                f"person '{p.full_name}' references only unknown departments: {unknown}"
            )
    return errors


def _safe_doc_filename(raw: str, index: int) -> str:
    """Sanitize a doc filename to a safe, traversal-proof ``*.md`` basename.

    Strips path separators and any chars outside ``[A-Za-z0-9_.-]``, rejects
    all-dot names (``..`` would resolve to the parent dir and crash the write),
    and guarantees a ``.md`` suffix. Used at BOTH generation time
    (``_normalize_bundle``) and load time (``materialize_to_dir``) so a
    hand-crafted bundle posted straight to the DB can't escape the temp dir.
    """
    fn = re.sub(r"[^A-Za-z0-9_.-]+", "_", (raw or "").strip())
    fn = fn.strip(".")  # neutralize "." / ".." and leading/trailing dots
    if not fn:
        fn = f"doc_{index + 1}"
    if not fn.endswith(".md"):
        fn = f"{fn}.md"
    return fn


def _normalize_bundle(bundle: FixtureBundle) -> FixtureBundle:
    """Coerce away unsafe / invalid values before persistence.

    - Drop authority-scope tokens that aren't valid AuthorityScope values.
    - Null out specialist_key values outside the allowed set (→ informational).
    - Ensure doc filenames are unique, safe, and end in .md.
    """
    for p in bundle.people:
        p.authority_scope = [s for s in p.authority_scope if s in _VALID_SCOPES]
    for d in bundle.departments:
        if d.specialist_key not in ALLOWED_SPECIALIST_KEYS:
            d.specialist_key = None

    seen: set[str] = set()
    for i, doc in enumerate(bundle.docs):
        fn = _safe_doc_filename(doc.filename, i)
        while fn in seen:
            fn = f"_{fn}"
        seen.add(fn)
        doc.filename = fn
    return bundle


def bundle_to_serialized(bundle: FixtureBundle, scenario_description: str) -> dict[str, Any]:
    """Serialize a validated bundle into the on-disk fixture layout + summary.

    Returns a dict of the serialized artifacts (matching the curated fixture
    file formats) plus the denormalized summary fields the store keeps for the
    list endpoint.
    """
    bundle = _normalize_bundle(bundle)
    profile = bundle.profile

    profile_yaml = yaml.safe_dump(
        {"company": profile.model_dump()}, default_flow_style=False, sort_keys=True
    )
    people_yaml = yaml.safe_dump(
        {"people": [p.model_dump() for p in bundle.people]}, sort_keys=False
    )
    departments_yaml = yaml.safe_dump(
        {"departments": [d.model_dump() for d in bundle.departments]}, sort_keys=False
    )
    memory_json = json.dumps(bundle.memory.model_dump(), indent=2)
    docs_json = json.dumps({d.filename: d.content for d in bundle.docs})

    # Compact org summaries for the demo cards — same shape the curated list
    # emits (cli.fixture_loader._summarize_departments / _summarize_people).
    dept_summary_json = json.dumps(
        [{"title": d.title or d.slug, "head": d.head_person_name} for d in bundle.departments]
    )
    people_summary_json = json.dumps(
        [
            {"name": p.full_name, "role": p.role or "", "is_principal": bool(p.is_principal)}
            for p in bundle.people
        ]
    )

    return {
        "profile_yaml": profile_yaml,
        "people_yaml": people_yaml,
        "departments_yaml": departments_yaml,
        "memory_json": memory_json,
        "docs_json": docs_json,
        "dept_summary_json": dept_summary_json,
        "people_summary_json": people_summary_json,
        "scenario_description": scenario_description,
        # summary fields
        "display_name": profile.name,
        "industry": profile.industry,
        "stage": profile.stage,
        "arr": profile.annual_revenue_arr,
        "headcount": profile.headcount,
        "founding_year": profile.founding_year,
        "mission": profile.mission,
        "doc_count": len(bundle.docs),
        "scenario_count": 0,
    }


def materialize_to_dir(record: dict[str, Any], target_dir: Path) -> None:
    """Write a stored fixture's serialized artifacts into ``target_dir``.

    ``record`` is a row from ``store.get_fixture`` (or the dict from
    ``bundle_to_serialized``). The resulting directory has the exact shape
    ``cli.fixture_loader._apply_state_from_source`` consumes.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "profile.yaml").write_text(
        record.get("profile_yaml") or "", encoding="utf-8"
    )
    (target_dir / "people.yaml").write_text(
        record.get("people_yaml") or "", encoding="utf-8"
    )
    (target_dir / "departments.yaml").write_text(
        record.get("departments_yaml") or "", encoding="utf-8"
    )
    (target_dir / "memory.json").write_text(
        record.get("memory_json") or "{}", encoding="utf-8"
    )

    docs = json.loads(record.get("docs_json") or "{}")
    if docs:
        docs_dir = target_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        for i, (filename, content) in enumerate(docs.items()):
            safe = _safe_doc_filename(filename, i)
            while safe in seen:
                safe = f"_{safe}"
            seen.add(safe)
            (docs_dir / safe).write_text(content or "", encoding="utf-8")


def _extract_tool_input(response: Any) -> dict[str, Any]:
    """Pull the emit_fixture tool input out of an Anthropic messages response."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_fixture":
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    raise GenerationError("model did not return an emit_fixture tool call")


# Grounded variant of the emit tool for engagement intake: same schema, but
# the description steers toward extraction (fewer docs, no invention). The
# hard grounding rules live in ENGAGEMENT_INTAKE_SYSTEM; this reinforces them
# at the tool boundary.
_ENGAGEMENT_EMIT_TOOL: dict[str, Any] = {
    **_EMIT_TOOL,
    "description": (
        "Emit a structured model of a REAL client company extracted from the "
        "intake material. Every department head and every alert recipient "
        "MUST be one of the people in `people` (match by full_name exactly), "
        "and every person must be named in the material. Leave unstated "
        "numeric fields null — never estimate. Generate 3-6 engagement "
        "working docs (intake brief, current-state summary, stakeholder map, "
        "open questions). Do NOT include email addresses or chat handles for "
        "anyone."
    ),
}


async def generate_fixture_bundle(
    description: str,
    settings: Any | None = None,
    *,
    model: str | None = None,
) -> FixtureBundle:
    """Generate and validate a FICTIONAL fixture bundle (demo simulator path).

    Model and system prompt resolve through the Council-configurable
    ``fixture_generator`` agent — so the model is selectable in the Council UI
    and routes through OpenRouter when enabled. Pass ``model`` to force a
    specific slug (tests/sandbox); otherwise the agent's effective model wins.

    Raises ``GenerationError`` if the model fails to emit a usable bundle, with
    one repair retry on validation failure.
    """
    from openexecutive.agents.fixture_generator import FixtureGeneratorAgent

    return await _generate_bundle(
        description,
        agent=FixtureGeneratorAgent(),
        tool=_EMIT_TOOL,
        user_prefix="Scenario",
        model=model,
    )


async def generate_engagement_bundle(
    description: str,
    settings: Any | None = None,
    *,
    model: str | None = None,
) -> FixtureBundle:
    """Generate a GROUNDED company bundle from real client intake notes.

    The engagement-setup sibling of ``generate_fixture_bundle``: same schema,
    validation, and repair-retry, but driven by the ``engagement_intake``
    agent whose prompt forbids inventing facts — unstated numbers stay null,
    only people named in the notes appear, and the docs are engagement
    working documents (intake brief, open questions) rather than fictional
    knowledge. Output is meant to be saved as a client-slot seed
    (``openexecutive.clients``), not into the demo fixture library.
    """
    from openexecutive.agents.engagement_intake import EngagementIntakeAgent

    return await _generate_bundle(
        description,
        agent=EngagementIntakeAgent(),
        tool=_ENGAGEMENT_EMIT_TOOL,
        user_prefix="Client intake material",
        model=model,
    )


async def _generate_bundle(
    description: str,
    *,
    agent: Any,
    tool: dict[str, Any],
    user_prefix: str,
    model: str | None = None,
) -> FixtureBundle:
    """Shared forced-tool generation loop with one validation-repair retry."""
    if not description or not description.strip():
        raise GenerationError("scenario description is empty")

    from openexecutive.providers.registry import get_provider

    resolved_model = model if model is not None else agent.effective_model()
    system_text = agent.effective_system_prompt()
    provider = get_provider(resolved_model)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"{user_prefix}:\n\n{description.strip()}"}
    ]

    last_errors: list[str] = []
    for attempt in range(2):
        response = await provider.messages_create(
            model=resolved_model,
            max_tokens=16000,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_fixture"},
            messages=messages,
        )
        raw = _extract_tool_input(response)
        try:
            bundle = FixtureBundle.model_validate(raw)
        except ValidationError as exc:
            last_errors = [str(exc)]
            if attempt == 0:
                messages.append({"role": "assistant", "content": json.dumps(raw)[:2000]})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That bundle failed schema validation:\n"
                            f"{exc}\n\nFix it and call emit_fixture again."
                        ),
                    }
                )
                continue
            raise GenerationError(f"bundle failed validation: {exc}") from exc

        errors = validate_bundle(bundle)
        if not errors:
            return bundle
        last_errors = errors
        if attempt == 0:
            messages.append({"role": "assistant", "content": json.dumps(raw)[:2000]})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That bundle has consistency errors:\n- "
                        + "\n- ".join(errors)
                        + "\n\nFix them and call emit_fixture again."
                    ),
                }
            )

    raise GenerationError("generated fixture failed validation: " + "; ".join(last_errors))
