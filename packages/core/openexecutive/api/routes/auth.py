"""Auth-facing surface — the People roster as one of two inputs to who
can sign in via Google OAuth on the UI.

The Next.js layer's ``auth.ts`` calls ``GET /auth/allowed-emails`` during
the NextAuth ``signIn`` callback to decide whether to admit a logged-in
Google user. Serving the roster here means adding someone to your team is
also how you grant them access — but it is **additive**, not a
replacement: the UI unions this list with its own ``ALLOWED_EMAILS`` env
var, so a roster change can never revoke an email the operator configured
through the environment (issue #132).

This route is still gated by the shared-secret middleware — the UI's
server-side fetch carries ``x-api-key`` so an unauthenticated caller
can't enumerate emails.

``GET /auth/me`` returns the email this request is made as
(``api.caller``), so a host origin's own page (docs/auth.md, "Host
origins") can warn when the session signed in here is someone else's, as
the framed pages do with the session they hold.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from openexecutive.people import store as people_store

router = APIRouter()


class SignedInAs(BaseModel):
    # Lowercased; "" when the request names no one.
    email: str


class AllowedEmail(BaseModel):
    email: str
    person_id: int


@router.get("/auth/allowed-emails", response_model=list[AllowedEmail])
def allowed_emails() -> list[AllowedEmail]:
    """Return ``[{email, person_id}]`` for every non-archived Person with an email.

    Empty list when no people are seeded yet (fresh install); the UI then
    relies on ``ALLOWED_EMAILS`` alone, so a brand-new operator can sign in
    and run onboarding.

    This list is **additive**. The UI admits an email that appears here *or*
    in its ``ALLOWED_EMAILS`` env var — this endpoint reports the roster and
    nothing else, and the union happens in ``auth.ts``. Removing a Person
    therefore does not revoke access on its own if their email is also in
    that env var.
    """
    return [
        AllowedEmail(email=p.email.lower(), person_id=p.id)  # type: ignore[arg-type]
        for p in people_store.list_people()
        if p.email and p.id is not None
    ]


@router.get("/auth/me", response_model=SignedInAs)
def signed_in_as(request: Request) -> SignedInAs:
    """The email this request is made as: the signed-in person's for a call
    through the web app (verified when signed callers are on), else what
    the caller header names. Nothing about them beyond what the caller
    already sent."""
    from openexecutive.api.caller import caller

    return SignedInAs(email=caller(request).email)
