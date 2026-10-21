"""Persisting a reviewed onboarding draft.

Everything here runs AFTER the user has read and edited the draft, and after
``profile.save_to_yaml`` has already succeeded. Each step is best-effort: a
failure to seed people or reconcile departments must never leave the user
without the profile they just saved.

The department step is deliberately **additive**, and that is the one place
this module must not copy ``cli/fixture_loader._seed_departments``. That
seeder DELETEs every department and goal before inserting, which is correct
when swapping in a demo company and destructive here: it would drop
departments the user already has (and their goals) on a re-run of setup. We
match drafted departments onto existing ones and create only what is
genuinely new.

A new install has no departments until this step runs, so the draft decides
which ones exist: none for a solo workspace, and none unless the user's own
words listed people by department. Goals stand on their own: the draft's goals
are saved with an owner, inside a department only when one was drafted. A new
department named after a specialist's area ("Finance", "HR") is wired to that
specialist, the same rule the Executive's ``create_goal`` tool uses, so its
goals get check-ins.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from openexecutive.utils.slug import DEPARTMENT_SLUG_FALLBACK, slugify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.onboarding.interview import DepartmentDraft, GoalDraft, PersonDraft
    from openexecutive.people.models import AuthorityScope

logger = logging.getLogger(__name__)


def save_onboarding_people(drafts: list[PersonDraft]) -> dict[str, int]:
    """Upsert the drafted roster and return ``{full_name: person_id}``.

    Genuinely an upsert: drafts are matched to existing rows by
    case-insensitive ``full_name`` and UPDATEd in place. That matters because
    re-running setup is a supported flow — a plain insert would duplicate
    every person, and ``find_principal_person`` tie-breaks on ``ORDER BY id``,
    so the STALE principal would keep winning caller resolution, alert routing
    and the scheduler's brief (its own docstring warns about exactly this).

    Contact columns are carried across on update. ``upsert_person``'s UPDATE
    branch writes every column, so omitting them would silently wipe the
    Slack / email / Telegram handles the user added on the People page.

    Anyone who was principal and is not the drafted principal is demoted and
    loses WILDCARD (only WILDCARD — other scopes they were granted stay).

    Best-effort: returns ``{}`` on failure and logs the exception TYPE only,
    because the traceback would carry the roster's names into the log stream.
    """
    try:
        from openexecutive.people.models import AuthorityScope
        from openexecutive.people.store import (
            get_person,
            list_people,
            set_authority_scope,
            upsert_person,
        )
        from openexecutive.people.store import (
            initialize_db as init_people_db,
        )

        named = [d for d in drafts if d.full_name.strip()]
        if not named:
            return {}

        init_people_db()
        existing = {p.full_name.strip().lower(): p for p in list_people()}

        ids: dict[str, int] = {}
        for draft in named:
            name = draft.full_name.strip()
            prior = existing.get(name.lower())
            if prior is None:
                pid = upsert_person(
                    full_name=name,
                    role=draft.role.strip(),
                    is_principal=draft.is_principal,
                )
            else:
                pid = upsert_person(
                    person_id=prior.id,
                    full_name=name,
                    # An empty drafted role means "not mentioned", not "clear it".
                    role=draft.role.strip() or prior.role,
                    is_principal=draft.is_principal,
                    # Carried across so the UPDATE does not null them out.
                    department_slugs=prior.department_slugs,
                    email=prior.email,
                    slack_user_id=prior.slack_user_id,
                    telegram_chat_id=prior.telegram_chat_id,
                    discord_user_id=prior.discord_user_id,
                    preferred_channel=prior.preferred_channel,
                    response_sla_hours=prior.response_sla_hours,
                    on_leave_until=prior.on_leave_until,
                    reports_to_person_id=prior.reports_to_person_id,
                )
            ids[name] = pid
            # Register immediately: two spellings of one name resolve to the
            # same row, and a stale snapshot would insert a duplicate instead.
            refreshed = get_person(pid)
            if refreshed is not None:
                existing[name.lower()] = refreshed

            if draft.is_principal:
                set_authority_scope(pid, [AuthorityScope.WILDCARD])
            elif prior is not None and prior.is_principal:
                # Demoted but still on the roster — _demote_stale_principals
                # skips them (they ARE in keep_ids), so strip WILDCARD here or
                # they keep blanket approval authority.
                _strip_wildcard(pid, prior.authority_scope)

        _demote_stale_principals(set(ids.values()))
        return ids
    except Exception as exc:
        logger.warning(
            "save_onboarding_people failed (%s) — skipping people creation",
            type(exc).__name__,
        )
        return {}


def _demote_stale_principals(keep_ids: set[int]) -> None:
    """Strip is_principal + WILDCARD from anyone outside the new roster.

    Without this, re-running setup leaves the previous founder flagged as
    principal with blanket approval authority, and ``find_principal_person``
    (oldest id wins) keeps resolving to them.
    """
    from openexecutive.people.store import (
        list_people,
        upsert_person,
    )

    for person in list_people():
        if not person.is_principal or person.id is None or person.id in keep_ids:
            continue
        upsert_person(
            person_id=person.id,
            full_name=person.full_name,
            role=person.role,
            is_principal=False,
            department_slugs=person.department_slugs,
            email=person.email,
            slack_user_id=person.slack_user_id,
            telegram_chat_id=person.telegram_chat_id,
            discord_user_id=person.discord_user_id,
            preferred_channel=person.preferred_channel,
            response_sla_hours=person.response_sla_hours,
            on_leave_until=person.on_leave_until,
            reports_to_person_id=person.reports_to_person_id,
        )
        _strip_wildcard(person.id, person.authority_scope)


def _strip_wildcard(person_id: int, current: list[AuthorityScope]) -> None:
    """Remove WILDCARD, leaving any other scope the person was granted."""
    from openexecutive.people.models import AuthorityScope as Scope
    from openexecutive.people.store import set_authority_scope

    remaining: list[Scope] = [s for s in current if s != Scope.WILDCARD]
    if len(remaining) != len(current):
        set_authority_scope(person_id, remaining)


# Loose on purpose: Google checks the address for real at sign-in. This only
# stops a typo or a stray sentence from becoming the owner's roster address.
_OWNER_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# RFC 5321's limit on a forward path.
_OWNER_EMAIL_MAX_LEN = 254


class OwnerEmailError(ValueError):
    """The owner email from the review screen can't be saved. The message is a
    fixed, input-free string, safe to return as an HTTP detail."""


def check_owner_email(
    raw: str | None, principal_name: str, *, caller_email: str | None = None
) -> str | None:
    """Normalise the owner's sign-in email, or return None when left blank.

    Runs BEFORE the commit writes anything, and setup only ever FILLS IN a
    missing email. Raises OwnerEmailError when the value is not one plausible
    address; when another non-archived person holds it (two rows sharing an
    email would resolve sign-in to the older one); when the drafted
    principal's own row already has a different email (replacing it would
    sign the owner out — web sign-in and caller resolution both key on it —
    so the owner changes it on the People page, signed in with the current
    one); or when the roster can't be read. "The drafted principal's row" is
    the one save_onboarding_people's name-keyed upsert will update — compared
    by id, because two rows can share a name and the upsert then takes the
    last one.
    """
    email = (raw or "").strip().lower()
    if not email:
        return None
    if len(email) > _OWNER_EMAIL_MAX_LEN or not _OWNER_EMAIL_RE.fullmatch(email):
        raise OwnerEmailError("That email address doesn't look right. Check it and try again.")
    from openexecutive.people.store import find_person_by_address, list_people

    key = principal_name.strip().lower()
    try:
        # A teammate's alias counts as theirs, like their address.
        holder = find_person_by_address(email)
        # The same dict save_onboarding_people builds, so the same row wins.
        own_row = {p.full_name.strip().lower(): p for p in list_people()}.get(key)
    except (OSError, sqlite3.Error) as exc:
        # Unknown is not "free": saving anyway could give two people one email.
        logger.warning("check_owner_email: lookup failed (%s)", type(exc).__name__)
        raise OwnerEmailError("Could not check that email just now. Try again.") from exc
    if holder is not None and (own_row is None or holder.id != own_row.id):
        if own_row is None and _is_own_seat(holder, email, caller_email):
            # The signed-in owner's own entry under another name (the one a
            # hosted sign-up seeds from the address, or an old spelling).
            # adopt_owner_row renames it before the roster is saved.
            return email
        if holder.is_principal:
            raise OwnerEmailError(
                "That email is on the current owner's People entry, but setup would update "
                "a different one (another name, or two entries share this name). Use the "
                "owner's name exactly as on the People page, or leave the email blank."
            )
        raise OwnerEmailError(
            "That email already belongs to someone else on the People page. "
            "Use the owner's own email, or leave it blank."
        )
    if own_row is not None and own_row.email and own_row.email.strip().lower() != email:
        raise OwnerEmailError(
            "The owner's People entry already has a different sign-in email, and setup "
            "never replaces it. Keep that one here, or sign in with it and change it on "
            "the People page."
        )
    return email


def _is_own_seat(holder: Any, email: str, caller_email: str | None) -> bool:
    """Whether ``holder`` is the signed-in caller's own principal entry: it is
    the principal, it carries the address being saved, and that address is
    the one the caller signed in with."""
    return bool(
        holder.is_principal
        and caller_email
        and email == caller_email.strip().lower()
        and (holder.email or "").strip().lower() == email
    )


def adopt_owner_row(email: str | None, principal_name: str, caller_email: str | None) -> bool:
    """Rename the caller's own principal entry to the name setup drafted.

    A hosted sign-up seeds the owner's People entry from the sign-in address
    when no name was given ("ada" for ada@example.com), and the interview
    drafts the real name. The name-keyed upsert would then add a second row
    and the email check would reject the save, so the matched entry takes
    the drafted name first; the upsert finds it by that name and updates it.
    Returns True when a row was renamed. Best-effort: a failure leaves the
    roster as it was and the save goes on.
    """
    email = (email or "").strip().lower()
    key = principal_name.strip().lower()
    if not email or not key:
        return False
    from openexecutive.people.store import find_person_by_address, list_people, update_person

    try:
        holder = find_person_by_address(email)
        if holder is None or holder.id is None or not _is_own_seat(holder, email, caller_email):
            return False
        if holder.full_name.strip().lower() == key:
            return False
        if any(p.full_name.strip().lower() == key for p in list_people()):
            return False
        return update_person(holder.id, full_name=principal_name.strip())
    except (OSError, sqlite3.Error) as exc:
        logger.warning("adopt_owner_row failed (%s)", type(exc).__name__)
        return False


def owner_change_blocked(principal_name: str, caller_person_id: int | None) -> bool:
    """Whether this commit would make a different person the owner while the
    caller is not the current owner.

    The seed demotes whoever was principal and grants WILDCARD to the drafted
    one, so re-running setup is also a way to take the owner's seat: any
    signed-in user can open /onboard. Keeping the current owner is open to
    everyone, as is a first setup (no principal yet); handing the role to
    another People row — including a new row, as a renamed owner becomes
    under the name-keyed upsert — takes the current owner. A caller with no
    x-caller-email (the CLI, local login) resolves to the principal already.
    Lookup errors propagate: the route turns them into a retryable 503.
    """
    from openexecutive.people.store import find_principal_person, is_principal_or_self, list_people

    current = find_principal_person()
    if current is None:
        return False
    # The same dict save_onboarding_people builds, so the same row wins.
    own_row = {p.full_name.strip().lower(): p for p in list_people()}.get(
        principal_name.strip().lower()
    )
    if own_row is not None and own_row.id == current.id:
        return False
    return caller_person_id is None or not is_principal_or_self(caller_person_id, None)


def owner_email_blocked(email: str | None, caller_person_id: int | None, caller_email: str) -> bool:
    """Whether someone who isn't the owner is filling in the owner's missing
    email with an address other than the one they signed in with.

    The roster is the web sign-in allow-list, so an address of their choosing
    would let them sign in with it as the owner. Their own is allowed: it is
    how an owner the app can't recognise yet links theirs, and a rostered
    teammate's own is already on their entry, which check_owner_email
    refuses. Lookup errors propagate (the route answers 503).
    """
    from openexecutive.people.store import find_principal_person, is_principal_or_self

    if not email:
        return False
    current = find_principal_person()
    if current is None or is_principal_or_self(caller_person_id, None):
        return False
    if (current.email or "").strip().lower() == email:
        return False
    return email != caller_email


def link_owner_email(person_id: int, email: str) -> bool:
    """Fill in the principal's sign-in email.

    Never replaces a different one, and never gives an email two owners:
    check_owner_email refuses both before anything is written, and this
    re-checks the row it is about to change. Best-effort, like the rest of
    this module — the profile is already saved, and a failure here only leaves
    the owner's email missing, as before this step existed: running setup
    again adds it.
    """
    try:
        from openexecutive.people.store import find_person_by_address, get_person, update_person

        person = get_person(person_id)
        holder = find_person_by_address(email)
        if (
            person is None
            or (person.email and person.email.strip().lower() != email)
            or (holder is not None and holder.id != person_id)
        ):
            return False
        return update_person(person_id, email=email)
    except Exception as exc:
        logger.warning("link_owner_email failed (%s)", type(exc).__name__)
        return False


def reconcile_onboarding_departments(
    drafts: list[DepartmentDraft],
    person_ids: dict[str, int],
) -> dict[str, int]:
    """Additively apply the drafted departments. Returns ``{updated, created}``.

    A drafted department matches an existing one by slug or case-insensitive
    title; a match is UPDATED in place (preserving its ``specialist_key`` and
    goals), anything unmatched is CREATED. Nothing is ever deleted — a
    department the user did not mention is left exactly as it was.
    """
    counts = {"updated": 0, "created": 0}
    try:
        from openexecutive.departments.models import DepartmentCharter
        from openexecutive.departments.store import (
            create_department,
            list_departments,
            match_department,
            specialist_key_for_area,
            update_department,
        )
        from openexecutive.departments.store import (
            initialize_db as init_departments_db,
        )

        named = [d for d in drafts if d.title.strip()]
        if not named:
            return counts

        init_departments_db()
        known = [d.config for d in list_departments()]
        # Two drafted titles can collide with each other as well as with an
        # existing row ("Growth" and "growth"). Without this the second one
        # would either create a `growth-2` row or silently overwrite the first
        # one's mission while the counts claimed two updates.
        seen: set[str] = set()

        for draft in named:
            title = draft.title.strip()
            # Same fallback the store uses, so the slug we look up is the one
            # create_department would have assigned.
            drafted_slug = slugify(title, fallback=DEPARTMENT_SLUG_FALLBACK)
            # By slug or case-insensitive title — the store's shared rule,
            # also used by the Executive's create_goal tool.
            match = match_department(title, known)
            # Key on the row actually being touched. Keying on the drafted slug
            # alone missed the match-by-TITLE case: the shipped `hr` department
            # is titled "People & Talent", so drafts "HR" and "People & Talent"
            # have different slugs but resolve to the same row — the second
            # silently clobbered the first while counts claimed two updates.
            key = match.slug if match is not None else drafted_slug
            if key in seen:
                logger.info(
                    "onboarding: skipping a department that resolves to one "
                    "already handled in this draft"
                )
                continue
            seen.add(key)

            head_id = person_ids.get(draft.head_person_name.strip()) if draft.head_person_name else None

            if match is None:
                candidate = specialist_key_for_area(title)
                # One department per specialist: a second would make the
                # specialist -> department mapping ambiguous.
                specialist_key = (
                    candidate
                    if candidate and not any(k.specialist_key == candidate for k in known)
                    else None
                )
                created = create_department(
                    title, mission=draft.mission.strip(), specialist_key=specialist_key
                )
                counts["created"] += 1
                slug = created.config.slug
                # Register it so a later draft in this same batch matches the
                # row we just made instead of creating a `-2` duplicate.
                known.append(created.config)
                # create_department fixes authority to propose_only and takes no
                # head, so apply the drafted values in a second call.
                update_department(slug, authority_level=draft.authority_level)
                if head_id is not None:
                    update_department(slug, head_person_id=head_id)
                continue

            # Keep the existing charter's scope/out_of_scope — the interview
            # never asks about them, so an empty draft must not erase them.
            charter = DepartmentCharter(
                mission=draft.mission.strip() or match.charter.mission,
                scope=list(match.charter.scope),
                out_of_scope=list(match.charter.out_of_scope),
            )
            update_department(
                match.slug,
                charter=charter,
                authority_level=draft.authority_level,
            )
            # Passed separately: update_department treats an explicit None as
            # "clear the head", and a draft with no named head must leave an
            # existing head alone.
            if head_id is not None:
                update_department(match.slug, head_person_id=head_id)
            counts["updated"] += 1

        return counts
    except Exception as exc:
        # Do NOT claim "unchanged": each department is its own transaction, so
        # a failure partway through leaves the earlier ones committed.
        logger.warning(
            "reconcile_onboarding_departments failed (%s) after %d update(s) "
            "and %d creation(s)",
            type(exc).__name__,
            counts["updated"],
            counts["created"],
        )
        return counts
    finally:
        # Must run even on the partial-failure path above, or the registry
        # serves a stale department list until its TTL lapses.
        if counts["updated"] or counts["created"]:
            try:
                from openexecutive.departments.registry import invalidate

                invalidate()
            except Exception:
                logger.warning("onboarding: department registry invalidate failed")


def save_onboarding_goals(
    drafts: list[GoalDraft],
    person_ids: dict[str, int],
    principal_id: int | None,
) -> int:
    """Add the drafted goals for this year. Returns how many were created.

    Additive like the departments: a goal whose wording matches one that
    already exists (any department, case-insensitive) is left alone, so
    re-running setup does not duplicate goals. A goal's owner is the named
    person, else the principal; its department is the drafted one it names,
    matched the way reconcile_onboarding_departments matched it, else none.
    Best-effort: logs the exception type only and keeps what was saved.
    """
    created = 0
    try:
        from openexecutive.departments.store import (
            initialize_db as init_departments_db,
        )
        from openexecutive.departments.store import (
            insert_goal,
            list_all_goals,
            list_departments,
            match_department,
        )
        from openexecutive.memory.workspace_settings import get_user_timezone

        named = [g for g in drafts if g.title.strip()]
        if not named:
            return 0
        init_departments_db()
        known = [d.config for d in list_departments()]
        seen = {g.key_result.strip().lower() for g in list_all_goals()}
        ids = {name.strip().lower(): pid for name, pid in person_ids.items()}
        year = str(datetime.now(get_user_timezone()).year)

        for draft in named:
            title = draft.title.strip()
            if title.lower() in seen:
                continue
            seen.add(title.lower())
            owner = ids.get(draft.owner_name.strip().lower()) if draft.owner_name.strip() else None
            department = (
                match_department(draft.department_title.strip(), known)
                if draft.department_title.strip()
                else None
            )
            insert_goal(
                department.slug if department is not None else None,
                period_type="year",
                period_value=year,
                key_result=title,
                target=draft.target.strip(),
                # Explicit even for the principal: a goal in a department
                # would otherwise fall to its head, not to whoever was named.
                owner_person_id=owner if owner is not None else principal_id,
            )
            created += 1
        return created
    except Exception as exc:
        logger.warning(
            "save_onboarding_goals failed (%s) after %d goal(s)", type(exc).__name__, created
        )
        return created


def derive_org_structure(
    profile: CompanyProfile,
    people: list[PersonDraft],
    departments: list[DepartmentDraft],
    goals: list[GoalDraft] | None = None,
) -> CompanyProfile:
    """Fill ``profile.org_structure`` from the people and department drafts,
    and this year's priorities from the goals when there are any.

    The onboarding review screen hides the Org Structure and priorities
    sections precisely so these stay derived values — otherwise the user
    would have two places to edit the same list and they would silently
    diverge.
    """
    titles = [d.title.strip() for d in departments if d.title.strip()]
    leadership = [
        f"{p.full_name.strip()}, {p.role.strip()}" if p.role.strip() else p.full_name.strip()
        for p in people
        if p.full_name.strip()
    ]
    org = profile.org_structure.model_copy(
        update={"departments": titles, "leadership_team": leadership}
    )
    update: dict[str, Any] = {"org_structure": org}
    goal_titles = [g.title.strip() for g in goals or [] if g.title.strip()]
    if goal_titles:
        update["strategic_priorities"] = profile.strategic_priorities.model_copy(
            update={"current_year": goal_titles}
        )
    return profile.model_copy(update=update)
