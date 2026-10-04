"""Take the lead: the Executive acts on its own, behind one gate.

Two switches, each off until someone turns it on:

- **As the Executive** (scope ``executive``, the principal's alone). The
  unattended passes (reflection, research, the scheduler's proactive trigger)
  keep the acting tools they otherwise lose in solo mode (booking, starting a
  workflow, messaging teammates and not just the principal), and every acting
  call they make goes through ``gate``.
- **As you** (scope ``person:<id>``). Handle it for me stops applying its
  Careful / Balanced / Bold limits to that person's replies and follow-ups;
  the gate's added rules apply instead (``reply_hit``), next to everything
  Handle it always keeps (signed sign-ins, exactly the thread's people,
  sensitive topics, one a thread, the daily limit).

**The gate** is plain code. Six kinds of action wait for someone first, each
with its own Ask first switch (all on to start): money, contracts, people
decisions, deleting or sharing, someone new, and big sends. On top of them
the company (set by the principal) and each person (for their own As you)
add rules: a person or address, a domain, words, or an amount. Added rules
always hold. A held action becomes a ``take_the_lead_action`` decision for
the person whose authority covers it (``people.store.find_approvers``, the
same scopes the department approval levels use), else the principal, with a
companion card on Today; approving it carries the exact call out
(``carry_out``) and declining drops it.

Pausing the Executive stops all of it: every pass above runs behind the
scheduler's pause gate. A person approving a held action is their own act,
so it works while paused.

Everything it does on its own is recorded in ``take_the_lead_log``; Recent
activity on Today names what it did (``api.routes.today._build_activity``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openexecutive.people.models import AuthorityScope

logger = logging.getLogger(__name__)

DECISION_CLASS = "take_the_lead_action"

SCOPE_EXECUTIVE = "executive"
SCOPE_COMPANY = "company"
_PERSON_PREFIX = "person:"

LEAD_TABLE = "take_the_lead"
RULES_TABLE = "take_the_lead_rules"
LOG_TABLE = "take_the_lead_log"
# Per company, like the roster the switches are keyed on (clients.slots,
# cli.fixture_loader.reset_all_state).
TABLES: tuple[str, ...] = (LEAD_TABLE, RULES_TABLE, LOG_TABLE)

MONEY = "money"
CONTRACTS = "contracts"
PEOPLE_DECISIONS = "people_decisions"
DELETE_SHARE = "delete_share"
SOMEONE_NEW = "someone_new"
BIG_SEND = "big_send"
KINDS: tuple[str, ...] = (MONEY, CONTRACTS, PEOPLE_DECISIONS, DELETE_SHARE, SOMEONE_NEW, BIG_SEND)
# More people than this on one action is a big send.
BIG_SEND_RECIPIENTS = 5

KIND_LABELS: dict[str, str] = {
    MONEY: "Money",
    CONTRACTS: "Contracts and legal",
    PEOPLE_DECISIONS: "Hiring and people decisions",
    DELETE_SHARE: "Deleting, cancelling or sharing",
    SOMEONE_NEW: "Someone new",
    BIG_SEND: "Big sends",
}
# One line under each switch saying what it catches, in the words check() uses.
KIND_HINTS: dict[str, str] = {
    MONEY: "Anything about payments, invoices, prices, budgets or an amount of money.",
    CONTRACTS: "Contracts, agreements, signatures, terms and other legal matters.",
    PEOPLE_DECISIONS: "Hiring, firing, pay, promotions and performance reviews.",
    DELETE_SHARE: "Deleting or cancelling something, or sharing a file or access.",
    SOMEONE_NEW: "Messages to anyone who isn't on your team or in your contacts.",
    BIG_SEND: f"Company-wide or department-wide messages, or anything going to more than {BIG_SEND_RECIPIENTS} people.",
}

RULE_PERSON = "person"
RULE_DOMAIN = "domain"
RULE_WORDS = "words"
RULE_AMOUNT = "amount"
RULE_KINDS: tuple[str, ...] = (RULE_PERSON, RULE_DOMAIN, RULE_WORDS, RULE_AMOUNT)
RULE_VALUE_MAX = 200
RULES_MAX = 50

# The acting skill tools. Everything else an unattended pass may call reads,
# or records something only for the Executive itself.
ACTING_TOOLS: frozenset[str] = frozenset({
    "archive_person",
    "assign_open_loop",
    "cancel_calendar_event",
    "create_calendar_event",
    "create_instant_meeting",
    "delete_skill",
    "message_person",
    "run_workflow",
    "send_company_broadcast",
    "send_department_message",
    "send_discord_dm",
    "send_slack_dm",
    "send_telegram_message",
})
_DELETE_TOOLS = frozenset({"archive_person", "cancel_calendar_event", "delete_skill"})
_BROADCAST_TOOLS = frozenset({"send_company_broadcast", "send_department_message"})
# A connected (MCP) tool passes ungated only when its name says it reads and
# nothing in it says it acts. Any other name goes through the gate.
_MCP_ACTING_RE = re.compile(
    r"(send|reply|forward|create|update|delete|remove|trash|cancel|share|permission|move|post|invite|draft|"
    r"modify|append|write|insert|batch|clear|rename|copy|add|upload|schedule|set|edit|import|publish|archive|"
    r"assign|replace|merge|transfer|approve|submit|book|respond|accept|decline)",
    re.IGNORECASE,
)
_MCP_READ_RE = re.compile(
    r"(?:^|[^a-z])(get|list|search|read|fetch|query|find|view|describe|download)(?:[^a-z]|$)",
    re.IGNORECASE,
)
_MCP_DELETE_RE = re.compile(r"(delete|remove|trash|cancel|share|permission|clear|archive)", re.IGNORECASE)

_MONEY_RE = re.compile(
    r"\b(invoices?|payments?|pay|paid|refunds?|pricing|price|quotes?|discounts?|budgets?|wire|bank|"
    r"purchase|spend|fees?|costs?)\b|[$€£¥]\s?\d|\b\d[\d,.]*\s?(usd|eur|gbp|dollars|euros|pounds)\b",
    re.IGNORECASE,
)
_CONTRACTS_RE = re.compile(
    r"\b(contracts?|agreements?|sign|signed|signature|nda|terms|legal|lawyers?|liabilit\w*|lawsuit)\b",
    re.IGNORECASE,
)
_PEOPLE_RE = re.compile(
    r"\b(hire|hiring|hired|offer letter|fire|fired|firing|terminat\w*|salary|salaries|compensation|"
    r"promot\w*|layoffs?|laid off|resign\w*|performance review|disciplinary)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AMOUNT_VALUE_RE = re.compile(
    r"(?:[$€£¥]\s?)(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?\s?(?:usd|eur|gbp|dollars|euros|pounds)\b",
    re.IGNORECASE,
)
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def person_scope(person_id: int) -> str:
    return f"{_PERSON_PREFIX}{person_id}"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _db(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from openexecutive.memory import episodic

    return Path(episodic.DB_PATH)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LEAD_TABLE} ("  # noqa: S608 — constant table name
        "scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RULES_TABLE} ("  # noqa: S608
        "id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, kind TEXT NOT NULL, "
        "value TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {LOG_TABLE} ("  # noqa: S608
        "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, scope TEXT NOT NULL, "
        "source TEXT NOT NULL, tool TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL, "
        "why TEXT NOT NULL DEFAULT '', decision_id INTEGER)"
    )


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db(db_path)))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


@dataclass(frozen=True)
class Lead:
    scope: str
    enabled: bool = False
    ask_first: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(KINDS, True))

    def asks_first(self, kind: str) -> bool:
        return self.ask_first.get(kind, True)


def kind_class(kind: str) -> str:
    """The decision ledger class whose mode is that kind's Ask first:
    ``propose`` (the ledger's default) asks first, ``auto_execute`` doesn't."""
    return f"{DECISION_CLASS}:{kind}"


def _ask_first(db_path: Path | None) -> dict[str, bool]:
    """Each kind's Ask first, from the decision ledger's class modes.
    Unreadable asks first."""
    from openexecutive.memory.decision_ledger import get_class_mode

    out = dict.fromkeys(KINDS, True)
    for kind in KINDS:
        try:
            out[kind] = get_class_mode(kind_class(kind), db_path=db_path) != "auto_execute"
        except Exception:
            out[kind] = True
    return out


def get(scope: str, *, db_path: Path | None = None) -> Lead:
    """The switch for ``scope``. Never raises: unreadable is off."""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                f"SELECT enabled FROM {LEAD_TABLE} WHERE scope = ?", (scope,),  # noqa: S608
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("take_the_lead: couldn't read the switch — treating it as off", exc_info=True)
        return Lead(scope=scope, ask_first=_ask_first(db_path))
    return Lead(scope=scope, enabled=bool(row and row["enabled"]), ask_first=_ask_first(db_path))


def set_(
    scope: str,
    *,
    enabled: bool | None = None,
    ask_first: dict[str, bool] | None = None,
    updated_by: str,
    db_path: Path | None = None,
) -> Lead:
    """Change ``scope``'s switch and/or its Ask first switches (callers
    authorize first). Unknown kinds are ignored."""
    from openexecutive.memory.decision_ledger import set_class_mode

    current = get(scope, db_path=db_path)
    new_enabled = current.enabled if enabled is None else enabled
    for kind, value in (ask_first or {}).items():
        if kind in KINDS and isinstance(value, bool) and value != current.asks_first(kind):
            set_class_mode(kind_class(kind), "propose" if value else "auto_execute", db_path=db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {LEAD_TABLE} (scope, enabled, updated_at, updated_by) "  # noqa: S608
            "VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (scope, 1 if new_enabled else 0, datetime.now(UTC).isoformat(), updated_by),
        )
        conn.commit()
    finally:
        conn.close()
    return get(scope, db_path=db_path)


def executive_on(*, db_path: Path | None = None) -> bool:
    return get(SCOPE_EXECUTIVE, db_path=db_path).enabled


def as_you_on(person_id: int, *, db_path: Path | None = None) -> bool:
    return get(person_scope(person_id), db_path=db_path).enabled


@dataclass(frozen=True)
class Rule:
    id: int
    scope: str
    kind: str
    value: str
    created_at: str


class RuleError(ValueError):
    """A rule that can't be added, with a sentence saying why."""


def clean_rule(kind: str, value: str) -> str:
    """``value`` normalised for ``kind``, or RuleError."""
    if kind not in RULE_KINDS:
        raise RuleError("Pick a person, a domain, words or an amount.")
    text = " ".join(str(value or "").split())
    if not text:
        raise RuleError("The rule is empty.")
    if len(text) > RULE_VALUE_MAX:
        raise RuleError(f"Keep it under {RULE_VALUE_MAX} characters.")
    if kind == RULE_DOMAIN:
        text = text.lower().lstrip("@")
        if not _DOMAIN_RE.match(text):
            raise RuleError("That isn't a domain, like example.com.")
    elif kind == RULE_AMOUNT:
        number = _number(text.lstrip("$€£¥"))
        if number is None or number <= 0:
            raise RuleError("That isn't an amount, like 500.")
        # Plain digits: "{:g}" turns 1000000 into "1e+06", which _number can't read back.
        text = format(number, "f").rstrip("0").rstrip(".")
    elif kind == RULE_PERSON:
        text = text.lower() if "@" in text else text
    return text


def list_rules(scopes: list[str], *, db_path: Path | None = None) -> list[Rule]:
    if not scopes:
        return []
    try:
        conn = _connect(db_path)
        try:
            marks = ",".join("?" * len(scopes))
            rows = conn.execute(
                f"SELECT id, scope, kind, value, created_at FROM {RULES_TABLE} "  # noqa: S608
                f"WHERE scope IN ({marks}) ORDER BY id",
                tuple(scopes),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("take_the_lead: couldn't read the rules", exc_info=True)
        raise
    return [Rule(id=r["id"], scope=r["scope"], kind=r["kind"], value=r["value"], created_at=r["created_at"])
            for r in rows]


def add_rule(scope: str, kind: str, value: str, *, created_by: str, db_path: Path | None = None) -> Rule:
    text = clean_rule(kind, value)
    conn = _connect(db_path)
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {RULES_TABLE} WHERE scope = ?", (scope,),  # noqa: S608
        ).fetchone()[0]
        if count >= RULES_MAX:
            raise RuleError(f"There are already {RULES_MAX} rules here.")
        now = datetime.now(UTC).isoformat()
        cur = conn.execute(
            f"INSERT INTO {RULES_TABLE} (scope, kind, value, created_by, created_at) VALUES (?, ?, ?, ?, ?)",  # noqa: S608
            (scope, kind, text, created_by, now),
        )
        conn.commit()
        rule_id = int(cur.lastrowid or 0)
    finally:
        conn.close()
    return Rule(id=rule_id, scope=scope, kind=kind, value=text, created_at=now)


def delete_rule(rule_id: int, scope: str, *, db_path: Path | None = None) -> bool:
    """Remove rule ``rule_id`` if it belongs to ``scope``."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(f"DELETE FROM {RULES_TABLE} WHERE id = ? AND scope = ?", (rule_id, scope))  # noqa: S608
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Hit:
    """Why an action waits: a base kind or an added rule."""

    kind: str
    reason: str
    rule_id: int | None = None


def _number(text: str) -> float | None:
    match = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\s*", text or "")
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    return value * (1000 if suffix == "k" else 1_000_000 if suffix == "m" else 1)


def amounts(text: str) -> list[float]:
    """Money amounts written in ``text`` ($500, 2k EUR, …)."""
    found: list[float] = []
    for m in _AMOUNT_VALUE_RE.finditer(text or ""):
        number = _number(f"{m.group(1) or m.group(3)}{m.group(2) or m.group(4) or ''}")
        if number is not None:
            found.append(number)
    return found


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _known(address: str) -> bool:
    """On the roster or a contact. Fails closed: unreadable is new."""
    from openexecutive.people.store import find_person_by_address

    try:
        return find_person_by_address(address, include_contacts=True) is not None
    except Exception:
        return False


def _rule_hit(rules: list[Rule], text: str, recipients: list[str]) -> Hit | None:
    lowered = text.lower()
    for rule in rules:
        value = rule.value
        if rule.kind == RULE_PERSON:
            if "@" in value and value in recipients:
                return Hit("rule", f"your rule about {value}", rule.id)
            if "@" not in value and value.lower() in lowered:
                return Hit("rule", f"your rule about {value}", rule.id)
        elif rule.kind == RULE_DOMAIN:
            if any(r.endswith("@" + value) or r.endswith("." + value) for r in recipients):
                return Hit("rule", f"your rule about {value}", rule.id)
        elif rule.kind == RULE_WORDS:
            if value.lower() in lowered:
                return Hit("rule", f'your rule about "{value}"', rule.id)
        elif rule.kind == RULE_AMOUNT:
            limit = _number(value) or 0
            if any(a >= limit for a in amounts(text)):
                return Hit("rule", f"your rule about amounts of {value} or more", rule.id)
    return None


def check(
    tool: str,
    tool_input: dict[str, Any],
    *,
    lead: Lead,
    rules: list[Rule],
    extra_recipients: list[str] | None = None,
    mcp: bool = False,
) -> Hit | None:
    """Why this action must wait for someone, or None when it may go.
    Plain code; an added rule always holds, a base kind only while its Ask
    first switch is on."""
    text = "\n".join(_strings(tool_input))
    recipients = sorted({a.lower() for a in [*_EMAIL_RE.findall(text), *(extra_recipients or [])]})
    hit = _rule_hit(rules, text, recipients)
    if hit is not None:
        return hit
    checks: list[tuple[str, bool, str]] = [
        (DELETE_SHARE, tool in _DELETE_TOOLS or (mcp and bool(_MCP_DELETE_RE.search(tool))),
         "it deletes, cancels or shares something"),
        (BIG_SEND, tool in _BROADCAST_TOOLS or len(recipients) > BIG_SEND_RECIPIENTS,
         "it goes to a lot of people"),
        (MONEY, bool(_MONEY_RE.search(text)), "it's about money"),
        (CONTRACTS, bool(_CONTRACTS_RE.search(text)), "it's about a contract or legal matter"),
        (PEOPLE_DECISIONS, bool(_PEOPLE_RE.search(text)), "it's about hiring or a people decision"),
        (SOMEONE_NEW, any(not _known(r) for r in recipients), "it goes to someone new"),
    ]
    for kind, matched, reason in checks:
        if matched and lead.asks_first(kind):
            return Hit(kind, reason)
    return None


def reply_hit(person_id: int, texts: list[str], recipients: list[str], *, db_path: Path | None = None) -> Hit | None:
    """For Take the lead as you: the company's and the person's own added
    rules against a reply or follow-up. Fails closed."""
    try:
        rules = list_rules([SCOPE_COMPANY, person_scope(person_id)], db_path=db_path)
    except Exception:
        return Hit("rule", "its rules couldn't be read")
    return _rule_hit(rules, "\n".join(texts), sorted({r.lower() for r in recipients}))


# --------------------------------------------------------------------------- #
# Acting under the gate
# --------------------------------------------------------------------------- #

_TOOL_LABELS: dict[str, str] = {
    "archive_person": "Remove someone from the team",
    "assign_open_loop": "Assign something to someone",
    "cancel_calendar_event": "Cancel a meeting",
    "create_calendar_event": "Book a meeting",
    "create_instant_meeting": "Start a call",
    "delete_skill": "Delete a skill",
    "message_person": "Message someone",
    "run_workflow": "Start a workflow",
    "send_company_broadcast": "Message the whole company",
    "send_department_message": "Message a department",
    "send_discord_dm": "Send a Discord message",
    "send_slack_dm": "Send a Slack message",
    "send_telegram_message": "Send a Telegram message",
}


# A connected tool, named in plain words: what it does, to which file, in
# which app. Matched against the tool's name, first match wins.
_MCP_VERBS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), v) for p, v in (
        (r"share|permission", "Share"),
        (r"delete|trash|remove", "Delete"),
        (r"create|new|copy", "Create"),
        (r"update|modify|append|write|edit|batch|insert|set_|format|clear", "Update"),
        (r"move|rename", "Move"),
    )
)
_MCP_APPS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), app, noun) for p, app, noun in (
        (r"excel|workbook|worksheet", "Excel", "a workbook"),
        (r"ms365.*(word|document)|word_document", "Word", "a document"),
        (r"onedrive|ms365.*(drive|file)", "OneDrive", "a file"),
        (r"sheet", "Google Sheets", "a spreadsheet"),
        (r"slide|presentation", "Google Slides", "a presentation"),
        (r"doc", "Google Docs", "a document"),
        (r"drive|file|folder", "Google Drive", "a file"),
    )
)
_MCP_NAME_KEYS = ("title", "name", "file_name", "filename", "document_title", "spreadsheet_title", "sheet_title")
_MCP_RECIPIENT_KEYS = ("email_address", "email", "emails", "share_with", "recipient", "recipients", "user_email")


def _first_text(tool_input: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if isinstance(v, str))
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= 80 else text[:77] + "…"
    return ""


def _connected_label(tool: str, tool_input: dict[str, Any], *, recipient: bool) -> str | None:
    """"Update Supplier deliveries (Google Sheets)", "Share Plant review prep
    with plant-team@…": the file or record by name, never its content. The
    person it's shared with only where ``recipient`` (the approval card,
    which only the principal and the approver see). None when the tool's
    name doesn't say what it does."""
    verb = next((v for rx, v in _MCP_VERBS if rx.search(tool)), None)
    if verb is None:
        return None
    app, noun = next(((a, n) for rx, a, n in _MCP_APPS if rx.search(tool)), (None, None))
    name = _first_text(tool_input, _MCP_NAME_KEYS)
    if not name and not app:
        return None
    what = name or noun or ""
    if verb == "Share":
        label = f"Share {what}"
        who = _first_text(tool_input, _MCP_RECIPIENT_KEYS) if recipient else ""
        return f"{label} with {who}" if who else label
    return f"{verb} {what} ({app})" if app else f"{verb} {what}"


def summarize(tool: str, tool_input: dict[str, Any], *, mcp: bool = False, quote: bool = True) -> str:
    """One plain line saying what the action does; ``quote`` adds the start
    of what it says and, for a share, who it's shared with (the approval
    card has them, the activity feed doesn't)."""
    label = _TOOL_LABELS.get(tool) or tool.replace("_", " ").strip().capitalize()
    if mcp:
        bare = tool.split("__", 1)[-1]
        label = _connected_label(tool, tool_input, recipient=quote) or bare.replace("_", " ").strip().capitalize()
    if tool == "message_person":
        try:
            from openexecutive.people.store import get_person

            person = get_person(int(tool_input.get("person_id")))  # type: ignore[arg-type]
            if person is not None:
                label = f"Message {person.full_name or person.email}"
        except Exception:
            pass
    elif tool == "create_calendar_event" and tool_input.get("title"):
        label = f"Book {tool_input.get('title')}"
    elif tool == "run_workflow" and tool_input.get("workflow_id"):
        label = f"Start the {tool_input.get('workflow_id')} workflow"
    snippet = ""
    for key in ("text", "message", "body", "description", "subject"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            snippet = " ".join(value.split())
            break
    if snippet and quote:
        snippet = snippet if len(snippet) <= 120 else snippet[:117] + "…"
        return f"{label}: “{snippet}”"[:240]
    return label[:240]


def record(
    *, scope: str, source: str, tool: str, summary: str, status: str, why: str = "",
    decision_id: int | None = None, db_path: Path | None = None,
) -> None:
    """Add a log row (Recent activity reads the done ones). Best effort."""
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                f"INSERT INTO {LOG_TABLE} (at, scope, source, tool, summary, status, why, decision_id) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), scope, source, tool, summary, status, why, decision_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("take_the_lead: couldn't record what it did", exc_info=True)


def _set_log_status(decision_id: int, status: str, *, db_path: Path | None = None) -> None:
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                f"UPDATE {LOG_TABLE} SET status = ? WHERE decision_id = ?", (status, decision_id),  # noqa: S608
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning("take_the_lead: couldn't update what it did", exc_info=True)


@dataclass(frozen=True)
class Done:
    id: int
    at: str
    scope: str
    source: str
    tool: str
    summary: str
    status: str
    why: str
    decision_id: int | None


def done(scopes: list[str], *, days: int = 7, limit: int = 100, db_path: Path | None = None) -> list[Done]:
    """What it did on its own for ``scopes`` in the last ``days``, newest
    first: the Take the lead rows of Today's Recent activity
    (``api.routes.today._build_activity``)."""
    if not scopes:
        return []
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = _connect(db_path)
    try:
        marks = ",".join("?" * len(scopes))
        rows = conn.execute(
            f"SELECT * FROM {LOG_TABLE} WHERE scope IN ({marks}) AND at >= ? "  # noqa: S608
            "ORDER BY at DESC, id DESC LIMIT ?",
            (*scopes, since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [Done(**dict(r)) for r in rows]


# Area → the authority scope whose holders may approve a held action of
# that kind (the department approval levels' People scopes); anything else
# is the principal's. The authority gate picks the person
# (``departments.authority._route_proposal``), as for a department proposal.
_CREDIT_RE = re.compile(
    r"\b(credit|loans?|debts?|financing|line of credit|payment terms|net \d+|overdue balance|write[- ]off)\b",
    re.IGNORECASE,
)
_VENDOR_RE = re.compile(
    r"\b(vendors?|suppliers?|procurement|purchase orders?|msa|sow|statement of work|reseller|onboard\w* (a|the|new) "
    r"(vendor|supplier))\b",
    re.IGNORECASE,
)
_BOARD_RE = re.compile(r"\b(board|board members?|directors|investors?|shareholders?|stockholders?)\b", re.IGNORECASE)
SPEND_SMALL = 2_000
SPEND_LARGE = 10_000


def approval_ranges(kind: str, text: str) -> list[AuthorityScope]:
    """The People approval ranges that can say yes to a held action, the
    closest fit first. Money picks Credit, or the spend range its largest
    amount falls in (no amount reads as a large one), and a higher spend
    range may approve a lower one; a contract with a vendor goes to Vendors,
    any other to Legal; a people decision to Hiring; anything else that's
    about the board or investors to Board. Deletes, shares and the rest have
    no range: they go to the principal."""
    from openexecutive.people.models import AuthorityScope

    if kind == MONEY:
        if _CREDIT_RE.search(text):
            return [AuthorityScope.CUSTOMER_CREDIT]
        largest = max(amounts(text), default=None)
        if largest is not None and largest < SPEND_SMALL:
            return [AuthorityScope.SPEND_LT_2K, AuthorityScope.SPEND_LT_10K, AuthorityScope.SPEND_GT_10K]
        if largest is not None and largest < SPEND_LARGE:
            return [AuthorityScope.SPEND_LT_10K, AuthorityScope.SPEND_GT_10K]
        return [AuthorityScope.SPEND_GT_10K]
    if kind == CONTRACTS:
        if _VENDOR_RE.search(text):
            return [AuthorityScope.VENDOR_ONBOARDING, AuthorityScope.LEGAL_SIGN]
        return [AuthorityScope.LEGAL_SIGN]
    if kind == PEOPLE_DECISIONS:
        return [AuthorityScope.HIRING_SIGNOFF]
    if kind != DELETE_SHARE and _BOARD_RE.search(text):
        return [AuthorityScope.BOARD_COMMS]
    return []


def _approver_for(kind: str, text: str, reason: str) -> int | None:
    """Who approves a held action: the first teammate holding one of its
    approval ranges (People), else the principal. Just me works the same:
    approval ranges belong to people, not departments."""
    from openexecutive.departments.authority import _route_proposal
    from openexecutive.people.store import find_approvers

    try:
        ranges = approval_ranges(kind, text)
        for scope in ranges:
            delegated = [p for p in find_approvers(scope) if not p.is_principal]
            if delegated:
                return delegated[0].id
        decision = _route_proposal(
            department_slug="", action="propose", required_scope=ranges[0] if ranges else None,
            now=datetime.now(UTC), reason=reason,
        )
        return decision.assignee_person_id
    except Exception:
        logger.warning("take_the_lead: couldn't find an approver — leaving it to the principal", exc_info=True)
        return None


def _is_principal(person_id: int | None) -> bool:
    from openexecutive.people.store import get_person

    if person_id is None:
        return True
    try:
        person = get_person(person_id)
    except Exception:
        return True
    return person is None or bool(person.is_principal)


def hold(
    tool: str, tool_input: dict[str, Any], hit: Hit, *, source: str, mcp: bool, db_path: Path | None = None,
) -> int:
    """Make a held action a decision for its approver, with a card on Today."""
    from openexecutive.alerts.models import PRIVATE_ALERT_TAG
    from openexecutive.alerts.store import insert_alert
    from openexecutive.memory.decision_ledger import (
        DECISION_ALERT_SOURCE,
        create_decision_instance,
        decision_alert_external_id,
        decision_instance_tag,
        get_live_by_idem,
    )

    # The same action still waiting from an earlier pass is that card, not a
    # new one. Keys are unique for good, so once one is answered the next
    # identical hold takes the next numbered key.
    base = "take_the_lead:" + hashlib.sha256(
        json.dumps([tool, tool_input, mcp], sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    idem: str | None = None
    for attempt in range(_HOLD_KEY_TRIES):
        key = f"{base}:{attempt}"
        waiting = get_live_by_idem(key, db_path=db_path)
        if waiting is not None:
            return waiting.id
        if not _idem_used(key, db_path=db_path):
            idem = key
            break
    summary = summarize(tool, tool_input, mcp=mcp)
    approver = _approver_for(hit.kind, "\n".join(_strings(tool_input)), hit.reason)
    private = _is_principal(approver)
    # What it would say quotes the Executive's context: on a card that sits
    # on the team's Today only the plain line shows; the approver reads the
    # rest through the decision, which only they and the principal can open.
    shown = summary if private else summarize(tool, tool_input, mcp=mcp, quote=False)
    payload = {
        "tool": tool, "input": tool_input, "mcp": mcp, "kind": hit.kind, "rule_id": hit.rule_id,
        "reason": hit.reason, "summary": summary, "source": source,
    }
    decision_id = create_decision_instance(
        decision_class=DECISION_CLASS,
        department="executive",
        originating_session_id=None,
        proposed_payload=payload,
        idempotency_key=idem,
        gate_mode="propose",
        approver_person_id=approver,
        confidence=None,
        db_path=db_path,
    )
    external_id = decision_alert_external_id(decision_id)
    try:
        insert_alert(
            source=DECISION_ALERT_SOURCE,
            external_id=external_id,
            severity="medium",
            headline=f"The Executive wants to: {shown}"[:160],
            body=f"{shown}\n\nIt waited because {hit.reason}.",
            suggested_action=shown,
            topic_tags=[
                decision_instance_tag(decision_id),
                f"decision_class:{DECISION_CLASS}",
                # For the principal, theirs alone. One that went to a
                # teammate for their area is on the team's Today like any
                # department approval (authority.propose_via_alert).
                *([PRIVATE_ALERT_TAG] if private else []),
            ],
            dedup_key=external_id,
            routed_to_person_id=approver,
        )
    except Exception:
        logger.exception("take_the_lead: couldn't put decision %d on Today", decision_id)
    record(scope=SCOPE_EXECUTIVE, source=source, tool=tool, summary=summarize(tool, tool_input, mcp=mcp, quote=False),
           status="waiting",
           why=f"It waited because {hit.reason}.", decision_id=decision_id, db_path=db_path)
    return decision_id


_HOLD_KEY_TRIES = 50


def _idem_used(key: str, *, db_path: Path | None = None) -> bool:
    from openexecutive.memory.decision_ledger import _db_path
    from openexecutive.memory.episodic import _get_conn

    with _get_conn(db_path or _db_path()) as conn:
        return conn.execute(
            "SELECT 1 FROM decision_instances WHERE idempotency_key = ?", (key,),
        ).fetchone() is not None


_Handler = Callable[[dict[str, Any]], Awaitable[Any]]


async def _gated(name: str, inner: _Handler, tool_input: dict[str, Any], *, source: str, mcp: bool) -> str:
    try:
        lead = get(SCOPE_EXECUTIVE)
        rules = list_rules([SCOPE_COMPANY])
        hit = check(name, tool_input, lead=lead, rules=rules, mcp=mcp)
    except Exception:
        logger.warning("take_the_lead: the gate failed — holding the action", exc_info=True)
        hit = Hit("rule", "its rules couldn't be read")
    if hit is not None:
        try:
            decision_id = hold(name, tool_input, hit, source=source, mcp=mcp)
        except Exception:
            logger.exception("take_the_lead: couldn't hold %s", name)
            return json.dumps({"error": f"{name} was not done: it needs someone's approval first. Do not retry."})
        return json.dumps({
            "status": "waiting_for_approval",
            "decision_instance_id": decision_id,
            "message": (
                f"Not done yet: it waits for approval on Today because {hit.reason}. "
                "Do not retry or work around it."
            ),
        })
    result = await inner(tool_input)
    text = result if isinstance(result, str) else json.dumps(result) if isinstance(result, dict) else str(result)
    failed = result_failed(text)
    record(scope=SCOPE_EXECUTIVE, source=source, tool=name, summary=summarize(name, tool_input, mcp=mcp, quote=False),
           status="failed" if failed else "done", why=_SOURCE_WHY.get(source, ""))
    return text


def result_failed(text: str) -> bool:
    """Whether a tool's result says it failed: a JSON object with an
    ``error`` key. Free text that merely mentions "error" (a subject line,
    a quoted message) is a success."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


_SOURCE_WHY: dict[str, str] = {
    "reflection": "It came up while it looked over what's going on.",
    "research": "It came up in its research.",
    "scheduled": "Something you or it scheduled came due.",
}


def gated_handlers(handlers: dict[str, Any], *, source: str) -> dict[str, Any]:
    """``handlers`` with every acting tool behind the gate."""
    out = dict(handlers)
    for name, inner in handlers.items():
        if name in ACTING_TOOLS:
            async def _wrapped(tool_input: dict[str, Any], _name: str = name, _inner: Any = inner) -> str:
                return await _gated(_name, _inner, tool_input, source=source, mcp=False)

            out[name] = _wrapped
    return out


def mcp_tool_acts(name: str) -> bool:
    """Whether a connected tool may change something. Fails closed: a name
    that doesn't plainly read is treated as acting."""
    return bool(_MCP_ACTING_RE.search(name)) or not _MCP_READ_RE.search(name)


def gated_call_tool(call_tool: _Handler, *, source: str) -> _Handler:
    """The MCP gateway's ``call_tool`` with acting tools behind the gate."""

    async def _call(tool_input: dict[str, Any]) -> Any:
        name = str(tool_input.get("name") or "")
        if not mcp_tool_acts(name):
            return await call_tool(tool_input)
        arguments = tool_input.get("arguments")
        inner_input = arguments if isinstance(arguments, dict) else {"arguments": arguments}

        async def _inner(_: dict[str, Any]) -> Any:
            return await call_tool(tool_input)

        return await _gated(name, _inner, inner_input, source=source, mcp=True)

    return _call


async def carry_out(payload: dict[str, Any], *, by_principal: bool) -> str:
    """Do a held action someone approved: the exact call, outside the gate,
    as an unattended run. Returns the tool's result."""
    from contextlib import nullcontext

    from openexecutive.orchestrator.people_tools import grant_contact_egress
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    tool = str(payload.get("tool") or "")
    tool_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    token = current_session.set(Session(unattended=True))
    try:
        with grant_contact_egress() if by_principal else nullcontext():
            if payload.get("mcp"):
                from openexecutive.orchestrator.mcp_gateway import get_active_gateway

                gateway = get_active_gateway()
                if gateway is None:
                    raise RuntimeError("The connection to the mail and calendar tools isn't running.")
                result = await gateway.call_tool({"name": tool, "arguments": tool_input})
            else:
                from openexecutive.orchestrator.executive import _ALL_SKILL_HANDLERS

                if tool not in ACTING_TOOLS or tool not in _ALL_SKILL_HANDLERS:
                    raise RuntimeError(f"{tool} can't be done from here.")
                result = await _ALL_SKILL_HANDLERS[tool](tool_input)
    finally:
        current_session.reset(token)
    return result if isinstance(result, str) else json.dumps(result, default=str)


def resolved(decision_id: int, status: str) -> None:
    """Mirror an approval or decline into the log."""
    _set_log_status(decision_id, status)


# --------------------------------------------------------------------------- #
# Waking on what comes in
# --------------------------------------------------------------------------- #

# New mail or chat wakes the reflection this soon, so a burst is one run…
WAKE_AFTER = timedelta(minutes=5)
# …and never more often than this.
WAKE_GAP = timedelta(minutes=30)
_LAST_WAKE: dict[str, datetime] = {}


def wake(reason: str, *, now: datetime | None = None) -> int | None:
    """With Take the lead as the Executive on, run the reflection soon
    (``scheduler.runner``'s ``take_the_lead_wake`` kind) instead of waiting
    for its daily time: at most one waiting, and one every ``WAKE_GAP``.
    Returns the scheduled action's id, or None. Never raises; the pause
    holds it like any scheduled work."""
    try:
        if not executive_on():
            return None
        from openexecutive.memory.episodic import insert_scheduled_action
        from openexecutive.scheduler.runner import TAKE_THE_LEAD_WAKE_KIND, _has_pending_brief

        current = now or datetime.now(UTC)
        last = _LAST_WAKE.get(SCOPE_EXECUTIVE)
        if last is not None and current - last < WAKE_GAP:
            return None
        if _has_pending_brief(TAKE_THE_LEAD_WAKE_KIND):
            return None
        action_id = insert_scheduled_action(
            run_at=(current + WAKE_AFTER).isoformat(),
            channel="__internal__",
            channel_ref="principal",
            intent_text=f"Take the lead: look over what's new ({reason[:80]}).",
            kind=TAKE_THE_LEAD_WAKE_KIND,
        )
        # Only once it's scheduled: a failed insert mustn't hold off the next wake.
        _LAST_WAKE[SCOPE_EXECUTIVE] = current
        return action_id
    except Exception:
        logger.warning("take_the_lead: couldn't schedule a wake", exc_info=True)
        return None
