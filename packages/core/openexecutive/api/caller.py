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
- ``user``: a signed-in person, verified by ``caller_gate``.
- ``operator``: the person at the controls (local login), verified by the
  gate, naming no one: runs as the principal, like ``open`` with no header.
- ``service``: a request the gate saw carry no caller at all (a script or an
  MCP client holding only the shared secret). It is never the principal.

**Signed callers.** With ``CALLER_ASSERTION_PUBLIC_KEYS`` set, the API stops
taking the header at its word. The UI proxy signs who is calling with an
Ed25519 key only it holds (``CALLER_ASSERTION_PRIVATE_KEY``,
``packages/ui/src/lib/callerAssertion.ts``) and sends that as
``x-caller-assertion``; ``caller_gate`` checks it on every request and sets
``request.state.caller``, which is what ``caller`` then answers:

- a valid assertion: ``user`` (its ``sub`` is the email) or ``operator``;
- no caller header at all: ``service``;
- anything else is a 401: an ``x-caller-email`` with no assertion, a bad
  signature, an unknown key, the wrong audience, method or path, an expired
  or reused assertion, or an ``x-caller-email`` that names someone else.

An assertion is ``v1.<payload>.<signature>``: base64url JSON claims
``{aud, exp, iat, jti, kid, kind, m, p, sub}``, signed over ``v1.<payload>``.
Each is good for one request: that method, that path and query exactly as the
API receives them, at most ``MAX_LIFETIME_S`` seconds, and its ``jti`` once
(this process remembers them; the API runs as one). The API holds only public
keys, so its environment cannot mint one. With no keys set nothing changes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

CALLER_EMAIL_HEADER = "x-caller-email"
CALLER_ASSERTION_HEADER = "x-caller-assertion"
PUBLIC_KEYS_ENV = "CALLER_ASSERTION_PUBLIC_KEYS"

# Audit rows name the caller by email, cut to this length.
MAX_ACTOR_LEN = 200

# Kept in step with packages/ui/src/lib/callerAssertion.ts (its tests read
# these lines).
ASSERTION_VERSION = "v1"
AUDIENCE = "openexecutive-api"
MAX_LIFETIME_S = 120
# How far ahead of this clock the signer's may run.
CLOCK_SKEW_S = 30

_MAX_ASSERTION_LEN = 4096
_MAX_EMAIL_LEN = 320
_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_JTI_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
# 32 bytes of base64url: 43 characters, and the one "=" some encoders add.
_RAW_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}=?")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")

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


# ---------------------------------------------------------------------------
# Signed callers
# ---------------------------------------------------------------------------


class CallerKeysError(ValueError):
    """``CALLER_ASSERTION_PUBLIC_KEYS`` is set but can't be used."""


class AssertionRefused(Exception):
    """An ``x-caller-assertion`` that doesn't hold; ``reason`` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def signing_on() -> bool:
    """Whether the API checks signed callers (``CALLER_ASSERTION_PUBLIC_KEYS``
    is set; ``api.main.create_app`` refuses to start when it can't be read)."""
    return bool(os.environ.get(PUBLIC_KEYS_ENV, "").strip())


def _b64url(text: str) -> bytes:
    if not _B64URL_RE.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_public_keys(raw: str) -> dict[str, Ed25519PublicKey]:
    """``kid:key[,kid:key…]``, each key 32 raw bytes in base64url, as
    ``scripts/make-caller-keys.py`` prints them. More than one while a key is
    being rotated. Empty is no keys; anything malformed raises, and so does a
    value that lists none (" , "): set means signed callers are on
    (``signing_on``), so it must hold a key the gate can check."""
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        kid, sep, encoded = (part.strip() for part in entry.partition(":"))
        if not sep or not _KID_RE.fullmatch(kid) or not _RAW_KEY_RE.fullmatch(encoded):
            raise CallerKeysError(
                "each key is <key id>:<public key>, as scripts/make-caller-keys.py prints it"
            )
        if kid in keys:
            raise CallerKeysError(f"key id {kid} is listed twice")
        try:
            keys[kid] = Ed25519PublicKey.from_public_bytes(_b64url(encoded.rstrip("=")))
        except ValueError as exc:
            raise CallerKeysError(f"key {kid} is not an Ed25519 public key") from exc
    if raw.strip() and not keys:
        raise CallerKeysError("it is set but lists no key")
    return keys


def public_keys_from_env() -> dict[str, Ed25519PublicKey]:
    return parse_public_keys(os.environ.get(PUBLIC_KEYS_ENV, ""))


class JtiLedger:
    """The assertion ids already used, each until it expires."""

    def __init__(self, limit: int = 100_000) -> None:
        self._seen: dict[str, int] = {}
        self._limit = limit
        self._prune_at = 1024

    def size(self) -> int:
        return len(self._seen)

    def claim(self, jti: str, exp: int, now: int) -> None:
        """Record ``jti``; raises when it was used before, or when the ledger
        is full of live ids (fail closed)."""
        if len(self._seen) >= min(self._prune_at, self._limit):
            # An expired id can't come back: its assertion fails the clock
            # check before it gets here.
            self._seen = {k: e for k, e in self._seen.items() if e >= now}
            self._prune_at = max(1024, 2 * len(self._seen))
        if jti in self._seen:
            raise AssertionRefused("replayed")
        if len(self._seen) >= self._limit:
            raise AssertionRefused("ledger_full")
        self._seen[jti] = exp


def _signs(key: Ed25519PublicKey, signature: bytes, data: bytes) -> bool:
    try:
        key.verify(signature, data)
    except InvalidSignature:
        return False
    return True


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionRefused("malformed")
    return value


def _plausible_email(value: str) -> bool:
    return (
        len(value) <= _MAX_EMAIL_LEN
        and value == value.strip().lower()
        and value.isprintable()
        and _EMAIL_RE.fullmatch(value) is not None
    )


def verify_assertion(
    token: str,
    *,
    keys: Mapping[str, Ed25519PublicKey],
    method: str,
    target: str,
    now: int,
    ledger: JtiLedger,
) -> Caller:
    """The caller ``token`` vouches for, for this one request; raises
    ``AssertionRefused`` when it doesn't hold. Its ``jti`` is spent only when
    everything else checks out."""
    if len(token) > _MAX_ASSERTION_LEN or not token.isascii():
        raise AssertionRefused("malformed")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ASSERTION_VERSION:
        raise AssertionRefused("malformed")
    try:
        payload = _b64url(parts[1])
        signature = _b64url(parts[2])
    except ValueError:
        raise AssertionRefused("malformed") from None
    # The signature is checked before anything is parsed, against each key
    # (one, or two while rotating), so no unsigned byte reaches the JSON
    # parser. The claims then have to name the key that signed them.
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signed_by = next((kid for kid, key in keys.items() if _signs(key, signature, signing_input)), None)
    if signed_by is None:
        raise AssertionRefused("signature")
    try:
        claims = json.loads(payload)
    except (ValueError, RecursionError):
        raise AssertionRefused("malformed") from None
    if not isinstance(claims, dict):
        raise AssertionRefused("malformed")
    if claims.get("kid") != signed_by:
        raise AssertionRefused("key_id")

    iat, exp = _seconds(claims.get("iat")), _seconds(claims.get("exp"))
    if claims.get("aud") != AUDIENCE:
        raise AssertionRefused("audience")
    if not 0 < exp - iat <= MAX_LIFETIME_S:
        raise AssertionRefused("lifetime")
    if iat > now + CLOCK_SKEW_S:
        raise AssertionRefused("not_yet_valid")
    if now > exp:
        raise AssertionRefused("expired")
    if claims.get("m") != method.upper():
        raise AssertionRefused("method")
    if claims.get("p") != target:
        raise AssertionRefused("path")
    jti, kind, sub = claims.get("jti"), claims.get("kind"), claims.get("sub")
    if not isinstance(jti, str) or not _JTI_RE.fullmatch(jti) or not isinstance(sub, str):
        raise AssertionRefused("malformed")
    who: Caller
    if kind == "user" and _plausible_email(sub):
        who = Caller("user", sub)
    elif kind == "operator" and sub == "":
        who = Caller("operator")
    else:
        raise AssertionRefused("subject")
    ledger.claim(jti, exp, now)
    return who


def request_target(request: Request) -> str:
    """This request's path and query exactly as it named them: what an
    assertion's ``p`` must equal."""
    scope = request.scope
    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) else str(scope.get("path", ""))
    query = scope.get("query_string") or b""
    return f"{path}?{query.decode('latin-1')}" if query else path


_Gate = Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]


def caller_gate(keys: Mapping[str, Ed25519PublicKey]) -> _Gate:
    """The middleware that decides ``request.state.caller`` on every request
    once signed callers are on (``api.main.create_app`` installs it)."""
    ledger = JtiLedger()

    def refuse(request: Request, reason: str) -> Response:
        # Never the assertion or the email: the reason and where it went.
        logger.warning(
            "caller refused reason=%s method=%s path=%r",
            reason, request.method, request_target(request)[:200],
        )
        code = "caller_assertion_required" if reason == "required" else "caller_assertion_invalid"
        return JSONResponse({"error": "unauthorized", "code": code}, status_code=401)

    async def gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method == "OPTIONS":
            # CORS preflight carries no credentials; it names no one.
            request.state.caller = Caller("service")
            return await call_next(request)
        emails = request.headers.getlist(CALLER_EMAIL_HEADER)
        tokens = request.headers.getlist(CALLER_ASSERTION_HEADER)
        if len(emails) > 1 or len(tokens) > 1:
            return refuse(request, "duplicate_header")
        email = emails[0].strip().lower() if emails else ""
        if not tokens:
            if email:
                return refuse(request, "required")
            request.state.caller = Caller("service")
            return await call_next(request)
        try:
            who = verify_assertion(
                tokens[0].strip(),
                keys=keys,
                method=request.method,
                target=request_target(request),
                now=int(time.time()),
                ledger=ledger,
            )
        except AssertionRefused as exc:
            return refuse(request, exc.reason)
        if email and email != who.email:
            return refuse(request, "email_mismatch")
        request.state.caller = who
        return await call_next(request)

    return gate
