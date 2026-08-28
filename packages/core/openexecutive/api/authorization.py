"""Route dependencies for permissions derived from the trusted UI identity."""
from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from openexecutive.config import get_settings
from openexecutive.people.store import find_person_by_email, find_principal_person


def configured_principal_email() -> str:
    """Read the recovery identity from env or Settings' local .env source."""
    configured = os.environ.get("PRINCIPAL_EMAIL", "").strip()
    if configured:
        return configured.lower()
    try:
        return (get_settings().principal_email or "").strip().lower()
    except Exception:
        # A settings parse failure must not turn an unconfigured recovery path
        # into an authorization failure; normal principal lookup still applies.
        return ""


def require_principal(
    x_caller_email: Annotated[
        str | None, Header(alias="X-Caller-Email")
    ] = None,
) -> None:
    """Require a principal identified by the trusted UI proxy.

    The shared-secret middleware is the outer boundary for API traffic. The UI
    proxy strips all client-provided ``x-caller-*`` headers and stamps this
    value from the verified NextAuth session before it reaches FastAPI.
    """
    if not x_caller_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Principal access required.",
        )
    caller = find_person_by_email(x_caller_email)
    principal = find_principal_person()
    # The canonical principal is deterministic even when legacy/imported data
    # contains multiple principal flags. Only that record may administer
    # instance-wide resources such as the single Codex App Server session.
    if caller is not None and principal is not None and caller.id == principal.id:
        return

    # A deployment-controlled override recovers legacy databases whose
    # principal predates browser-email binding (including a wrong/null row).
    # It is deliberately configuration-only: an ordinary roster member can
    # never claim this authority through an HTTP request.
    configured_principal = configured_principal_email()
    if configured_principal and configured_principal == x_caller_email.strip().lower():
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Principal access required.",
    )


PrincipalOnly = Annotated[None, Depends(require_principal)]
