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
method, that exact path and query (as sent, e.g. /audit/logs?limit=5), for 30
seconds, once. Never print or store the private key.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages" / "core"))

from openexecutive.api.caller_signing import mint_assertion  # noqa: E402


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
            mint_assertion(
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
