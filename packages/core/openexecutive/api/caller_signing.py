"""Making signed-caller keys and assertions: the signing half of
``api/caller.py`` (docs/auth.md, "Signed callers").

The API itself never signs: it holds only public keys. This is for the code
that does, besides the web app: ``scripts/make-caller-keys.py``,
``scripts/mint-caller-assertion.py``, and any program that provisions an API
or calls one as the operator. It needs only ``cryptography`` and the standard
library, so the scripts can import it without the rest of the package.

The wire format is the one ``packages/ui/src/lib/callerAssertion.ts`` signs;
``tests/unit/test_caller_assertion.py`` checks both against the same vectors.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Kept in step with api/caller.py and packages/ui/src/lib/callerAssertion.ts
# (the tests compare all three).
VERSION = "v1"
AUDIENCE = "openexecutive-api"
# Short, so a captured assertion is soon worthless, and well under the API's
# MAX_LIFETIME_S, so a slow clock on either side still leaves room.
LIFETIME_S = 30

KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SEED_RE = re.compile(r"[A-Za-z0-9_-]{43}=?")

SignerKind = Literal["user", "operator"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_key_pair(kid: str | None = None) -> tuple[str, str]:
    """A fresh pair as ``(CALLER_ASSERTION_PRIVATE_KEY value,
    CALLER_ASSERTION_PUBLIC_KEYS entry)``, both ``kid:key``. ``kid`` names the
    key (letters, digits, ``_`` or ``-``, up to 32); random when omitted."""
    kid = kid if kid is not None else secrets.token_hex(4)
    if not KID_RE.fullmatch(kid):
        raise ValueError("a key id is 1-32 letters, digits, _ or -")
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return f"{kid}:{_b64url(seed)}", f"{kid}:{_b64url(public)}"


def load_private_key(setting: str) -> tuple[str, Ed25519PrivateKey]:
    """``kid:seed`` (CALLER_ASSERTION_PRIVATE_KEY) → the key id and key. The
    error never includes the key."""
    kid, sep, seed = (part.strip() for part in setting.strip().partition(":"))
    if not sep or not KID_RE.fullmatch(kid) or not _SEED_RE.fullmatch(seed):
        raise ValueError(
            "CALLER_ASSERTION_PRIVATE_KEY is <key id>:<private key>, as "
            "scripts/make-caller-keys.py prints it"
        )
    raw = base64.urlsafe_b64decode(seed.rstrip("=") + "=")
    return kid, Ed25519PrivateKey.from_private_bytes(raw)


def mint_assertion(
    setting: str,
    *,
    kind: SignerKind,
    email: str,
    method: str,
    target: str,
    now_ms: int | None = None,
    jti: str | None = None,
) -> str:
    """One ``x-caller-assertion`` value, good for one request: ``method``,
    ``target`` (the path with its query, exactly as the request sends it), for
    ``LIFETIME_S``, once. ``kind`` is ``user`` (named by ``email``) or
    ``operator`` (the owner at the controls, naming no one)."""
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
