"""Colette's operating profile: charters, goals, cadences, owners, roster.

``seed_default_departments`` ships generic two-line charters, no goals, a daily
09:00 check-in on all eight departments, and no people. For Colette that leaves
the CFO/COO seats without a mandate and the CMO/CPO/CEO seats unmapped to the
humans they partner with. This module layers Colette's profile on top.

Design rules (so it is safe to run against a live database):

* **Applied once per profile version** (sentinel in ``departments_meta``), so
  anything the team edits afterwards in the UI sticks. ``force=True`` re-runs it
  but still only fills things that are untouched.
* **Never overwrites human edits.** Charters are replaced only when still the
  upstream default; cadences only when still the upstream default; goals only
  when the department has none; heads only when unset; people only when no one
  with that name exists.
* **Goals are drafts.** The targets below are starting positions derived from
  the company's stated priorities; the department owners are expected to edit
  them in the Departments UI.
* **Company-specific contact data stays out of the repo.** Names are last-name
  conventions; emails/Slack ids come from an optional, gitignored
  ``company/roster.yaml`` (see ``roster.example.yaml``).

Cadences are UTC. 13:00 UTC is 09:00 US Eastern during daylight time (08:00 in
winter).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from openexecutive.departments.charters import DEFAULT_CHECK_IN_CADENCE, DEFAULT_DEPARTMENTS
from openexecutive.departments.models import (
    DepartmentCharter,
    DepartmentState,
    PeriodType,
)
from openexecutive.people.models import AuthorityScope

logger = logging.getLogger(__name__)

PROFILE_VERSION = "colette_operating_profile_v1"
_QUARTER = "Q4 2026"


# --------------------------------------------------------------------------- #
# Profile data
# --------------------------------------------------------------------------- #

class GoalSeed(BaseModel):
    key_result: str
    target: str
    period_type: PeriodType = "quarter"
    period_value: str = _QUARTER


class DepartmentProfile(BaseModel):
    slug: str
    specialist_key: str | None = None
    charter: DepartmentCharter
    check_in_cadence: str
    #: Roster key (see ``ROSTER``) of the human who owns the department.
    owner: str | None = None
    goals: list[GoalSeed] = Field(default_factory=list)


class PersonSeed(BaseModel):
    key: str
    full_name: str
    role: str
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    scopes: list[AuthorityScope] = Field(default_factory=list)
    reports_to: str | None = None


# Names are the team's last-name convention; edit in the People UI or override
# via company/roster.yaml.
ROSTER: tuple[PersonSeed, ...] = (
    PersonSeed(
        key="ceo",
        full_name="West",
        role="CEO and Chief AI Officer",
        is_principal=True,
        department_slugs=["strategy", "finance", "operations", "legal"],
        scopes=[AuthorityScope.WILDCARD],
    ),
    PersonSeed(
        key="cmo",
        full_name="Wajnberg",
        role="CMO",
        department_slugs=["marketing"],
        scopes=[AuthorityScope.SPEND_LT_2K, AuthorityScope.MEETING_SCHEDULING],
        reports_to="ceo",
    ),
    PersonSeed(
        key="cpo",
        full_name="Dingus",
        role="CPO",
        department_slugs=["product"],
        scopes=[
            AuthorityScope.SPEND_LT_2K,
            AuthorityScope.VENDOR_ONBOARDING,
            AuthorityScope.MEETING_SCHEDULING,
        ],
        reports_to="ceo",
    ),
)

PROFILES: tuple[DepartmentProfile, ...] = (
    DepartmentProfile(
        slug="finance",
        specialist_key="cfo",
        owner="ceo",
        check_in_cadence="weekly@mon-13:00",
        charter=DepartmentCharter(
            mission=(
                "Own the company's financial operating system: know cash and runway every "
                "week, understand what it costs to serve a customer, and keep money and "
                "compliance from ever being the reason growth stalls."
            ),
            scope=[
                "weekly cash, burn and runway",
                "cost-to-serve per family (model/API, messaging, cloud, support)",
                "recurring vendor and cloud spend register",
                "entity, tax and compliance calendar",
                "pricing economics (margin and payback)",
                "fundraising readiness and investor financials",
            ],
            out_of_scope=[
                "legal positions on contracts (Legal)",
                "payroll mechanics and tax filing (outside CPA)",
                "procurement below the approval threshold (Operations)",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Runway known every week from actuals",
                target="Runway of at least 12 months, recomputed each Monday from billing and bank actuals",
            ),
            GoalSeed(
                key_result="Cost-to-serve per family modeled",
                target="Model (model/API, messaging, cloud, support) complete and reviewed before alpha pricing is set",
            ),
            GoalSeed(
                key_result="Recurring spend register complete",
                target="100% of recurring vendor and cloud charges listed with an owner and renewal date; week-over-week variance flagged",
            ),
            GoalSeed(
                key_result="Entity and compliance calendar built",
                target="Every filing and tax deadline for the next 12 months on a calendar with an owner; zero missed",
            ),
        ],
    ),
    DepartmentProfile(
        slug="operations",
        specialist_key="coo",
        owner="ceo",
        check_in_cadence="weekly@thu-13:00",
        charter=DepartmentCharter(
            mission=(
                "Own the operating system that makes growth possible: onboarding, runbooks, "
                "delivery rhythm and dependency risk. A stuck onboarding is an operations "
                "failure, not a customer failure."
            ),
            scope=[
                "alpha-family onboarding pipeline (stage, time-in-stage, blockers)",
                "runbooks for anything done by hand more than twice",
                "delivery rhythm and cycle health",
                "vendor and dependency register with fallbacks",
                "operational risk during infrastructure change",
            ],
            out_of_scope=[
                "product scope decisions (Product)",
                "brand and messaging (Marketing)",
                "vendor contract economics above the approval threshold (Finance)",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Alpha cohort onboarded",
                target="Five alpha families live; every family in a named stage with time-in-stage tracked",
            ),
            GoalSeed(
                key_result="Onboarding runbook written and timed",
                target="Consent-tiered intake to live documented end to end; median time from signup to live recorded",
            ),
            GoalSeed(
                key_result="Per-family messaging sender provisioning is repeatable",
                target="A new family's dedicated sender is provisioned from the runbook with no ad-hoc debugging",
            ),
            GoalSeed(
                key_result="Delivery rhythm healthy",
                target="Weekly cycle review held; no in-progress issue idle more than 7 days without an owner and a plan",
            ),
            GoalSeed(
                key_result="Critical dependencies have documented fallbacks",
                target="Messaging, cloud, model-provider and identity dependencies each have an owner and a fallback",
            ),
            GoalSeed(
                key_result="Cloud migration lands safely",
                target="Tenant #1 migrated with a tested rollback plan and no family data in version-control history",
            ),
        ],
    ),
    DepartmentProfile(
        slug="marketing",
        specialist_key="cmo",
        owner="cmo",
        check_in_cadence="weekly@tue-14:00",
        charter=DepartmentCharter(
            mission=(
                "Support the CMO's ownership of brand, positioning and go-to-market. The "
                "partner seat drafts and challenges; the CMO decides."
            ),
            scope=[
                "brand foundation and voice guardrails",
                "positioning and messaging",
                "launch, referral and channel plans",
                "public claims review (with Product and Legal)",
            ],
            out_of_scope=[
                "spend commitments (CMO approves)",
                "product commitments (Product)",
                "legal and privacy claims (Legal)",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Brand foundation operationalized into assistant guardrails",
                target="Brand harness v1 approved by the CMO and applied to the assistant's behavior",
            ),
            GoalSeed(
                key_result="Public narrative matches what the product can keep",
                target="Every public claim traced to a shipped capability and reviewed by Product and Legal before publishing",
            ),
            GoalSeed(
                key_result="Word-of-mouth path defined before any paid channel",
                target="Friends-and-family learnings captured and a referral path defined",
            ),
        ],
    ),
    DepartmentProfile(
        slug="product",
        specialist_key="cpo",
        owner="cpo",
        check_in_cadence="weekly@wed-14:00",
        charter=DepartmentCharter(
            mission=(
                "Support the CPO's ownership of product. The partner seat (VP of Product) "
                "shapes discovery, specs and sequencing; the CPO decides."
            ),
            scope=[
                "discovery and opportunity assessment",
                "specs with explicit non-goals and consent implications",
                "roadmap sequencing against cost and capacity",
                "core-behavior vs shared-pattern vs private-per-family boundaries",
            ],
            out_of_scope=[
                "deploying or merging changes",
                "brand and messaging (Marketing)",
                "vendor contract terms (Finance, Legal)",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Trust-tiered onboarding live for the alpha cohort",
                target="Scoped, time-boxed, revocable access offers shipped; no broad up-front permission request",
            ),
            GoalSeed(
                key_result="Triage matches families' own judgment",
                target="Alpha families confirm the Right Now / This Week / Reference split fits; misses logged and reviewed weekly",
            ),
            GoalSeed(
                key_result="Tenant isolation holds",
                target="Database-per-tenant verified and no cross-family content leakage in testing",
            ),
        ],
    ),
    DepartmentProfile(
        slug="strategy",
        specialist_key="ceo_advisor",
        owner="ceo",
        check_in_cadence="weekly@fri-15:00",
        charter=DepartmentCharter(
            mission=(
                "Improve the odds the company succeeds: the CEO's challenger-first weekly "
                "review of the success scorecard, open decisions and risks."
            ),
            scope=[
                "weekly company success scorecard",
                "decision log and follow-through",
                "pre-mortems and risk review",
                "ICP, pricing and positioning strategy",
                "capital and milestone planning",
            ],
            out_of_scope=[
                "execution inside other departments",
                "communications sent in the CEO's name",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Success scorecard current every week",
                target="All seven scorecard lines have an owner and a current value each Friday",
            ),
            GoalSeed(
                key_result="ICP and pricing hypotheses tested against alpha evidence",
                target="ICP narrowed and a value-based price tested with alpha families before the cohort closes",
            ),
            GoalSeed(
                key_result="Open decisions closed",
                target="No decision open more than two weeks without an owner and a date",
            ),
        ],
    ),
    DepartmentProfile(
        slug="legal",
        specialist_key="gc",
        owner="ceo",
        check_in_cadence="weekly@fri-13:00",
        charter=DepartmentCharter(
            mission=(
                "Manage legal exposure so growth never outruns consent and compliance. "
                "Qualified counsel makes the final call on legal positions."
            ),
            scope=[
                "entity and corporate housekeeping",
                "privacy policy, terms and consent language",
                "data-handling incidents",
                "vendor and data-processing agreements",
                "trademark and domain",
            ],
            out_of_scope=[
                "tax advice (outside CPA)",
                "litigation (outside counsel)",
            ],
        ),
        goals=[
            GoalSeed(
                key_result="Consent and privacy language reviewed before onboarding families",
                target="Terms, privacy policy and consent flow reviewed by qualified counsel before the next family is onboarded",
            ),
        ],
    ),
)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

@dataclass
class ApplyReport:
    skipped: bool = False
    people_created: list[str] = field(default_factory=list)
    departments_updated: list[str] = field(default_factory=list)
    goals_added: int = 0
    cadences_cleared: list[str] = field(default_factory=list)


def _roster_overrides() -> dict[str, dict[str, Any]]:
    """Optional gitignored ``company/roster.yaml``: per-roster-key contact data."""
    try:
        from openexecutive.config import get_settings

        path = Path(get_settings().company_profile_path).parent / "roster.yaml"
        if not path.exists():
            return {}
        raw = yaml.safe_load(path.read_text()) or {}
        people = raw.get("people", raw) if isinstance(raw, dict) else {}
        return {str(k): v for k, v in people.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 - a bad roster file must not block startup.
        logger.warning("colette_profile: could not read roster.yaml", exc_info=True)
        return {}


_ALLOWED_OVERRIDE_FIELDS = {
    "full_name",
    "role",
    "email",
    "slack_user_id",
    "telegram_chat_id",
    "discord_user_id",
    "preferred_channel",
}


def _seed_people(db_path: Path | None, report: ApplyReport) -> dict[str, int]:
    """Create roster people that do not exist yet. Returns roster key → person id."""
    from openexecutive.people import store as people_store

    overrides = _roster_overrides()
    existing = people_store.list_people(include_archived=True, db_path=db_path)
    by_name = {p.full_name.strip().lower(): p for p in existing if p.id is not None}
    principal = next((p for p in existing if p.is_principal and not p.archived), None)

    ids: dict[str, int] = {}
    for seed in ROSTER:
        extra = {
            k: v
            for k, v in (overrides.get(seed.key) or {}).items()
            if k in _ALLOWED_OVERRIDE_FIELDS
        }
        name = str(extra.pop("full_name", seed.full_name))
        role = str(extra.pop("role", seed.role))

        match = by_name.get(name.strip().lower())
        if match is None and seed.is_principal and principal is not None:
            # A principal already exists under another name: adopt, never add a second.
            match = principal
        if match is not None and match.id is not None:
            ids[seed.key] = match.id
            continue

        person_id = people_store.upsert_person(
            full_name=name,
            role=role,
            is_principal=seed.is_principal,
            department_slugs=seed.department_slugs,
            reports_to_person_id=ids.get(seed.reports_to) if seed.reports_to else None,
            db_path=db_path,
            **extra,
        )
        people_store.set_authority_scope(person_id, list(seed.scopes), db_path=db_path)
        ids[seed.key] = person_id
        report.people_created.append(name)
        logger.info("colette_profile: created person %r (%s)", name, role)
    return ids


def _is_upstream_default(slug: str, charter: DepartmentCharter) -> bool:
    for default_slug, _title, _key, default_charter in DEFAULT_DEPARTMENTS:
        if default_slug == slug:
            return (
                charter.mission == default_charter.mission
                and charter.scope == default_charter.scope
                and charter.out_of_scope == default_charter.out_of_scope
            )
    return False


def _registered_specialists() -> set[str]:
    from openexecutive.orchestrator.router import SPECIALIST_REGISTRY

    return set(SPECIALIST_REGISTRY)


def _apply_department(
    profile: DepartmentProfile,
    state: DepartmentState,
    owner_id: int | None,
    *,
    db_path: Path | None,
    report: ApplyReport,
) -> None:
    from openexecutive.departments import store
    from openexecutive.departments.cadence import cancel_pending_cadence

    cfg = state.config
    registered = _registered_specialists()
    changed = False

    kwargs: dict[str, Any] = {}
    if (
        profile.specialist_key
        and cfg.specialist_key != profile.specialist_key
        and (cfg.specialist_key is None or cfg.specialist_key not in registered)
    ):
        kwargs["specialist_key"] = profile.specialist_key
    if _is_upstream_default(profile.slug, cfg.charter):
        kwargs["charter"] = profile.charter
    cadence_changed = False
    if cfg.cadences.get("check_in", "") in ("", DEFAULT_CHECK_IN_CADENCE):
        kwargs["cadences"] = {**cfg.cadences, "check_in": profile.check_in_cadence}
        cadence_changed = True
    if cfg.head_person_id is None and owner_id is not None:
        kwargs["head_person_id"] = owner_id
    if kwargs:
        store.update_department(profile.slug, db_path=db_path, **kwargs)
        changed = True
    if cadence_changed:
        # Drop the stale pending occurrence so bootstrap re-enqueues at the new time.
        cancel_pending_cadence(profile.slug, db_path=db_path)

    if not state.goals:
        for goal in profile.goals:
            store.insert_goal(
                profile.slug,
                period_type=goal.period_type,
                period_value=goal.period_value,
                key_result=goal.key_result,
                target=goal.target,
                db_path=db_path,
            )
            report.goals_added += 1
        changed = changed or bool(profile.goals)

    if changed:
        report.departments_updated.append(profile.slug)


def reconcile_unrunnable_departments(db_path: Path | None = None) -> list[str]:
    """Stop daily check-ins for departments whose specialist is not registered.

    Departments seeded from upstream (HR, Board Comms) point at specialists this
    deployment does not run; their check-in would call an unknown agent and
    produce nonsense. Clears the ``check_in`` cadence and cancels the pending
    occurrence. Idempotent; returns the slugs cleared.
    """
    from openexecutive.departments import store
    from openexecutive.departments.cadence import cancel_pending_cadence

    registered = _registered_specialists()
    cleared: list[str] = []
    for state in store.list_departments(db_path=db_path):
        cfg = state.config
        if cfg.specialist_key and cfg.specialist_key not in registered and cfg.cadences.get("check_in"):
            remaining = {k: v for k, v in cfg.cadences.items() if k != "check_in"}
            store.update_department(cfg.slug, cadences=remaining, db_path=db_path)
            cancel_pending_cadence(cfg.slug, db_path=db_path)
            cleared.append(cfg.slug)
            logger.warning(
                "colette_profile: department %r points at unregistered specialist %r; "
                "check-in cadence cleared",
                cfg.slug,
                cfg.specialist_key,
            )
    return cleared


def apply_colette_profile(*, force: bool = False, db_path: Path | None = None) -> ApplyReport:
    """Apply the Colette profile (idempotent; see module docstring)."""
    from openexecutive.departments import registry, store

    report = ApplyReport()

    if not force and store.get_meta(PROFILE_VERSION, db_path=db_path):
        report.skipped = True
    else:
        ids = _seed_people(db_path, report)
        for profile in PROFILES:
            state = store.get_department(profile.slug, db_path=db_path)
            if state is None:
                continue  # The team deleted this department; respect that.
            _apply_department(
                profile,
                state,
                ids.get(profile.owner) if profile.owner else None,
                db_path=db_path,
                report=report,
            )
        from datetime import UTC, datetime

        store.set_meta(PROFILE_VERSION, datetime.now(UTC).isoformat(), db_path=db_path)

    report.cadences_cleared = reconcile_unrunnable_departments(db_path=db_path)
    registry.invalidate()
    return report
