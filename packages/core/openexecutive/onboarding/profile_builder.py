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
    _save_wizard_people(state.answers)

    return profile


def _save_wizard_people(answers: dict) -> None:  # type: ignore[type-arg]
    """Create Person rows from wizard org-chart answers. Best-effort."""
    try:
        from openexecutive.people.models import AuthorityScope
        from openexecutive.people.store import (
            initialize_db as init_people_db,
        )
        from openexecutive.people.store import (
            set_authority_scope,
            upsert_person,
        )

        records = build_people_from_answers(answers)
        if not records:
            return
        init_people_db()
        for rec in records:
            raw_scopes: list[str] = rec.pop("authority_scope", [])
            pid = upsert_person(**rec)
            scopes = []
            for tok in raw_scopes:
                with contextlib.suppress(ValueError):
                    scopes.append(AuthorityScope(tok))
            if scopes:
                set_authority_scope(pid, scopes)
    except Exception as exc:
        # Type name only: the traceback would carry the wizard's org-chart
        # answers (names, emails, handles) into the log stream.
        logger.warning(
            "_save_wizard_people failed (%s) — skipping people creation", type(exc).__name__
        )


def load_or_create_profile(path: Path | str | None = None) -> CompanyProfile:
    if path is None:
        from openexecutive.config import get_settings

        path = get_settings().company_profile_path

    return CompanyProfile.load_from_yaml(Path(path))
