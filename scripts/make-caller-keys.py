#!/usr/bin/env python3
"""Make the key pair for signed sign-ins (docs/auth.md, "Signed callers").

The web app signs who is signed in on every request it passes to the API, so
the API no longer takes that on trust from whoever holds
BACKEND_SHARED_SECRET. This prints the two settings:

    uv run --with cryptography python scripts/make-caller-keys.py

- CALLER_ASSERTION_PRIVATE_KEY goes on the web app (packages/ui) only. It
  signs; keep it as secret as BACKEND_SHARED_SECRET, and never give it to the
  API.
- CALLER_ASSERTION_PUBLIC_KEYS goes on the API only. It can check a signature
  but not make one.

Set both and restart both apps. To rotate: make a new pair, add its public
key to the API's list (comma-separated) and restart the API, move the web app
to the new private key, then drop the old public key.
"""
from __future__ import annotations

import argparse
import base64
import re
import secrets
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The same rule as the API's (openexecutive/api/caller.py _KID_RE).
KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pair(kid: str) -> tuple[str, str]:
    """``(private setting, public setting)`` values for a fresh key."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return f"{kid}:{_b64url(seed)}", f"{kid}:{_b64url(public)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--kid",
        help="a name for this key: letters, digits, _ or -, up to 32 (default: random)",
    )
    args = parser.parse_args()
    kid = args.kid or secrets.token_hex(4)
    if not KID_RE.fullmatch(kid):
        print("--kid must be 1-32 letters, digits, _ or -", file=sys.stderr)
        return 2
    private, public = make_pair(kid)
    print("# The web app (packages/ui) only. Secret:")
    print(f"CALLER_ASSERTION_PRIVATE_KEY={private}")
    print("# The API only:")
    print(f"CALLER_ASSERTION_PUBLIC_KEYS={public}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
