#!/usr/bin/env python3
"""Sign one API request as a caller, for curl against an API with signed
sign-ins on (docs/auth.md, "Signed callers").

With CALLER_ASSERTION_PUBLIC_KEYS set on the API, a request that carries only
x-api-key is a service: it is never the owner. To call an owner-only route
from a terminal, sign it with the web app's private key:

    export CALLER_ASSERTION_PRIVATE_KEY=...   # the web app's
    curl -H "x-api-key: $BACKEND_SHARED_SECRET" \\
         -H "x-caller-assertion: $(uv run --with cryptography python \\
               scripts/mint-caller-assertion.py GET /today)" \\
         "$OE_API/today"

It signs as the operator (the owner at the controls) unless --email names
the signed-in person to sign as. One assertion is good for one request: that
method, that exact path and query (as sent, e.g. /audit/logs?limit=5), for 60
seconds, once. Never print or store the private key.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Kept in step with openexecutive/api/caller.py and
# packages/ui/src/lib/callerAssertion.ts (the tests compare all three).
VERSION = "v1"
AUDIENCE = "openexecutive-api"
LIFETIME_S = 60
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SEED_RE = re.compile(r"[A-Za-z0-9_-]{43}=?")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_private_key(setting: str) -> tuple[str, Ed25519PrivateKey]:
    """``kid:seed`` (CALLER_ASSERTION_PRIVATE_KEY) → the key id and key."""
    kid, sep, seed = (part.strip() for part in setting.strip().partition(":"))
    if not sep or not _KID_RE.fullmatch(kid) or not _SEED_RE.fullmatch(seed):
        raise ValueError(
            "CALLER_ASSERTION_PRIVATE_KEY is <key id>:<private key>, as "
            "scripts/make-caller-keys.py prints it"
        )
    raw = base64.urlsafe_b64decode(seed.rstrip("=") + "=")
    return kid, Ed25519PrivateKey.from_private_bytes(raw)


def mint(
    setting: str,
    *,
    kind: str,
    email: str,
    method: str,
    target: str,
    now_ms: int | None = None,
    jti: str | None = None,
) -> str:
    """One ``x-caller-assertion`` value. ``target`` is the path with its query,
    exactly as the request will send it."""
    if kind not in ("user", "operator"):
        raise ValueError("kind is user or operator")
    sub = email.strip().lower() if kind == "user" else ""
    if kind == "user" and "@" not in sub:
        raise ValueError("a user is named by their email")
    if kind == "operator" and email.strip():
        raise ValueError("the operator names no one")
    kid, key = load_private_key(setting)
    iat = (int(time.time() * 1000) if now_ms is None else now_ms) // 1000
    claims = {
        "aud": AUDIENCE,
        "exp": iat + LIFETIME_S,
        "iat": iat,
        "jti": jti or secrets.token_urlsafe(18),
        "kid": kid,
        "kind": kind,
        "m": method.upper(),
        "p": target,
        "sub": sub,
    }
    # Sorted keys and no spaces: byte for byte what the web app signs.
    body = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    signing_input = f"{VERSION}.{_b64url(body.encode('utf-8'))}"
    return f"{signing_input}.{_b64url(key.sign(signing_input.encode('ascii')))}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("method", help="GET, POST, PUT, PATCH or DELETE")
    parser.add_argument("target", help="the path with its query, as curl will send it")
    parser.add_argument("--email", default="", help="sign as this signed-in person instead")
    args = parser.parse_args()
    setting = os.environ.get("CALLER_ASSERTION_PRIVATE_KEY", "")
    if not setting.strip():
        print("Set CALLER_ASSERTION_PRIVATE_KEY (the web app's) first.", file=sys.stderr)
        return 2
    if not args.target.startswith("/"):
        print("The path starts with / (the query, if any, included).", file=sys.stderr)
        return 2
    try:
        print(
            mint(
                setting,
                kind="user" if args.email else "operator",
                email=args.email,
                method=args.method,
                target=args.target,
            )
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
