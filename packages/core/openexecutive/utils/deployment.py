"""Deployment flags read from the environment.

Whether this process serves an internet-reachable deployment, and whether
``make dev`` started it for local login. ``api.main`` builds its guards on
these, and ``delegation.settings`` needs the same local-login answer without
importing ``api.main`` (which builds the app at import).
"""
from __future__ import annotations

import os

# Values that explicitly mean "not a public deployment". Anything else
# non-empty arms the guard: for a fail-closed check, an unrecognised value
# must err toward requiring the secret, never toward skipping it.
FALSEY_ENV = frozenset({"", "0", "false", "no", "off"})


def is_public_deployment() -> bool:
    """Whether this process is serving an internet-reachable deployment.

    Driven by the explicit ``OE_PUBLIC_DEPLOYMENT`` env var rather than any
    hosting provider's injected variables, so the check works identically on
    every platform (and in plain Docker). See docs/deployment.md.
    """
    return os.environ.get("OE_PUBLIC_DEPLOYMENT", "").strip().lower() not in FALSEY_ENV


def is_local_login() -> bool:
    """Whether `make dev` started this API for local login (no sign-in; see
    packages/ui/src/lib/localLogin.ts). Never on a public deployment."""
    return os.environ.get("OE_LOCAL_LOGIN", "").strip() == "1" and not is_public_deployment()
