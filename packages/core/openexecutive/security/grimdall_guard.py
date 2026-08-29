"""Grimdall execution guardrails for Open Executive.

Open Executive hands the Executive model a broad tool surface (skills, MCP
``call_tool`` against Gmail/Calendar/Drive, broadcast + schedule + workflow
tools). Because RAG context and inbound channel messages (email, Slack,
Telegram, Discord, Google Chat) are injected into user turns, a malicious
document or message can steer the model into tool calls that exfiltrate
secrets, destroy state, or burn unbounded LLM spend.

This module installs a deterministic, sub-millisecond pre-execution boundary
on the tool-use dispatch loop. Three static checks plus one ledger check:

1. **SECRET DENIAL** — blocks tool arguments that name credential files
   (``.env``, ``~/.ssh``, ``~/.aws``, ``company/``, ``/data``, Google
   service-account JSON, ``episodic_memory.db``, ``chroma_db``).
2. **EGRESS ALLOWLIST** — bare ``http(s)`` URL arguments (the shape an
   exfiltration command takes) must resolve to an allowlisted host, matched
   exactly or as a real subdomain. Fails closed: an unparsable or unlisted
   host is a violation. Pure public-fetch tools whose data path terminates at
   allowlisted providers (``web_search``, ``scrape_url``, ``load_mcp_server``)
   are exempt — URL prose inside message bodies is never scanned.
3. **DESTRUCTIVE BLOCK** — blocks shell-destructive command fragments in tool
   arguments (``rm -rf``, ``sudo``, ``chmod 777``, ``shutdown``,
   ``git reset --hard``, ``DROP TABLE`` / ``TRUNCATE TABLE``).
4. **PER-SESSION SPEND GUARDRAIL** — sums the session's ``cache_event`` audit
   rows (the same rows the /audit usage view reads) and blocks further tool
   dispatch once ``GRIMDALL_MAX_TOKENS_PER_SESSION`` or
   ``GRIMDALL_MAX_COST_USD_PER_SESSION`` is exceeded. Catches runaway opus
   extended-thinking loops that recur across many turns.

Deployment modes (``openexecutive/config.py``):

* **Off (default)** — ``GRIMDALL_ENABLED`` unset/false: the guard returns
  immediately; behavior is identical to a checkout without this module.
* **Shadow** — ``GRIMDALL_ENABLED=true``: violations are written to the
  existing ``audit_log`` as signed ``grimdall_block`` receipts and execution
  proceeds unchanged.
* **Enforce** — ``GRIMDALL_ENFORCE=true``: violating tool calls are replaced
  with an error ``tool_result`` the model can react to; the receipt is still
  written first.

Receipts are HMAC-SHA256 signed with a per-install key (``GRIMDALL_SIGNING_KEY``
env var, or an auto-generated ``company/.grimdall-key`` — ``company/`` is
gitignored) so a tampered audit trail is detectable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openexecutive.config import get_settings

logger = logging.getLogger(__name__)

# ── Rule 1: SECRET DENIAL ─────────────────────────────────────────────────
# Credential path fragments matched (case-insensitively, separators unified)
# against every string leaf of a tool call's arguments. Component patterns
# must be a whole path segment (``.env`` as a filename); fragment patterns are
# plain substrings of a normalized path. ``company/profile.yaml`` and
# ``company/docs/`` are blocked (profile + uploaded documents are confidential
# egress targets), but a bare ``company/`` path is NOT — attaching a board pack
# to a roster-gated email is a legitimate flow.
_SECRET_COMPONENTS = (".env", ".ssh", ".aws")
_SECRET_FRAGMENTS = (
    "company/profile.yaml",
    "company/docs/",
    "/data/",  # Fly.io persistent volume
    "episodic_memory.db",
    "chroma_db",
    "service_account",
    "service-account",
    "credentials.json",
    "client_secret.json",
    "token.json",
)

# ── Content-only tools ────────────────────────────────────────────────────
# Tools whose arguments are message *content* (no file paths, no URLs to
# dereference, no commands) — send_company_broadcast / send_department_message
# route a plain text body to the channel send handlers. Like URL prose, their
# bodies are data, not instructions, so the static scans skip them. This keeps
# a legitimate broadcast that happens to mention ``rm -rf`` or a path from
# false-positiving in Enforce Mode. MCP send tools are NOT exempt — their
# ``attachment_paths`` argument is exactly the T3 exfiltration vector.
_CONTENT_EXEMPT_TOOLS = frozenset({"send_company_broadcast", "send_department_message"})

# ── Rule 2: EGRESS ALLOWLIST ──────────────────────────────────────────────
# Base domains Open Executive's tool layer may target with a bare URL
# argument. Subdomains of an allowed base are permitted (``domain_matches``).
# Extend per deployment via GRIMDALL_EGRESS_ALLOWLIST (comma-separated).
_EGRESS_BASE_ALLOWLIST = frozenset(
    {
        "api.anthropic.com",
        "openrouter.ai",
        "run.xcrawl.com",
        "openstax.org",
        "slack.com",
        "api.telegram.org",
        "googleapis.com",
        "discord.com",
        "github.com",
        "raw.githubusercontent.com",
    }
)
# Tools whose purpose is fetching arbitrary public content. Their data path
# terminates at an allowlisted provider (Anthropic for server-side web_search,
# xcrawl for scrape_url) and they carry their own URL gating (load_mcp_server
# is https-only plus the filters.load_control allowlist), so scanning their
# URL arguments would break legitimate research without adding egress safety.
_EGRESS_EXEMPT_TOOLS = frozenset({"web_search", "scrape_url", "load_mcp_server"})
_URL_PREFIX = re.compile(r"^https?://", re.IGNORECASE)

# ── Rule 3: DESTRUCTIVE BLOCK ─────────────────────────────────────────────
# Shell-destructive fragments in tool arguments. Unambiguous and rare in
# legitimate calls, so they fail closed without a tool-name denylist (the repo
# has legit delete/remove tools — delete_skill, remove_watchlist_entry — so
# name-based blocking would break real flows).
_DESTRUCTIVE_PATTERNS: tuple[tuple[str, bool], ...] = (
    ("rm -rf", False),
    ("rm -fr", False),
    (r"\bsudo\b", True),
    ("chmod 777", False),
    ("shutdown", False),
    ("git reset --hard", False),
    ("drop table", False),
    ("truncate table", False),
)

# ── Rule 4: per-session spend ledger ──────────────────────────────────────
# Mirrors the SUM expressions in audit/logger.py (_USAGE_SUM_COLS) but scoped
# to one session so the guard reads the same numbers the /audit usage view
# shows. Tokens are integer counts; cost is fractional USD (populated on the
# OpenRouter path only — Anthropic-direct rows carry None cost and count 0).
_USAGE_INT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_SPEND_SUM_SQL = (
    "SELECT "
    + ", ".join(
        f"COALESCE(SUM(CAST(json_extract(details_json,'$.{f}') AS INTEGER)),0) AS {f}"
        for f in _USAGE_INT_FIELDS
    )
    + ", COALESCE(SUM(CAST(json_extract(details_json,'$.cost_usd') AS REAL)),0) AS cost_usd "
    "FROM audit_log WHERE event_type = 'cache_event' AND session_id = ?"
)

_EPISODIC_DB_DEFAULT = "./episodic_memory.db"


@dataclass(frozen=True)
class Violation:
    """A single guard rule a tool call violated."""

    rule: str
    reason: str


@dataclass
class Decision:
    """Outcome of evaluating one tool call against the guardrails.

    ``allowed`` is False only in Enforce Mode — Shadow Mode logs the receipt
    and lets the call proceed so existing workflows are never broken.
    """

    allowed: bool
    violations: tuple[Violation, ...]
    mode: str
    receipt: dict[str, Any] | None = None

    def error_result(self) -> str:
        """JSON error payload returned to the model in place of a tool result."""
        if not self.violations:
            return json.dumps({"error": "grimdall_block: no reason"}, ensure_ascii=False)
        first = self.violations[0]
        return json.dumps(
            {"error": f"grimdall_block: {first.rule}: {first.reason}"},
            ensure_ascii=False,
        )


class GrimdallBlockError(RuntimeError):
    """Raised when a guard check fails in a context with no tool_result channel.

    The dispatch hooks never raise this (they return an error tool_result);
    it exists for direct library callers that want a typed exception.
    """


# ── Mode helpers ──────────────────────────────────────────────────────────
def is_enabled() -> bool:
    """Return whether Grimdall is on (shadow or enforce). Default off."""
    return get_settings().grimdall_enabled


def get_mode() -> str:
    """Return ``"off"``, ``"shadow"``, or ``"enforce"``."""
    settings = get_settings()
    if not settings.grimdall_enabled:
        return "off"
    return "enforce" if settings.grimdall_enforce else "shadow"


# ── Argument iteration ────────────────────────────────────────────────────
def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string leaf of a nested dict/list argument structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_strings(item)
    elif value is not None:
        yield str(value)


def _normalized_path(text: str) -> str:
    """Lowercase and unify separators for deterministic path matching."""
    return text.replace("\\", "/").lower()


def _path_components(normalized: str) -> list[str]:
    return [part for part in normalized.split("/") if part]


def _args_sha256(args: Any) -> str:
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Rule 1: SECRET DENIAL ─────────────────────────────────────────────────
def check_secret_denial(tool: str, args: Any) -> list[Violation]:
    """Return violations when arguments name credential files."""
    if tool in _CONTENT_EXEMPT_TOOLS:
        return []
    violations: list[Violation] = []
    for text in _iter_strings(args):
        normalized = _normalized_path(text)
        components = _path_components(normalized)
        if any(
            component == secret or component.startswith(f"{secret}.")
            for component in components
            for secret in _SECRET_COMPONENTS
        ):
            violations.append(
                Violation("secret", f"argument references a credential path: {text[:120]}")
            )
            continue
        for fragment in _SECRET_FRAGMENTS:
            if fragment in normalized:
                violations.append(
                    Violation("secret", f"argument references a credential path: {text[:120]}")
                )
                break
    return violations


# ── Rule 2: EGRESS ALLOWLIST ──────────────────────────────────────────────
def _domain_matches(host: str, *domains: str) -> bool:
    """Match a hostname exactly or as a real subdomain of an allowed base."""
    normalized = host.lower().lstrip(".").rstrip(".")
    if not normalized:
        return False
    for domain in domains:
        allowed = domain.lower().lstrip(".").rstrip(".")
        if normalized == allowed or normalized.endswith("." + allowed):
            return True
    return False


def _bare_urls(args: Any) -> list[str]:
    """Return string leaves that are *bare* http(s) URLs.

    Only values that are exactly a URL (no surrounding prose) count — that is
    the shape an exfiltration command takes, while URL mentions inside message
    bodies or document text are legitimate content.
    """
    urls: list[str] = []
    for text in _iter_strings(args):
        stripped = text.strip()
        if _URL_PREFIX.match(stripped) and not any(ch.isspace() for ch in stripped):
            urls.append(stripped)
    return urls


def _url_host(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return host or None


def check_egress_allowlist(tool: str, args: Any) -> list[Violation]:
    """Return violations when a bare URL argument targets an unapproved host."""
    if tool in _EGRESS_EXEMPT_TOOLS or tool in _CONTENT_EXEMPT_TOOLS:
        return []
    settings = get_settings()
    allowed = _EGRESS_BASE_ALLOWLIST | frozenset(settings.grimdall_egress_allowlist)
    violations: list[Violation] = []
    for url in _bare_urls(args):
        host = _url_host(url)
        if host is None:
            violations.append(Violation("egress", f"could not parse egress URL: {url[:120]}"))
            continue
        if not _domain_matches(host, *allowed):
            violations.append(
                Violation(
                    "egress",
                    f"egress to {host!r} is not on the Grimdall allowlist ({url[:120]})",
                )
            )
    return violations


# ── Rule 3: DESTRUCTIVE BLOCK ─────────────────────────────────────────────
def check_destructive(tool: str, args: Any) -> list[Violation]:
    """Return violations when arguments carry shell-destructive commands."""
    if tool in _CONTENT_EXEMPT_TOOLS:
        return []
    violations: list[Violation] = []
    for text in _iter_strings(args):
        # Collapse all whitespace runs so ``rm  -rf`` / tab tricks still match.
        normalized = " ".join(text.casefold().split())
        for pattern, is_regex in _DESTRUCTIVE_PATTERNS:
            if is_regex:
                if re.search(pattern, normalized):
                    violations.append(
                        Violation("destructive", f"destructive command in argument: {text[:120]}")
                    )
                    break
            elif pattern in normalized:
                violations.append(
                    Violation("destructive", f"destructive command in argument: {text[:120]}")
                )
                break
    return violations


# ── Rule 4: per-session spend guardrail ───────────────────────────────────
def _session_usage(session_id: str | None) -> dict[str, float | int]:
    """Sum token/cost usage for one session from cache_event audit rows."""
    empty = {**{k: 0 for k in _USAGE_INT_FIELDS}, "cost_usd": 0.0}
    if not session_id:
        return empty
    db_path = Path(os.environ.get("EPISODIC_DB_PATH") or _EPISODIC_DB_DEFAULT)
    if not db_path.exists():
        return empty
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(_SPEND_SUM_SQL, (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("grimdall: spend ledger read failed session=%s", session_id, exc_info=True)
        return empty
    if row is None:
        return empty
    return {
        **{k: int(row[idx] or 0) for idx, k in enumerate(_USAGE_INT_FIELDS)},
        "cost_usd": float(row[len(_USAGE_INT_FIELDS)] or 0.0),
    }


def check_spend(session_id: str | None) -> list[Violation]:
    """Return violations when the session has exceeded its spend budgets.

    Both budgets default to 0 = disabled. Tokens are the sum of input, output,
    and cache tokens across the session's cache_event rows; cost applies only
    where the provider surfaced a real USD figure (OpenRouter path).
    """
    settings = get_settings()
    max_tokens = settings.grimdall_max_tokens_per_session
    max_cost = settings.grimdall_max_cost_usd_per_session
    if max_tokens <= 0 and max_cost <= 0:
        return []
    usage = _session_usage(session_id)
    violations: list[Violation] = []
    if max_tokens > 0:
        tokens = sum(int(usage[k]) for k in _USAGE_INT_FIELDS)
        if tokens > max_tokens:
            violations.append(
                Violation(
                    "spend",
                    f"session token budget exceeded: {tokens} > {max_tokens}",
                )
            )
    if max_cost > 0:
        cost = float(usage["cost_usd"])
        if cost > max_cost:
            violations.append(
                Violation("spend", f"session cost budget exceeded: ${cost:.4f} > ${max_cost:.4f}")
            )
    return violations


# ── Signed receipts ───────────────────────────────────────────────────────
def _signing_key() -> bytes:
    """Return (creating on first use) the per-install HMAC signing key.

    Precedence: ``GRIMDALL_SIGNING_KEY`` env var, then ``company/.grimdall-key``
    (owner-only, gitignored via the existing ``company/`` ignore rule).
    """
    settings = get_settings()
    if settings.grimdall_signing_key:
        return settings.grimdall_signing_key.strip().encode("utf-8")
    key_path = Path.cwd() / "company" / ".grimdall-key"
    if key_path.exists():
        existing = key_path.read_bytes().strip()
        if existing:
            return existing
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode("ascii")
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key + b"\n")
    finally:
        os.close(fd)
    return key


def _build_receipt(tool: str, args_sha256: str, rules: Sequence[str], mode: str) -> dict[str, Any]:
    """One signed receipt record, stored in the audit row's details."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload = json.dumps(
        {
            "ts": ts,
            "tool": tool,
            "args_sha256": args_sha256,
            "rules": sorted(rules),
            "mode": mode,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return json.loads(payload) | {"sig": signature}


def _log_receipt(
    receipt: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
    actor: str,
) -> None:
    """Fire-and-forget audit write; audit failures never break a turn."""
    try:
        from openexecutive.audit import log_event

        log_event(
            "grimdall_block",
            f"grimdall {receipt['mode']}: {receipt['tool']} blocked ({', '.join(receipt['rules'])})",
            session_id=session_id,
            turn_id=turn_id,
            actor=actor,
            details=receipt,
        )
    except Exception:
        logger.warning("grimdall: receipt write failed", exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────
def guard_tool_call(
    tool_name: str,
    args: Any,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    actor: str = "executive",
) -> Decision:
    """Evaluate one tool call against all four guardrails.

    ``call_tool`` envelopes are unwrapped so the MCP tool (e.g. Gmail send)
    and its real arguments are what get checked — the executive dispatch hook
    and ``mcp_gateway.call_tool`` both pass the envelope through here.
    """
    settings = get_settings()
    if not settings.grimdall_enabled:
        return Decision(allowed=True, violations=(), mode="off")

    mode = "enforce" if settings.grimdall_enforce else "shadow"

    # Unwrap the MCP gateway envelope: {"name": <inner tool>, "arguments": …}
    effective_tool, effective_args = tool_name, args
    if tool_name == "call_tool" and isinstance(args, dict):
        inner_name = args.get("name")
        inner_args = args.get("arguments", {})
        if isinstance(inner_name, str) and inner_name:
            effective_tool = inner_name
        if isinstance(inner_args, str):
            try:
                inner_args = json.loads(inner_args)
            except json.JSONDecodeError:
                inner_args = {}
        effective_args = inner_args

    violations: list[Violation] = []
    violations.extend(check_secret_denial(effective_tool, effective_args))
    violations.extend(check_egress_allowlist(effective_tool, effective_args))
    violations.extend(check_destructive(effective_tool, effective_args))
    violations.extend(check_spend(session_id))

    if not violations:
        return Decision(allowed=True, violations=(), mode=mode)

    rules = sorted({v.rule for v in violations})
    receipt = _build_receipt(effective_tool, _args_sha256(effective_args), rules, mode)
    _log_receipt(receipt, session_id=session_id, turn_id=turn_id, actor=actor)

    if mode == "enforce":
        return Decision(allowed=False, violations=tuple(violations), mode=mode, receipt=receipt)
    # Shadow mode: recorded, execution proceeds.
    return Decision(allowed=True, violations=tuple(violations), mode=mode, receipt=receipt)
