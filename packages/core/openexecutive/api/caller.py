"""Who is calling the API: the one place that reads the signed-in caller.

The web UI's proxy (``packages/ui/src/app/api/backend/[...path]/route.ts``)
strips every client-sent ``x-caller-*`` header and stamps ``x-caller-email``
from the verified sign-in; a local-login session sends none. Routes never read
that header themselves. They ask here, so there is one answer to "who is
this?" and one place that decides how far to trust it
(``tests/unit/test_caller.py`` fails on a direct read anywhere else).

``Caller.kind``:

- ``open``: the request is taken at its word. ``email`` is the
  ``x-caller-email`` header as sent, trusted because whoever holds
  ``BACKEND_SHARED_SECRET`` (anyone, with it unset) is trusted as the proxy.
  With no header the request names no one and runs as the principal: the
  CLI's rule, for direct curl and local login.
- ``user``: a signed-in person, verified by the caller-assertion gate.
- ``operator``: the person at the controls, verified by the gate, naming no
  one: runs as the principal, like ``open`` with no header.
- ``service``: a request the gate verified carried no caller at all (a
  script or an MCP client holding only the shared secret). It is never the
  principal.

Only ``open`` is read from the header; the other three come from
``request.state.caller``, which only the gate sets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

CALLER_EMAIL_HEADER = "x-caller-email"

# Audit rows name the caller by email, cut to this length.
MAX_ACTOR_LEN = 200

CallerKind = Literal["open", "user", "operator", "service"]


@dataclass(frozen=True)
class Caller:
    kind: CallerKind
    # The signed-in person's email, stripped and lowercased; "" when the
    # request names no one.
    email: str = ""

    @property
    def defaults_to_principal(self) -> bool:
        """Whether a request that names no one runs as the principal: open
        mode with no email, or an operator. Never a service, never a user."""
        return not self.email and self.kind in ("open", "operator")


def caller(request: Any) -> Caller:
    """The caller of ``request``: what the gate verified, else the header."""
    verified = getattr(getattr(request, "state", None), "caller", None)
    if isinstance(verified, Caller):
        return verified
    raw = request.headers.get(CALLER_EMAIL_HEADER) or ""
    return Caller("open", raw.strip().lower())


def caller_email(request: Any) -> str:
    """The signed-in caller's email, lowercased; "" when there is none."""
    return caller(request).email


def signed_in(request: Any) -> bool:
    """Whether the request carries a signed-in person (not the principal by
    default, not a service)."""
    return bool(caller(request).email)


def actor(request: Any) -> str:
    """Who to name in an audit row: the caller's email, else ``"api"``."""
    return caller(request).email[:MAX_ACTOR_LEN] or "api"


def identity_key(request: Any) -> str:
    """A stable key for the caller when no roster entry names them: their
    email, else ``"local"`` for a request that runs as the principal, else
    ``"service"``, so a service can never hold what the owner started."""
    who = caller(request)
    if who.email:
        return f"email:{who.email}"
    return "local" if who.defaults_to_principal else "service"
