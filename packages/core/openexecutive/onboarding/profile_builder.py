from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding.wizard import (
    WizardState,
    build_people_from_answers,
    build_profile_from_answers,
)

logger = logging.getLogger(__name__)


def build_and_save_profile(
    state: WizardState,
    profile_path: Path | str | None = None,
    *,
    principal_email: str | None = None,
) -> CompanyProfile:
    if profile_path is None:
        from openexecutive.config import get_settings

        profile_path = get_settings().company_profile_path

    profile_path = Path(profile_path)
    raw = build_profile_from_answers(state.answers)
    profile = CompanyProfile.model_validate(raw)
    profile.save_to_yaml(profile_path)

    # Persist any people extracted from wizard org-chart steps (Phase 3).
    # Failures are logged and swallowed — a people-persistence error must
    # never block the profile save.
    _save_wizard_people(state.answers, principal_email=principal_email)

    return profile


def _save_wizard_people(  # type: ignore[type-arg]
    answers: dict, *, principal_email: str | None = None
) -> None:
    """Create People rows, binding first-run principal to the verified email."""
    try:
        from openexecutive.people import registry as people_registry
        from openexecutive.people import store as people_store
        from openexecutive.people.models import AuthorityScope

        records = build_people_from_answers(answers)
        if not records:
            return
        people_store.initialize_db()
        existing_principal = people_store.find_principal_person()
        for rec in records:
            record = dict(rec)
            if record.get("is_principal"):
                # A second onboarding run must not mint a new instance admin.
                if existing_principal is not None:
                    # The local CLI recovery path binds an older principal
                    # record created before browser identity binding existed.
                    if (
                        existing_principal.id is not None
                        and principal_email
                        and principal_email.strip()
                        and (existing_principal.email or "").lower()
                        != principal_email.strip().lower()
                    ):
                        people_store.update_person(
                            existing_principal.id,
                            email=principal_email.strip().lower(),
                        )
                    if (
                        existing_principal.id is not None
                        and AuthorityScope.WILDCARD not in existing_principal.authority_scope
                    ):
                        people_store.set_authority_scope(
                            existing_principal.id,
                            [*existing_principal.authority_scope, AuthorityScope.WILDCARD],
                        )
                    logger.warning("onboarding skipped duplicate principal record")
                    continue
                # The caller email is supplied only by the authenticated UI
                # proxy (or explicitly by the local CLI). It binds the
                # bootstrap principal to the administrator identity.
                if principal_email and principal_email.strip():
                    record["email"] = principal_email.strip().lower()
            raw_scopes: list[str] = record.pop("authority_scope", [])
            pid = people_store.upsert_person(**record)
            scopes = []
            for tok in raw_scopes:
                with contextlib.suppress(ValueError):
                    scopes.append(AuthorityScope(tok))
            if scopes:
                people_store.set_authority_scope(pid, scopes)
        people_registry.invalidate()
    except Exception:
        logger.warning("_save_wizard_people failed — skipping people creation", exc_info=True)


def load_or_create_profile(path: Path | str | None = None) -> CompanyProfile:
    if path is None:
        from openexecutive.config import get_settings

        path = get_settings().company_profile_path

    return CompanyProfile.load_from_yaml(Path(path))
