"""Deterministic policy for what the research pass may put on the watchlist.

The research workflow's watchlist pass used to call ``add_watchlist_entry``
directly: whatever the model proposed landed, live, with no rationale, at
``cadence=15min`` / ``severity_floor=low`` and (for feeds) no keyword
trigger — so one unfiltered competitor blog could out-noise the whole
watchlist. The model now only *proposes* (``propose_watch``); this module
decides, and it decides from company data, not from the model's confidence
in itself.

Two tiers:

- **direct** — added live, quietly (daily cadence, keyword trigger). Requires the grounding entity to be *in company data* (a named
  competitor / vendor / ticker / initiative / priority / the company itself,
  or an entity a department lists under its watched entities) plus enough
  corroboration (see :func:`classify`). This is the "high likelihood, worth
  watching — just add it" case.
- **suggest** — inserted in ``dry_run`` (polls, never alerts) with
  ``origin=research_proposed``; the principal — or, when the grounding term
  belongs to a department, that department's head — approves or declines
  it on ``/watchlist``. This is the "not sure" case.

Company data is more than the static profile: a department's watched
entities, charter scope and goals, and the entities named in recent
episodic decisions all feed the grounding vocabulary, and a term that came
from a department carries that department along so the watch (and every
alert it raises) is routed to it.

A proposal with nothing vouching for it (no linked finding, score 0), one
for a source already watched, one past the enabled-watch ceiling or the
per-run budgets, is rejected; a target or entity the principal declined is
refused in the tool handler before the model can route around it with a
new slug. Every decision is audited with its score and reasons.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from openexecutive.alerts.models import AlertSeverity
from openexecutive.audit import log_event as audit_log
from openexecutive.monitoring import store as ms
from openexecutive.monitoring.models import (
    DECLINE_KIND_EXPIRED,
    DECLINE_KIND_EXPLICIT,
    DECLINE_REASON_EXPIRED,
    DECLINE_REASON_NOT_RELEVANT,
    DECLINE_REASON_WRONG_SOURCE,
    MODE_ACTIVE,
    MODE_DRY_RUN,
    ORIGIN_RESEARCH,
    ORIGIN_RESEARCH_PROPOSED,
    SOURCE_KIND_EDGAR,
    SOURCE_KIND_PAGE_WATCH,
    SOURCE_KIND_QUERY,
    SOURCE_KIND_RSS,
    SOURCE_KIND_STOCK,
    SOURCE_KIND_VENDOR_STATUS,
    WatchlistItem,
)
from openexecutive.monitoring.research.models import ResearchFinding
from openexecutive.monitoring.sources._http import validate_target_url
from openexecutive.monitoring.target_validation import page_watch_target_problem
from openexecutive.monitoring.validation import normalize_target, registrable_domain

logger = logging.getLogger(__name__)

# Audit event types. The brief's "handled overnight" block renders the first
# two (see briefing.brief_state.HANDLED_EVENT_KINDS).
EVENT_ADDED = "watchlist_research_added"
EVENT_AUTO_DISABLED = "watchlist_auto_disabled"
EVENT_SUGGESTED = "watchlist_research_suggested"
EVENT_REJECTED = "watchlist_research_rejected"
EVENT_SUGGESTION_EXPIRED = "watchlist_suggestion_expired"
EVENT_SUGGESTION_APPROVED = "watchlist_suggestion_approved"
EVENT_SUGGESTION_DECLINED = "watchlist_suggestion_declined"

# Policy outcomes recorded in watchlist_policy_outcomes.
OUTCOME_APPROVED = "approved"
OUTCOME_DECLINED = "declined"
OUTCOME_EXPIRED = "expired"
OUTCOME_AUTO_DISABLED = "auto_disabled"

TIER_DIRECT = "direct"
TIER_SUGGEST = "suggest"
TIER_REJECT = "reject"

# Score needed to add without asking, and the minimum for a suggestion to
# be worth the principal's time at all. See classify() for the ledger.
DIRECT_THRESHOLD = 4
SUGGEST_THRESHOLD = 1
# History only moves the score once the policy has a real sample.
_HISTORY_MIN_SAMPLES = 5

# Hosts that are primary sources in their own right: a watch there is
# "the entity's own source" even though the host isn't the entity's domain.
PRIMARY_SOURCE_HOSTS: frozenset[str] = frozenset({
    "sec.gov", "efts.sec.gov", "data.sec.gov",
    "federalregister.gov", "eur-lex.europa.eu",
})

# Grounding-vocabulary kinds.
KIND_COMPANY = "company"
KIND_COMPETITOR = "competitor"
KIND_VENDOR = "vendor"
KIND_TICKER = "ticker"
KIND_INITIATIVE = "initiative"
KIND_PRIORITY = "priority"
# A department's explicit `watched_entities` list: a named external entity,
# like a profile vendor, so it may ground a direct add.
KIND_DEPARTMENT_ENTITY = "department_entity"
# A department's charter scope phrase or goal key result: free text, so it
# grounds suggestions only (like a priority).
KIND_DEPARTMENT_SCOPE = "department_scope"
KIND_WATCH = "watch"  # already watched (label/target) — corroborates, never grounds
GROUNDING_KINDS: frozenset[str] = frozenset({
    KIND_COMPANY, KIND_COMPETITOR, KIND_VENDOR, KIND_TICKER, KIND_INITIATIVE, KIND_PRIORITY,
    KIND_DEPARTMENT_ENTITY, KIND_DEPARTMENT_SCOPE,
})
# Kinds that name an external ENTITY the company deals with. Only these can
# ground a direct add: an initiative title or a priority sentence is company
# data too, but it is a bag of common words ("growth", "platform") that a
# planted finding can echo, so a watch grounded only in one is a suggestion.
STRONG_GROUNDING_KINDS: frozenset[str] = frozenset({
    KIND_COMPANY, KIND_COMPETITOR, KIND_VENDOR, KIND_TICKER, KIND_DEPARTMENT_ENTITY,
})
# When one term appears in several sources the strongest kind wins. Profile
# kinds outrank department kinds on purpose: a vendor the profile names and
# Finance also lists stays a `vendor` (so the policy's vendor history keeps
# applying to it) and merely gains Finance as its department.
_KIND_RANK: dict[str, int] = {
    KIND_COMPANY: 9, KIND_COMPETITOR: 8, KIND_VENDOR: 7, KIND_TICKER: 6,
    KIND_DEPARTMENT_ENTITY: 5, KIND_INITIATIVE: 4, KIND_PRIORITY: 3,
    KIND_DEPARTMENT_SCOPE: 2, KIND_WATCH: 1,
}
# Recent decisions are consulted this far back, and only this many.
DECISION_LOOKBACK_DAYS = 90
DECISION_LIMIT = 10

# Material-event words every research feed trigger carries, so a competitor
# blog surfaces "we raised / we launched / pricing" and not every post.
_EVENT_KEYWORDS: tuple[str, ...] = (
    "pricing", "launch", "acqui", "funding", "raises", "layoff", "outage",
    "breach", "partnership", "ceo", "lawsuit", "regulat",
)
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "our", "into", "from", "that", "this", "year",
    "grow", "growth", "build", "expand", "increase", "improve", "drive", "launch",
    "new", "more", "across", "team", "company", "inc", "corp", "ltd", "llc", "co",
    "group", "labs", "platform", "product", "market", "customer", "customers",
    "revenue", "sales", "enterprise", "global", "digital", "cloud", "data", "api",
    "http", "https", "www", "com", "net", "org", "feed", "blog", "status", "news",
})
# Shortest token that may match on its own inside a multi-word term. Tickers
# and short names still match exactly (whole-term equality is checked first).
_MIN_PARTIAL_TOKEN = 4
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Auto-add defaults per source kind: (cadence, severity_floor). The floor
# is low everywhere: it is no longer a filter (a medium floor here once held
# back every feed post and page change a research watch noticed). What keeps
# a research watch quiet is its trigger: the stock threshold and the
# keywords below, which the watch's "Tells you when" line says out loud.
_QUIET_DEFAULTS: dict[str, tuple[str, AlertSeverity]] = {
    SOURCE_KIND_STOCK: ("daily", AlertSeverity.LOW),
    SOURCE_KIND_EDGAR: ("daily", AlertSeverity.LOW),
    SOURCE_KIND_RSS: ("daily", AlertSeverity.LOW),
    SOURCE_KIND_VENDOR_STATUS: ("hourly", AlertSeverity.LOW),
    # Daily, not weekly: a news page changes most days, and the digest keeps
    # it to one alert a day whatever the poll rate.
    SOURCE_KIND_PAGE_WATCH: ("daily", AlertSeverity.LOW),
    SOURCE_KIND_QUERY: ("weekly", AlertSeverity.LOW),
}
_STOCK_DEFAULT_PCT = 5

# Deterministic auto-disable thresholds (research-origin rows only).
_DISABLE_MIN_FIRED = 5
_DISABLE_MIN_DISMISSED = 2
_DISABLE_MAX_TRUST = 0.5
# "No signals for N days" is deliberately NOT a retirement rule: page_watch
# and vendor_status emit nothing while the page is unchanged / the vendor
# is healthy, which is exactly a working watch. Only poll failures say a
# source is broken.
_POLL_FAILURES_TO_DISABLE = 3
_POLL_HISTORY_TO_READ = 10
# Nudge the principal only when suggestions have piled up unreviewed.
_NUDGE_MIN_PENDING = 3
_NUDGE_MIN_AGE_DAYS = 7
NUDGE_ALERT_SOURCE = "watchlist_suggestions"
# Department-routed suggestions go to the department head as one card per
# department per run (see _notify_department_heads).
DEPARTMENT_CARD_MAX_LINES = 10


# --------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------- #


@dataclass
class WatchProposal:
    """One validated ``propose_watch`` call, before policy."""

    slug: str
    signal_type: str
    target: str
    normalized_target: str
    rationale: str = ""
    grounding_entity: str = ""
    finding_index: int | None = None
    certainty: str = "unsure"
    config: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    route_to_specialist: str = ""
    display_label: str = ""
    # One plain line for the approval card ("GitHub status: outages could
    # break our deploys."), written by the model as it proposes the watch.
    card_line: str = ""


@dataclass
class PolicySettings:
    max_direct_adds: int = 2
    max_suggestions: int = 2
    max_enabled: int = 40
    suggestion_ttl_days: int = 14

    @classmethod
    def load(cls) -> PolicySettings:
        try:
            from openexecutive.config import get_settings

            s = get_settings()
            return cls(
                max_direct_adds=int(s.watchlist_research_max_direct_adds),
                max_suggestions=int(s.watchlist_research_max_proposals),
                max_enabled=int(s.watchlist_max_enabled),
                suggestion_ttl_days=int(s.watchlist_proposal_ttl_days),
            )
        except Exception:
            logger.debug("watch_policy: settings unavailable — using defaults", exc_info=True)
            return cls()


class VocabEntry(NamedTuple):
    """One grounding-vocabulary value: the term's kind, the department that
    supplied or also claims it, and the entity's own domains when the
    profile / department entry names them ("Brex (brex.com)"). With
    domains pinned, only those sites are the entity's own source; without,
    the site label has to be the entity's name."""

    kind: str
    department: str = ""
    domains: tuple[str, ...] = ()


class DepartmentRef(NamedTuple):
    """What the policy needs to know about a department for routing."""

    title: str
    head_person_id: int | None = None


Vocabulary = dict[str, VocabEntry]


@dataclass
class PolicyContext:
    """Everything classify() needs, gathered once per run."""

    vocabulary: Vocabulary  # normalized term -> (kind, department slug)
    priority_terms: list[str]
    existing: list[WatchlistItem]
    outcome_counts: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    settings: PolicySettings = field(default_factory=PolicySettings)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # slug -> (title, head person id); routing targets for department terms.
    departments: dict[str, DepartmentRef] = field(default_factory=dict)
    # Recent episodic decisions (memory.episodic.Decision or anything with
    # ``summary`` / ``department`` attributes), newest first.
    recent_decisions: list[Any] = field(default_factory=list)


@dataclass
class Decision:
    tier: str
    score: int
    entity: str
    grounding_kind: str
    reasons: list[str]
    # Department the watch is routed to ("" = the principal's).
    department: str = ""


# --------------------------------------------------------------------- #
# Grounding vocabulary
# --------------------------------------------------------------------- #


def _norm(term: str) -> str:
    return " ".join(_TOKEN_RE.findall((term or "").lower()))


# A profile list entry is often "Name (TICKER) — free-text description" or
# "Name A / Name B (products…)". Only the NAME grounds; the description is a
# bag of common words that would both hide the name (short names such as BYD
# never match inside a sentence) and poison own-source checks ("global" in
# "BYD — global cost leader" would make global.com BYD's own site).
# Em/en dashes split regardless of spacing (typography varies); a plain
# hyphen only when spaced, so "Mercedes-Benz" and "T-Mobile" survive.
_ENTRY_SPLIT_RE = re.compile(r"\s*[—–]\s*| - |[:;]")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_TICKER_RE = re.compile(r"^(?:[A-Z]{1,5}(?:\.[A-Z]{1,3})?|\d{3,6}\.[A-Z]{2,3})$")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")
_URL_RE = re.compile(r"^(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/?#].*)?$", re.IGNORECASE)
# Upper-case words that appear in parentheses in ordinary profile prose and
# are not tickers. A parenthesised token is a ticker only when it is alone
# in its group and not one of these; the profile's own `tickers` list is
# taken as-is.
# An ALL-CAPS dotted token ending in one of these is a host someone typed
# in capitals ("GM.COM"), not an exchange ticker ("1211.HK", "MBG.DE").
_TLD_NOT_EXCHANGE: frozenset[str] = frozenset({
    "COM", "NET", "ORG", "IO", "CO", "AI", "DEV", "APP", "EDU", "GOV", "INFO", "BIZ", "US", "UK",
    "CA", "EU", "IN", "AU",
})
_NOT_TICKERS: frozenset[str] = frozenset({
    "US", "USA", "EU", "UK", "EV", "EVS", "AI", "ML", "IT", "HR", "PR", "IR", "IP", "ALL", "NOW",
    "NEW", "OEM", "OEMS", "LFP", "NMC", "EMEA", "APAC", "LATAM", "CEO", "CFO", "COO", "CTO", "CMO",
    "CPO", "CHRO", "GC", "IPO", "ESG", "API", "SAAS", "B2B", "B2C", "SMB", "SME", "TCO", "ROI",
    "KPI", "OKR", "GTM", "R2", "R3", "C1", "C2", "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ",
})
# Profile entries are free text; bound what the parser looks at so a
# pathological value cannot stall the scheduler tick.
_MAX_ENTRY_CHARS = 256


class ParsedEntity(NamedTuple):
    names: list[str]
    tickers: list[str]
    domains: list[str]


def _strip_parens(text: str) -> tuple[str, list[str]]:
    """Remove parenthesised groups (innermost first, a few levels deep) and
    return the bare text plus the groups' contents."""
    groups: list[str] = []
    for _ in range(3):
        found = _PAREN_RE.findall(text)
        if not found:
            break
        groups.extend(found)
        text = _PAREN_RE.sub(" ", text)
    return text, groups


def _entry_head(text: str) -> str:
    """The name part of an entry: everything before the first separator
    that is not inside parentheses."""
    depth = 0
    masked = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        masked.append(" " if depth and ch not in "()" else ch)
    cut = _ENTRY_SPLIT_RE.search("".join(masked))
    return text[: cut.start()] if cut else text


def _host_under(host: str, pinned: tuple[str, ...]) -> bool:
    """``host`` is one of the pinned hosts or a subdomain of one
    (news.gm.com under gm.com; never gm.com.evil.com)."""
    host = host.lower().rstrip(".")
    return bool(host) and any(host == d or host.endswith("." + d) for d in pinned)


def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _ticker_in_group(parts: list[str]) -> str | None:
    """The one ticker in a parenthesised group: an ALL-CAPS ticker-shaped
    token ("TSLA", "F", "1211.HK") that is not an ordinary abbreviation, when
    it is the only such token and every other part is a host."""
    candidates = [
        part for part in parts
        if part == part.upper() and _TICKER_RE.match(part) and part not in _NOT_TICKERS
    ]
    others = [part for part in parts if part not in candidates]
    if len(candidates) == 1 and all(_DOMAIN_RE.match(o.lower()) for o in others):
        return candidates[0]
    return None


def entity_names(raw: str) -> ParsedEntity:
    """``(names, tickers, domains)`` parsed from one profile / department entry.

    "Tesla (TSLA) — Model Y is the benchmark" → names ["Tesla"], tickers
    ["TSLA"]; "GM / Chevrolet (Equinox EV, Blazer EV) — direct competitor" →
    names ["GM", "Chevrolet"]; "Brex (brex.com, status.brex.com)" → names
    ["Brex"], domains ["brex.com", "status.brex.com"]; "Acme (ACME,
    acme.com)" → ticker and domain; a bare URL or host
    ("https://status.stripe.com") → names ["stripe"], domains
    ["status.stripe.com"]; "Stripe" → ["Stripe"].

    Only parentheses in the NAME part (before the dash / colon) are read;
    anything in the description is prose. A group yields a ticker only for
    a single ALL-CAPS ticker-shaped token that is not an ordinary
    abbreviation ("US", "EV", "IT") — a capitalised brand ("(Waymo)") or a
    product list is never a ticker; type tickers in capitals. Lower-case
    dotted tokens are hosts, which pin the entity's own domains.
    Parenthesised brand names ("Alphabet (Google)") are NOT aliases: list
    them as entries of their own."""
    text = " ".join(str(raw or "").split())[:_MAX_ENTRY_CHARS]
    if not text:
        return ParsedEntity([], [], [])
    url = _URL_RE.match(text)
    # A URL, or a bare lower-case host ("status.stripe.com"); an upper-case
    # dotted symbol ("1211.HK") is a ticker, handled by the caller.
    if url and ("://" in text or "/" in text or (" " not in text and text == text.lower())):
        host = url.group(1).lower()
        label = _site_label(host)
        return ParsedEntity([label] if label else [], [], [host])
    head, groups = _strip_parens(_entry_head(text))
    tickers: list[str] = []
    domains: list[str] = []
    for group in groups:
        parts = [part.strip().rstrip(".") for part in re.split(r"[,/;]", group) if part.strip(". ")]
        for part in parts:
            typed_host = part != part.upper() or part.rsplit(".", 1)[-1] in _TLD_NOT_EXCHANGE
            if typed_host and _DOMAIN_RE.match(part.lower()) and part.lower() not in domains:
                domains.append(part.lower())
        ticker = _ticker_in_group([p for p in parts if p.lower() not in domains])
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    names = [n.strip(" ,") for n in head.split("/")]
    names = [n for n in names if len(_norm(n)) >= 2]
    return ParsedEntity(names, tickers, domains)


def ticker_symbol(raw: str) -> str:
    """The symbol in a profile `tickers` entry as typed, minus any trailing
    note: "NVDA — our supplier" → "NVDA", "1211.HK (BYD)" → "1211.HK"."""
    text = " ".join(str(raw or "").split())[:_MAX_ENTRY_CHARS]
    head, _groups = _strip_parens(_entry_head(text))
    return head.strip(" ,").upper()


def grounding_vocabulary(
    profile: Any,
    initiatives: list[Any],
    existing: list[WatchlistItem],
    departments: list[Any] | None = None,
) -> Vocabulary:
    """Normalized term → :class:`VocabEntry`. Company-data kinds win over
    ``watch`` when a term appears in both (a competitor that is already
    watched still grounds a new watch on a *different* source of theirs).

    ``departments`` are ``DepartmentState`` rows: each watched entity is a
    ``department_entity`` term and each charter-scope phrase / goal key
    result a ``department_scope`` term, all tagged with the department's
    slug. When a term is both a profile kind and a department's, the
    stronger kind wins and the department is kept — a profile competitor
    that Finance also lists stays a ``competitor`` and routes to Finance.
    When two departments claim one term, the department that supplied the
    winning kind owns it (a watched entity beats a scope phrase); on a tie
    the first one seen keeps it."""
    vocab: Vocabulary = {}

    def add(term: str, kind: str, department: str = "", domains: tuple[str, ...] = ()) -> None:
        n = _norm(term)
        # One-letter tickers (F, V, T) are real symbols; anything else that
        # short is noise.
        if len(n) < 2 and not (kind == KIND_TICKER and len(n) == 1):
            return
        current = vocab.get(n)
        if current is None:
            vocab[n] = VocabEntry(kind, department, domains)
            return
        merged = current.domains + tuple(d for d in domains if d not in current.domains)
        if _KIND_RANK[kind] > _KIND_RANK[current.kind]:
            vocab[n] = VocabEntry(kind, department or current.department, merged)
        else:
            vocab[n] = VocabEntry(current.kind, current.department or department, merged)

    for item in existing:
        for label_key in ("display_name", "feed_label", "vendor_label", "label"):
            label = item.config_json.get(label_key) if isinstance(item.config_json, dict) else None
            if label:
                add(str(label), KIND_WATCH)
        # A URL target contributes its site label only ("acme" for
        # https://status.acme.com/feed), never its scheme/host/path tokens.
        host = registrable_domain(item.target)
        add(_site_label(host) if host else item.target, KIND_WATCH)
        add(item.slug.replace("-", " "), KIND_WATCH)
    for i in initiatives:
        add(str(getattr(i, "title", "") or ""), KIND_INITIATIVE)
    def add_entity(raw: str, kind: str, department: str = "") -> None:
        # Named-entity lists: only the parsed name(s) ground; a parenthesised
        # ticker is a ticker term of its own; parenthesised domains pin the
        # entity's own sites.
        parsed = entity_names(raw)
        for name in parsed.names:
            add(name, kind, department, tuple(parsed.domains))
        for ticker in parsed.tickers:
            add(ticker, KIND_TICKER, department)

    if profile is not None:
        for p in list(getattr(getattr(profile, "strategic_priorities", None), "current_year", []) or []):
            add(str(p), KIND_PRIORITY)
        for t in list(getattr(profile, "tickers", []) or []):
            add(ticker_symbol(str(t)), KIND_TICKER)
        for v in list(getattr(profile, "vendors", []) or []):
            add_entity(str(v), KIND_VENDOR)
        for c in list(getattr(getattr(profile, "competitive_landscape", None), "primary_competitors", []) or []):
            add_entity(str(c), KIND_COMPETITOR)
        add_entity(str(getattr(profile, "name", "") or ""), KIND_COMPANY)
    for state in departments or []:
        config = getattr(state, "config", None)
        slug = str(getattr(config, "slug", "") or "")
        if not slug:
            continue
        for entity in list(getattr(config, "watched_entities", []) or []):
            add_entity(str(entity), KIND_DEPARTMENT_ENTITY, slug)
        charter = getattr(config, "charter", None)
        for phrase in list(getattr(charter, "scope", []) or []):
            add(str(phrase), KIND_DEPARTMENT_SCOPE, slug)
        for goal in list(getattr(state, "goals", []) or []):
            add(str(getattr(goal, "key_result", "") or ""), KIND_DEPARTMENT_SCOPE, slug)
    return vocab


def load_departments(db_path: Path | None = None) -> list[Any]:
    """Every ``DepartmentState`` (watched entities, charter, goals, head),
    or ``[]`` when the departments store is unavailable. Never raises."""
    try:
        from openexecutive.departments.store import list_departments

        return list(list_departments(db_path))
    except Exception:
        logger.exception("watch_policy: list_departments failed")
        return []


def recent_decisions(
    now: datetime | None = None, *, db_path: Path | None = None,
) -> list[Any]:
    """Episodic decisions from the last :data:`DECISION_LOOKBACK_DAYS`
    days, newest first, at most :data:`DECISION_LIMIT`. Never raises."""
    try:
        from openexecutive.memory.episodic import get_recent_decisions

        rows = get_recent_decisions(limit=DECISION_LIMIT, db_path=db_path)
    except Exception:
        logger.debug("watch_policy: recent decisions unavailable", exc_info=True)
        return []
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=DECISION_LOOKBACK_DAYS)
    out: list[Any] = []
    for row in rows:
        ts = _parse(str(getattr(row, "timestamp", "") or ""))
        # A row whose timestamp cannot be read must not count as "recent"
        # forever; it is simply not recent.
        if ts is None or ts < cutoff:
            continue
        out.append(row)
    return out


def department_refs(departments: list[Any] | None) -> dict[str, DepartmentRef]:
    """``slug → DepartmentRef`` for :attr:`PolicyContext.departments`."""
    refs: dict[str, DepartmentRef] = {}
    for state in departments or []:
        config = getattr(state, "config", None)
        slug = str(getattr(config, "slug", "") or "")
        if not slug:
            continue
        head = getattr(config, "head_person_id", None)
        refs[slug] = DepartmentRef(
            title=str(getattr(config, "title", "") or slug),
            head_person_id=int(head) if isinstance(head, int) else None,
        )
    return refs


def priority_terms(profile: Any) -> list[str]:
    """Distinctive words from the current-year priorities (feed triggers)."""
    out: list[str] = []
    for p in list(getattr(getattr(profile, "strategic_priorities", None), "current_year", []) or []):
        for tok in _TOKEN_RE.findall(str(p).lower()):
            if len(tok) >= 5 and tok not in _STOPWORDS and tok not in out:
                out.append(tok)
    return out[:6]


def _name_tokens(text: str, vocabulary: dict[str, Any] | None = None) -> list[str]:
    """Ordered tokens that may carry a partial match: not stopwords, and
    either long enough or a whole vocabulary term in their own right (short
    names such as IBM, AWS, BYD, GM)."""
    vocab = vocabulary or {}
    return [
        t for t in text.split()
        if t not in _STOPWORDS and (len(t) >= _MIN_PARTIAL_TOKEN or t in vocab)
    ]


def _distinctive_tokens(text: str, vocabulary: dict[str, Any] | None = None) -> set[str]:
    """Set form of :func:`_name_tokens`."""
    return set(_name_tokens(text, vocabulary))


def _entry(value: Any) -> VocabEntry:
    """Coerce a vocabulary value: a plain kind string (legacy / test dicts)
    or a :class:`VocabEntry`."""
    if isinstance(value, VocabEntry):
        return value
    if isinstance(value, tuple) and 2 <= len(value) <= 3:
        return VocabEntry(str(value[0]), str(value[1] or ""), tuple(value[2]) if len(value) == 3 else ())
    return VocabEntry(str(value), "")


def match_entity(entity: str, vocabulary: dict[str, Any]) -> tuple[str, str] | None:
    """``(term, kind)`` for the vocabulary term the entity names, else None.

    Token containment both ways, so "Acme Corp" matches "Acme" and "Acme"
    matches "Acme Corp"; single-token matches must be a whole token."""
    hit = match_vocab(entity, vocabulary)
    return (hit[0], hit[1].kind) if hit else None


def match_vocab(entity: str, vocabulary: dict[str, Any]) -> tuple[str, VocabEntry] | None:
    """``(term, VocabEntry)`` for the vocabulary term the entity names, else
    None — :func:`match_entity` with the department kept."""
    n = _norm(entity)
    exact_value = vocabulary.get(n) if n else None
    exact = _entry(exact_value) if exact_value is not None else None
    if exact is not None and exact.kind != KIND_WATCH:
        return n, exact
    if len(n) < 2:
        return None
    # Partial matching only on distinctive tokens: no stopwords, nothing
    # shorter than _MIN_PARTIAL_TOKEN unless the token is itself a whole
    # vocabulary term ("IBM Corp" still matches competitor "IBM"), so
    # "Growth Inc" cannot match the priority "drive growth in EMEA" and
    # "corp" matches nothing.
    tokens = _distinctive_tokens(n, vocabulary)
    # An exact hit on a watch label is only the fallback: a company-data term
    # that contains (or is contained by) the entity still wins.
    best: tuple[str, VocabEntry] | None = (n, exact) if exact is not None else None
    if not tokens:
        return best
    for term, value in vocabulary.items():
        entry = _entry(value)
        if entry.kind == KIND_TICKER:
            # A ticker symbol grounds only when it IS the entity ("US" is
            # not "US Foods", "IT" is not "IT Brew"); the exact case was
            # handled above.
            continue
        term_tokens = _distinctive_tokens(term, vocabulary)
        if not term_tokens or not (term_tokens <= tokens or tokens <= term_tokens):
            continue
        # Prefer a company-data kind over a watch label, then the longer term.
        if best is None or (best[1].kind == KIND_WATCH and entry.kind != KIND_WATCH) or (
            best[1].kind == entry.kind and len(term) > len(best[0])
        ):
            best = (term, entry)
    return best


def named_in_decision(
    entity_term: str, decision: Any, vocabulary: dict[str, Any] | None = None,
) -> bool:
    """Does a recent episodic decision name this entity? The whole term as
    a phrase, or EVERY distinctive token of the term as a whole word — so
    "Acme Corp" is named by "Acme pricing review" (corp is a stopword) but
    "Acme Security" is not named by "Review the security policy"."""
    text = _norm(str(getattr(decision, "summary", "") or ""))
    if not entity_term or not text:
        return False
    if f" {entity_term} " in f" {text} ":
        return True
    entity_tokens = _distinctive_tokens(entity_term, vocabulary)
    return bool(entity_tokens) and entity_tokens <= set(text.split())


def _decision_bonus(entity_term: str, ctx: PolicyContext) -> tuple[int, str, str]:
    """``(points, reason, department hint)`` from the recent decisions: +1
    when one names the entity; the department it was recorded for (when
    the policy knows that department) is offered as a routing hint."""
    for recent in ctx.recent_decisions:
        if not named_in_decision(entity_term, recent, ctx.vocabulary):
            continue
        decided_for = str(getattr(recent, "department", "") or "")
        return 1, "named in a recent decision", decided_for if decided_for in ctx.departments else ""
    return 0, "", ""


def entity_declined(entity: str, declines: list[Any]) -> bool:
    """True when the principal declined this entity as *not relevant* (the
    reason that blacklists the company/topic, not just one source)."""
    n = _norm(entity)
    if len(n) < 2:
        return False
    tokens = {t for t in n.split() if t not in _STOPWORDS and len(t) >= _MIN_PARTIAL_TOKEN}
    for d in declines:
        if getattr(d, "reason", "") != DECLINE_REASON_NOT_RELEVANT:
            continue
        dn = _norm(str(getattr(d, "entity", "") or ""))
        if len(dn) < 2:
            continue
        # Exact, or a one-word declined name ("BYD") as a whole word of the
        # entity ("BYD Auto") and vice versa — short names have no
        # distinctive tokens, so the token rule below cannot see them.
        if dn == n or (" " not in dn and dn not in _STOPWORDS and dn in n.split()):
            return True
        d_tokens = {t for t in dn.split() if t not in _STOPWORDS and len(t) >= _MIN_PARTIAL_TOKEN}
        if tokens and d_tokens and (d_tokens <= tokens or tokens <= d_tokens):
            return True
    return False


# --------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------- #


def _is_own_source(proposal: WatchProposal, entity_term: str, vocabulary: dict[str, Any]) -> bool:
    """The target is the entity's own primary source: its ticker for
    stock/edgar, its own domain for a URL, or an allowlisted primary host."""
    if proposal.signal_type in (SOURCE_KIND_STOCK, SOURCE_KIND_EDGAR):
        # Only when the grounding entity IS this ticker. A ticker the profile
        # tracks for someone else (MSFT) says nothing about "Acme Corp"; the
        # model grounds a ticker watch with the ticker itself.
        return _norm(proposal.target) == entity_term
    host = registrable_domain(proposal.target)
    if not host:
        return False
    if host in PRIMARY_SOURCE_HOSTS or any(host.endswith("." + h) for h in PRIMARY_SOURCE_HOSTS):
        return True
    # When the profile / department pins the entity's domains ("GM
    # (gm.com)"), only those sites are its own source: a look-alike
    # registration such as gm.co.ke does not qualify however well it
    # imitates the entity.
    entry_value = vocabulary.get(entity_term)
    pinned = _entry(entry_value).domains if entry_value is not None else ()
    if pinned:
        return _host_under(host, pinned)
    # Only the site's own label counts ("acme" in status.acme.com / acme.co.uk);
    # subdomain labels and the TLD never do, so "api" or ".cloud" cannot
    # make an unrelated host look like the entity's. And the label must be
    # the entity's NAME — its leading distinctive word, or all of its words
    # run together (acmepayments.com) — not any word of a multi-word entity:
    # "Acme Payments" is not payments.io. Entities are free text
    # (departments type them), so a generic trailing word must never hand
    # an unrelated domain "own source" credit.
    entity_tokens = _name_tokens(entity_term, vocabulary)
    if not entity_tokens:
        return False
    return _site_label(host) in {entity_tokens[0], "".join(entity_tokens)}


# Second-level labels under which the real site label sits one step deeper
# (acme.co.uk → "acme").
_PUBLIC_SECOND_LEVEL: frozenset[str] = frozenset({
    "co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "gob",
})


def _site_label(host: str) -> str:
    """The label that names the site: "acme" for acme.com, status.acme.com
    and acme.co.uk. Empty for a bare TLD / IP-like host."""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    if len(labels) >= 3 and labels[-2] in _PUBLIC_SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def _finding_supports(
    proposal: WatchProposal,
    finding: ResearchFinding,
    entity_term: str,
    vocabulary: dict[str, Any] | None = None,
) -> bool:
    """Does the cited finding actually concern this target? A URL target
    must share its site with one of the finding's cited URLs; a ticker or
    query target must appear in the finding's text or be about the entity
    the finding names."""
    text = _norm(f"{finding.title} {finding.summary}")
    text_tokens = set(text.split())
    entity_tokens = _distinctive_tokens(entity_term, vocabulary)
    names_entity = bool(entity_term) and (
        f" {entity_term} " in f" {text} " or bool(entity_tokens & text_tokens)
    )
    if proposal.signal_type in (SOURCE_KIND_STOCK, SOURCE_KIND_EDGAR):
        # Phrase containment: a dotted ticker normalizes to two words.
        target_phrase = _norm(proposal.target)
        return bool(target_phrase) and f" {target_phrase} " in f" {text} " or names_entity
    if proposal.signal_type == SOURCE_KIND_QUERY:
        return names_entity
    # A URL target must share its site with a cited URL: a finding that
    # merely names the entity cannot vouch for an arbitrary page about it.
    host = registrable_domain(proposal.target)
    cited = {registrable_domain(u) for u in finding.relevant_urls}
    cited.discard("")
    return bool(host) and any(
        host == c or host.endswith("." + c) or c.endswith("." + host) for c in cited
    )


def _history_adjustment(
    signal_type: str, grounding_kind: str, counts: dict[tuple[str, str], dict[str, int]],
) -> tuple[int, str]:
    tally = counts.get((signal_type, grounding_kind)) or {}
    good = tally.get(OUTCOME_APPROVED, 0)
    bad = tally.get(OUTCOME_DECLINED, 0) + tally.get(OUTCOME_AUTO_DISABLED, 0)
    n = good + bad
    if n < _HISTORY_MIN_SAMPLES:
        return 0, ""
    rate = good / n
    if rate >= 0.7:
        return 1, f"history {good}/{n} approved"
    if rate <= 0.3:
        return -1, f"history {good}/{n} approved"
    return 0, ""


def _same_source_already_watched(proposal: WatchProposal, existing: list[WatchlistItem]) -> bool:
    host = registrable_domain(proposal.target)
    for item in existing:
        # Disabled rows count: a source the sweep retired as noisy or dead
        # must not come back under a new slug with reset counters.
        if normalize_target(item.signal_type, item.target) == proposal.normalized_target:
            return True
        if host and item.signal_type == proposal.signal_type and registrable_domain(item.target) == host:
            return True
    return False


def classify(
    proposal: WatchProposal, finding: ResearchFinding | None, ctx: PolicyContext,
) -> Decision:
    """Score a proposal. The ledger:

    +2 grounding entity names something in company data (mandatory for direct)
    +1 entity is already watched under another source (corroboration)
    +1 target is the entity's own source (its ticker / its domain / a primary host)
    +1 finding confidence is high
    +1 cross-specialist consensus on the finding
    +1 the entity is named in a recent episodic decision
    ±1 policy history for (signal_type, grounding kind) at >=5 samples

    Finding points count only when the cited finding concerns this source
    (shares its site with a cited URL, or names the entity). Direct at
    >= DIRECT_THRESHOLD with the mandatory entity match, only for the
    entity's OWN source, only with a finding that concerns this source,
    and only when the entity is a named competitor /
    vendor / ticker / the company / a department's watched entity
    (STRONG_GROUNDING_KINDS); the model's own ``certainty`` can only
    downgrade. ``query`` is never direct.

    The decision carries a ``department`` when the matched term belongs to
    one (or, failing that, when the decision that names the entity does):
    the watch and its alerts are routed there instead of to the principal.
    """
    reasons: list[str] = []
    score = 0
    match = match_vocab(proposal.grounding_entity, ctx.vocabulary)
    entity_term, entry = (match if match else ("", VocabEntry("", "")))
    kind, department = entry.kind, entry.department
    grounded = kind in GROUNDING_KINDS
    if grounded:
        score += 2
        what = (
            f"a {ctx.departments[department].title if department in ctx.departments else department} "
            f"{'watched entity' if kind == KIND_DEPARTMENT_ENTITY else 'scope item'}"
            if kind in (KIND_DEPARTMENT_ENTITY, KIND_DEPARTMENT_SCOPE) and department
            else f"a company {kind}"
        )
        reasons.append(f"entity '{proposal.grounding_entity}' is {what}")
    elif kind == KIND_WATCH:
        score += 1
        reasons.append(f"entity '{proposal.grounding_entity}' matches an existing watch")
    else:
        reasons.append(f"entity '{proposal.grounding_entity}' is not in company data")

    if grounded:
        # Only a company-data entity earns the decision point; a bare watch
        # label must not pick up a department from a decision.
        bonus, why, hint = _decision_bonus(entity_term, ctx)
        if bonus:
            score += bonus
            reasons.append(why)
            # A decision made for a department is a routing hint when the
            # term itself has none.
            department = department or hint

    own_source = bool(entity_term) and _is_own_source(proposal, entity_term, ctx.vocabulary)
    if own_source:
        score += 1
        reasons.append("target is the entity's own source")
    supported = finding is not None and _finding_supports(proposal, finding, entity_term, ctx.vocabulary)
    if finding is not None and not supported:
        # The cited finding says nothing about THIS source, so it lends it
        # no credit — the model cannot borrow a strong finding's points for
        # an unrelated URL. (Company-data grounding still counts.)
        reasons.append("cited finding does not mention this source")
    if finding is not None and supported:
        if finding.confidence == "high":
            score += 1
            reasons.append("finding confidence high")
        if "," in (finding.source_specialist or ""):
            score += 1
            reasons.append("cross-specialist consensus")
    adj, why = _history_adjustment(proposal.signal_type, kind, ctx.outcome_counts)
    if adj:
        score += adj
        reasons.append(why)

    if _same_source_already_watched(proposal, ctx.existing):
        reasons.append("same source already watched")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons, department)

    enabled_count = sum(1 for i in ctx.existing if i.enabled)
    if enabled_count >= ctx.settings.max_enabled:
        # Suggestions poll too, so the ceiling has to stop them as well or
        # it would latch: every run would add polling rows and no proposal
        # could ever be direct again.
        reasons.append(f"watchlist at its ceiling ({enabled_count} enabled)")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons, department)
    if finding is None or score < SUGGEST_THRESHOLD:
        # Nothing vouches for it: no linked finding, or neither company data
        # nor evidence scored a point. Not worth the principal's time.
        reasons.append("no evidence to put in front of the principal")
        return Decision(TIER_REJECT, score, entity_term, kind, reasons, department)

    tier = TIER_SUGGEST
    if grounded and score >= DIRECT_THRESHOLD:
        tier = TIER_DIRECT
    if tier == TIER_DIRECT and proposal.signal_type == SOURCE_KIND_QUERY:
        tier = TIER_SUGGEST
        reasons.append("standing web queries are always suggestions")
    if tier == TIER_DIRECT and not own_source:
        # Adding on its own is reserved for the entity's OWN source (its
        # ticker, its site, its status page); a third-party page about a
        # competitor, however well corroborated, is the principal's call.
        tier = TIER_SUGGEST
        reasons.append("not the entity's own source")
    if tier == TIER_DIRECT and not supported:
        # Company data plus a decision mention can reach the bar on their
        # own; adding without asking still needs a finding that actually
        # cites this source, so nothing goes live on hearsay.
        tier = TIER_SUGGEST
        reasons.append("no finding cites this source")
    if tier == TIER_DIRECT and kind not in STRONG_GROUNDING_KINDS:
        tier = TIER_SUGGEST
        reasons.append(
            f"grounded only in a {kind} — needs a named competitor, vendor, ticker "
            "or department watched entity"
        )
    if tier == TIER_DIRECT and proposal.certainty != "confident":
        tier = TIER_SUGGEST
        reasons.append("model marked it unsure")
    return Decision(tier, score, entity_term, kind, reasons, department)


# --------------------------------------------------------------------- #
# Quiet defaults
# --------------------------------------------------------------------- #


def quiet_defaults(
    proposal: WatchProposal, priority_words: list[str],
) -> tuple[str, AlertSeverity, dict[str, Any]]:
    """``(cadence, severity_floor, trigger)`` a research row lands with.

    The model's cadence/floor are ignored: a research watch must be quiet
    by construction and the principal can loosen it on /watchlist."""
    cadence, floor = _QUIET_DEFAULTS.get(proposal.signal_type, ("daily", AlertSeverity.LOW))
    trigger: dict[str, Any] = dict(proposal.trigger or {})
    if proposal.signal_type == SOURCE_KIND_STOCK:
        if not isinstance(trigger.get("abs_change_pct_gte"), (int, float)):
            trigger["abs_change_pct_gte"] = _STOCK_DEFAULT_PCT
    elif proposal.signal_type in (SOURCE_KIND_RSS, SOURCE_KIND_QUERY):
        # Material-event words + priority terms only, and only for sources
        # with prose to match (feed titles, search snippets). A page_watch
        # summary is "[label] page changed — <diff>" — a keyword filter
        # there would mean the watch could never fire. The entity's own
        # name must NOT be a keyword either: feed summaries carry the feed
        # label, so it would match every entry.
        existing_kw = trigger.get("keywords")
        keywords: list[str] = [str(k) for k in existing_kw] if isinstance(existing_kw, list) else []
        for kw in list(_EVENT_KEYWORDS) + priority_words:
            if kw not in keywords:
                keywords.append(kw)
        trigger["keywords"] = keywords[:20]
    return cadence, floor, trigger


# --------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------- #


def _safe_source_url(finding: ResearchFinding | None) -> str:
    """First cited URL that is a public http(s) URL — it is rendered as a
    link beside the Approve button, so anything else is dropped."""
    for url in (finding.relevant_urls if finding else []):
        candidate = str(url or "").strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        ok, _reason = validate_target_url(candidate)
        if ok:
            return candidate[:500]
    return ""


def _policy_stamp(
    decision: Decision,
    proposal: WatchProposal,
    finding: ResearchFinding | None,
    supported: bool = True,
) -> dict[str, Any]:
    return {
        # The matched company-data term, else the model's own entity string,
        # so a not_relevant decline on an ungrounded suggestion still
        # blacklists the entity the model named.
        "entity": decision.entity or _norm(proposal.grounding_entity),
        "grounding_kind": decision.grounding_kind,
        "department": decision.department,
        "specialist": (finding.source_specialist if finding else ""),
        "score": decision.score,
        "reasons": decision.reasons[:8],
        "source_url": _safe_source_url(finding) if supported else "",
    }


_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def auto_link_finding(
    proposal: WatchProposal, findings: list[ResearchFinding], ctx: PolicyContext,
) -> int | None:
    """Index of the finding that best concerns this proposal's source, for a
    proposal the model filed without a usable ``finding_index``.

    Candidates pass the same test the score uses (:func:`_finding_supports`
    with the entity term :func:`classify` would use), so the link can never
    lend a proposal a finding the score would not honour. Among candidates,
    one that cites the target's site or names the ticker outranks one that
    only names the entity, then higher confidence, then the earlier finding — so the evidence URL stamped on the row is the
    source's own, not an unrelated company's."""
    match = match_vocab(proposal.grounding_entity, ctx.vocabulary)
    entity_term = match[0] if match else ""
    target_phrase = _norm(proposal.target)
    host = registrable_domain(proposal.target)
    entity_labels = set(_name_tokens(entity_term, ctx.vocabulary)) | {target_phrase.replace(" ", "")}
    entry_value = ctx.vocabulary.get(entity_term) if entity_term else None
    pinned = _entry(entry_value).domains if entry_value is not None else ()
    best: tuple[tuple[int, int, int, int], int] | None = None
    for index, finding in enumerate(findings):
        if not _finding_supports(proposal, finding, entity_term, ctx.vocabulary):
            continue
        text = _norm(f"{finding.title} {finding.summary}")
        cites_source = int(
            bool(host) and any(registrable_domain(u) == host for u in finding.relevant_urls)
            or bool(target_phrase) and f" {target_phrase} " in f" {text} "
        )
        # A finding whose own citations sit on the entity's site (acme.com
        # for ACME) outranks one that names it from a rival's page, so the
        # evidence link stamped on the row is the entity's, not a stranger's.
        cites_entity_site = int(any(
            _host_under(_url_host(u), pinned) if pinned
            else _site_label(registrable_domain(u)) in entity_labels
            for u in finding.relevant_urls
        ))
        key = (
            cites_source,
            cites_entity_site,
            _CONFIDENCE_RANK.get(finding.confidence, 0),
            -index,
        )
        if best is None or key > best[0]:
            best = (key, index)
    return best[1] if best else None


def _goal_for(decision: Decision, db_path: Path | None = None) -> int | None:
    """The goal a watch is for, when the term that grounded it is a goal's
    own wording. None for any other grounding, or when goals can't be read."""
    if decision.grounding_kind != KIND_DEPARTMENT_SCOPE or not decision.entity:
        return None
    try:
        from openexecutive.departments.store import list_all_goals

        for goal in list_all_goals(db_path):
            if goal.id is not None and _norm(goal.key_result) == decision.entity:
                return goal.id
    except Exception:
        logger.debug("watch_policy: goals unavailable", exc_info=True)
    return None


def policy_stamp_of(item: WatchlistItem) -> dict[str, Any]:
    stamp = item.config_json.get("_policy") if isinstance(item.config_json, dict) else None
    return dict(stamp) if isinstance(stamp, dict) else {}


# Order in which proposals are persisted: what could go live first, then what
# could be suggested, then what is already rejected (consumes no budget).
_TIER_RANK: dict[str, int] = {TIER_DIRECT: 0, TIER_SUGGEST: 1, TIER_REJECT: 2}


def _link_evidence(
    proposal: WatchProposal,
    findings: list[ResearchFinding],
    ctx: PolicyContext,
) -> tuple[WatchProposal, ResearchFinding | None, bool, str]:
    """Resolve the finding a proposal cites, repairing a missing or
    unsupported ``finding_index`` with the finding that best concerns the
    source. Returns ``(proposal, finding, supported, linked_note)``; the
    proposal is a copy when its index was repaired (the caller's is left as
    filed)."""
    finding = (
        findings[proposal.finding_index]
        if proposal.finding_index is not None and 0 <= proposal.finding_index < len(findings)
        else None
    )
    linked_note = ""
    cited_term = match_vocab(proposal.grounding_entity, ctx.vocabulary)
    given = proposal.finding_index
    given_valid = given is not None and 0 <= given < len(findings)
    if finding is None or not _finding_supports(
        proposal, finding, cited_term[0] if cited_term else "", ctx.vocabulary,
    ):
        # The model routinely omits finding_index, or guesses one that
        # does not concern this source; the finding that does is still
        # the evidence, so link it (a copy: the caller's proposal is
        # left as filed). A cited finding that does not concern the
        # source is never kept as evidence.
        cited_note = ""
        if given is not None:
            cited_note = f" (the cited #{given + 1} did not)" if given_valid else " (the cited index was invalid)"
        auto_index = auto_link_finding(proposal, findings, ctx)
        if auto_index is not None:
            finding = findings[auto_index]
            proposal = dataclasses.replace(proposal, finding_index=auto_index)
            linked_note = f"linked to finding #{auto_index + 1}, which concerns this source" + cited_note
    # A cited finding that does not concern the source still marks the
    # proposal as research-derived (it may be a suggestion) but is never
    # the evidence link shown beside Approve.
    supported = finding is not None and _finding_supports(
        proposal, finding, cited_term[0] if cited_term else "", ctx.vocabulary,
    )
    return proposal, finding, supported, linked_note


def apply_proposals(
    proposals: list[WatchProposal],
    findings: list[ResearchFinding],
    ctx: PolicyContext,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Classify and persist. Returns tool-call-shaped summaries
    (``tool='propose_watch'``, plus ``outcome`` = added | suggested | rejected),
    one per proposal in the order they were filed, so the run's artifact and
    audit accounting stay uniform.

    Proposals are applied strongest first: each is classified once against
    the run's starting state, then persisted in order of tier (direct adds,
    then suggestions, then rejects) and descending score, ties in file order.
    So when the model files more candidates than the per-run budgets or the
    enabled-watch ceiling allow, the slots go to the best-grounded ones
    rather than the earliest, and a duplicate target keeps its strongest
    filing. The score itself does not depend on what was inserted earlier,
    but the tier does: the persisting pass classifies again so a row
    inserted for a stronger proposal counts as "already watched" for a
    weaker one on the same site."""
    summaries: list[dict[str, Any] | None] = [None] * len(proposals)
    direct_left = max(0, ctx.settings.max_direct_adds)
    suggest_left = max(0, ctx.settings.max_suggestions)
    seen_targets: set[str] = set()
    suggested_by_dept: dict[str, list[WatchlistItem]] = {}
    # Rows inserted earlier in this loop must count as "already watched" for
    # later proposals; work on a copy so the caller's context is untouched.
    live_existing = list(ctx.existing)
    ctx = dataclasses.replace(ctx, existing=live_existing)

    # Pass 1 (file order): link each proposal to its evidence and classify
    # it against the starting state to fix the order.
    prepared: list[tuple[tuple[int, int, int], WatchProposal, ResearchFinding | None, bool, str]] = []
    for index, proposal in enumerate(proposals):
        proposal, finding, supported, linked_note = _link_evidence(proposal, findings, ctx)
        first_look = classify(proposal, finding, ctx)
        key = (_TIER_RANK.get(first_look.tier, len(_TIER_RANK)), -first_look.score, index)
        prepared.append((key, proposal, finding, supported, linked_note))
    prepared.sort(key=lambda item: item[0])

    # Pass 2 (strongest first): duplicates, budgets, inserts, audit rows.
    for (_tier_rank, _neg_score, index), proposal, finding, supported, linked_note in prepared:
        if proposal.normalized_target in seen_targets:
            summaries[index] = _summary(proposal, "rejected", "duplicate target in this run")
            continue
        seen_targets.add(proposal.normalized_target)
        decision = classify(proposal, finding, ctx)
        if linked_note:
            decision.reasons.insert(0, linked_note)
        if decision.tier == TIER_DIRECT and direct_left <= 0:
            decision.tier = TIER_SUGGEST
            decision.reasons.append("direct-add budget spent this run")
        if decision.tier == TIER_SUGGEST and suggest_left <= 0:
            decision.tier = TIER_REJECT
            decision.reasons.append("suggestion budget spent this run")

        if decision.tier == TIER_REJECT:
            audit_log(
                EVENT_REJECTED,
                f"Research watch rejected: {proposal.slug} — {'; '.join(decision.reasons[-2:])}",
                actor="executive",
                details={"slug": proposal.slug, "target": proposal.target,
                         "signal_type": proposal.signal_type, **_policy_stamp(decision, proposal, finding, supported)},
            )
            summaries[index] = _summary(proposal, "rejected", decision.reasons[-1] if decision.reasons else "")
            continue
        cadence, floor, trigger = quiet_defaults(proposal, ctx.priority_terms)
        config = dict(proposal.config)
        config["_policy"] = _policy_stamp(decision, proposal, finding, supported)
        if proposal.card_line:
            config[CARD_LINE_KEY] = proposal.card_line
        is_direct = decision.tier == TIER_DIRECT
        # A department-grounded watch is the department's: its alerts go to
        # the head (as of now — the head at insert time; a later head change
        # does not re-route existing rows) and its suggestion card too.
        dept_ref = ctx.departments.get(decision.department) if decision.department else None
        try:
            new_id = ms.insert_watchlist_item(
                slug=proposal.slug,
                signal_type=proposal.signal_type,
                target=proposal.target,
                config=config,
                trigger=trigger,
                cadence=cadence,
                severity_floor=floor,
                severity_ceiling=AlertSeverity.URGENT,
                route_to_specialist=proposal.route_to_specialist,
                route_to_department=decision.department if dept_ref else "",
                route_to_person_id=dept_ref.head_person_id if dept_ref else None,
                mode=MODE_ACTIVE if is_direct else MODE_DRY_RUN,
                notes=proposal.rationale[:500],
                origin=ORIGIN_RESEARCH if is_direct else ORIGIN_RESEARCH_PROPOSED,
                goal_id=_goal_for(decision, db_path),
                db_path=db_path,
            )
        except Exception:
            logger.exception("watch_policy: insert failed for %s", proposal.slug)
            summaries[index] = _summary(proposal, "rejected", "insert failed (see server log)")
            continue

        inserted = ms.get_watchlist_item(new_id, db_path=db_path)
        if inserted is not None:
            live_existing.append(inserted)
        details = {
            "watchlist_id": new_id, "slug": proposal.slug, "target": proposal.target,
            "signal_type": proposal.signal_type, "rationale": proposal.rationale[:240],
            "cadence": cadence, "severity_floor": floor.value, "trigger": trigger,
            **_policy_stamp(decision, proposal, finding, supported),
        }
        if is_direct:
            direct_left -= 1
            audit_log(
                EVENT_ADDED,
                f"Started watching {proposal.slug} — {proposal.rationale[:120] or decision.entity}",
                actor="executive", details=details,
            )
            summaries[index] = _summary(
                proposal, "added",
                f"added (score {decision.score}; {decision.reasons[0] if decision.reasons else ''})",
            )
        else:
            suggest_left -= 1
            audit_log(
                EVENT_SUGGESTED,
                f"Suggested watching {proposal.slug} — {'; '.join(decision.reasons[-2:])}",
                actor="executive", details=details,
            )
            summaries[index] = _summary(
                proposal, "suggested",
                f"suggested for approval (score {decision.score}; {decision.reasons[-1] if decision.reasons else ''})",
            )
            if dept_ref is not None and inserted is not None:
                suggested_by_dept.setdefault(decision.department, []).append(inserted)
    _notify_department_heads(suggested_by_dept, ctx)
    # Every proposal was given a summary in one of the passes above.
    return [s for s in summaries if s is not None]


def _notify_department_heads(
    suggested: dict[str, list[WatchlistItem]], ctx: PolicyContext,
) -> int:
    """One "watch suggestions" card per department whose head should
    review the suggestions this run filed for it — routed to the head, or
    to the principal when the department has no head. Weekly-keyed and
    coalesced (see ``propose_via_alert``) so re-runs refresh an open card
    and never re-mint one the head already handled. Returns cards filed."""
    if not suggested:
        return 0
    from openexecutive.departments.authority import propose_via_alert

    year, week, _ = ctx.now.isocalendar()
    suffix = f"{year}-W{week:02d}"
    filed = 0
    for slug, items in suggested.items():
        ref = ctx.departments.get(slug)
        if ref is None:
            continue
        person_id = ref.head_person_id
        if person_id is None:
            try:
                from openexecutive.people.registry import get_principal

                principal = get_principal()
                person_id = int(principal.id) if principal and principal.id is not None else None
            except Exception:
                logger.debug("watch_policy: principal lookup failed", exc_info=True)
        if person_id is None:
            continue
        # Same plain lines as the principal's card (``nudge_body``).
        lines = [f"• {card_line(i)}" for i in items[:DEPARTMENT_CARD_MAX_LINES]]
        if (more := len(items) - DEPARTMENT_CARD_MAX_LINES) > 0:
            lines.append(f"• and {more} more on the Watch list")
        body = (
            f"I'd like to keep an eye on these for {ref.title}. Approve or decline "
            "them on the Watch list page.\n" + "\n".join(lines)
        )
        try:
            alert_id = propose_via_alert(
                slug, person_id,
                summary=f"Watch suggestions for {ref.title}",
                body=body,
                suggested_action="Review the suggestions on /watchlist",
                extra_tags=["watchlist"],
                external_id_suffix=suffix,
            )
        except Exception:
            logger.exception("watch_policy: department card failed for %s", slug)
            continue
        if alert_id:
            filed += 1
    return filed


def _summary(proposal: WatchProposal, outcome: str, detail: str) -> dict[str, Any]:
    return {
        "tool": "propose_watch",
        "input_preview": f"{proposal.slug} [{proposal.signal_type}] {proposal.target}"[:120],
        "result_preview": f"{proposal.slug}: {detail}"[:160],
        "ok": outcome != "rejected",
        "outcome": outcome,
        "slug": proposal.slug,
        # What the research write-up shows: the watch in words, not its slug.
        "label": (proposal.display_label or proposal.target)[:160],
    }


# --------------------------------------------------------------------- #
# Approve / decline (called by the API routes)
# --------------------------------------------------------------------- #


def record_outcome_for(item: WatchlistItem, outcome: str, *, db_path: Path | None = None) -> None:
    stamp = policy_stamp_of(item)
    try:
        ms.record_policy_outcome(
            signal_type=item.signal_type,
            grounding_kind=str(stamp.get("grounding_kind", "")),
            specialist=str(stamp.get("specialist", "")),
            outcome=outcome,
            db_path=db_path,
        )
    except Exception:
        logger.debug("watch_policy: outcome record failed", exc_info=True)


# --------------------------------------------------------------------- #
# Sweep (scheduler, every 15 min alongside the alert expiry sweep)
# --------------------------------------------------------------------- #


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _consecutive_poll_failures(slug: str) -> int:
    """How many of the most recent polls of ``slug`` failed, counting back
    from the newest until one succeeded."""
    try:
        from openexecutive.audit.logger import get_audit_logger

        rows = get_audit_logger().query(
            event_type="external_monitor_poll", q=f"Polled {slug} (", limit=_POLL_HISTORY_TO_READ,
        )
    except Exception:
        return 0
    n = 0
    for ev in rows:
        details = ev.details if isinstance(ev.details, dict) else {}
        if details.get("watchlist_slug") not in (None, slug):
            continue
        if not details.get("failed"):
            break
        n += 1
    return n


def _auto_disable_reason(item: WatchlistItem) -> str:
    if (
        item.fired_count >= _DISABLE_MIN_FIRED
        and item.dismiss_count >= _DISABLE_MIN_DISMISSED
        and item.trust_score <= _DISABLE_MAX_TRUST
    ):
        return f"dismissed {item.dismiss_count} of {item.fired_count} alerts (trust {item.trust_score:.2f})"
    if _consecutive_poll_failures(item.slug) >= _POLL_FAILURES_TO_DISABLE:
        return f"{_POLL_FAILURES_TO_DISABLE} consecutive poll failures"
    if item.signal_type == SOURCE_KIND_PAGE_WATCH:
        # A watch added before research was held to index pages: one
        # article, a document or a sign-in wall polls forever and never
        # changes, so it is retired like a dead source.
        problem = page_watch_target_problem(item.target)
        if problem:
            return problem
    return ""


def sweep(now: datetime | None = None, *, db_path: Path | None = None) -> dict[str, int]:
    """Expire stale suggestions, auto-disable research watches that proved
    noisy or dead, and nudge the principal when suggestions pile up. Never
    raises. Returns counts for logging."""
    now = now or datetime.now(UTC)
    counts = {"expired": 0, "disabled": 0, "nudged": 0}
    settings = PolicySettings.load()
    try:
        counts["expired"] = _expire_suggestions(now, settings, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: expire failed")
    try:
        counts["disabled"] = _auto_disable(now, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: auto-disable failed")
    try:
        counts["nudged"] = _nudge_if_piled_up(now, db_path)
    except Exception:
        logger.exception("watch_policy.sweep: nudge failed")
    return counts


def _expire_suggestions(now: datetime, settings: PolicySettings, db_path: Path | None) -> int:
    ttl = timedelta(days=max(1, settings.suggestion_ttl_days))
    n = 0
    for item in ms.list_pending_suggestions(db_path=db_path):
        created = _parse(item.created_at)
        if created is None or now - created < ttl or item.id is None:
            continue
        # Compare-and-delete: a principal approving this very row a moment
        # ago must win over the sweep.
        if not ms.delete_pending_suggestion(item.id, db_path=db_path):
            continue
        ms.insert_decline(
            normalized_target=normalize_target(item.signal_type, item.target),
            kind=DECLINE_KIND_EXPIRED, reason=DECLINE_REASON_EXPIRED,
            entity=str(policy_stamp_of(item).get("entity", "")),
            signal_type=item.signal_type, slug=item.slug, db_path=db_path,
        )
        record_outcome_for(item, OUTCOME_EXPIRED, db_path=db_path)
        audit_log(
            EVENT_SUGGESTION_EXPIRED,
            f"Watch suggestion {item.slug} expired unreviewed after {settings.suggestion_ttl_days}d",
            actor="scheduler",
            details={"slug": item.slug, "target": item.target, "signal_type": item.signal_type},
        )
        n += 1
    return n


def _auto_disable(now: datetime, db_path: Path | None) -> int:
    n = 0
    for item in ms.list_watchlist(enabled_only=True, db_path=db_path):
        if item.origin != ORIGIN_RESEARCH or item.mode != MODE_ACTIVE or item.id is None:
            continue
        reason = _auto_disable_reason(item)
        if not reason:
            continue
        ms.set_enabled(item.id, False, db_path=db_path)
        record_outcome_for(item, OUTCOME_AUTO_DISABLED, db_path=db_path)
        # Remember the target (retryable after 90 days, like an unreviewed
        # expiry) so the next research run cannot re-add the same source
        # under a new slug with fresh counters.
        ms.insert_decline(
            normalized_target=normalize_target(item.signal_type, item.target),
            kind=DECLINE_KIND_EXPIRED, reason=reason[:120],
            entity=str(policy_stamp_of(item).get("entity", "")),
            signal_type=item.signal_type, slug=item.slug, db_path=db_path,
        )
        audit_log(
            EVENT_AUTO_DISABLED,
            f"Stopped watching {item.slug} — {reason}",
            actor="scheduler",
            details={"watchlist_id": item.id, "slug": item.slug, "target": item.target,
                     "signal_type": item.signal_type, "reason": reason},
        )
        n += 1
    return n


# The approval card names each suggestion the way a person would say it, one
# short line each, never its slug, its signal type or the research notes cut
# off mid-word (a 2026-11 card read "vendor-status-github (vendor_status) —
# Repeated October Actions/Pages/webhook and REST latency incid...").
CARD_LINE_KEY = "_card_line"
MAX_CARD_LINE = 120
NUDGE_SHOWN = 10


# No count in the headline: an open card is refreshed in place
# (``coalesce_alert`` updates its body only), so a count there goes stale.
NUDGE_HEADLINE = "Watch suggestions waiting for your OK"


def nudge_headline() -> str:
    return NUDGE_HEADLINE


def _plain(text: str) -> str:
    """One line with no link or markup syntax: card text comes from model
    words and labels shaped by web content, so a chat app must never render
    it as a link, and a newline must never forge a bullet."""
    return " ".join(re.sub(r"[<>\[\]|`@*~]", " ", text).split())


def card_line(item: WatchlistItem) -> str:
    """One plain line for ``item`` on the card: the line the model wrote for
    it, else what it watches in words (``describe.watching_line``)."""
    from openexecutive.monitoring.describe import watching_line

    config = item.config_json if isinstance(item.config_json, dict) else {}
    written = _plain(str(config.get(CARD_LINE_KEY) or ""))
    if written and len(written) <= MAX_CARD_LINE:
        # The model's words name no source on their own: the site it
        # watches (else its ticker or query) always follows them, so an
        # approval is of the real target.
        source = registrable_domain(item.target) or _plain(item.target)[:60]
        return f"{written} ({source})" if source else written
    label = next(
        (str(config[k]) for k in ("vendor_label", "feed_label", "display_name", "label") if config.get(k)), "",
    )
    line = _plain(watching_line(item.signal_type, item.target, label))
    return line if len(line) <= MAX_CARD_LINE * 2 else line[: MAX_CARD_LINE * 2 - 1].rstrip() + "…"


def nudge_body(pending: list[WatchlistItem]) -> str:
    lines = [f"• {card_line(i)}" for i in pending[:NUDGE_SHOWN]]
    more = len(pending) - NUDGE_SHOWN
    if more > 0:
        lines.append(f"• and {more} more on the Watch list")
    return "I'd like to keep an eye on these:\n" + "\n".join(lines)


def decline_suggestion(
    item: WatchlistItem, reason: str, *, actor: str = "principal", db_path: Path | None = None,
) -> bool:
    """Remove a pending suggestion and remember the no, so the research
    pass never proposes that source again under a new slug. False when it
    was approved, declined or removed meanwhile."""
    if item.id is None or not ms.delete_pending_suggestion(item.id, db_path=db_path):
        return False
    try:
        ms.insert_decline(
            normalized_target=normalize_target(item.signal_type, item.target),
            kind=DECLINE_KIND_EXPLICIT,
            reason=reason,
            entity=str(policy_stamp_of(item).get("entity", "")),
            signal_type=item.signal_type,
            slug=item.slug,
            db_path=db_path,
        )
        record_outcome_for(item, OUTCOME_DECLINED, db_path=db_path)
    except Exception:
        logger.exception("watch_policy: decline record failed slug=%s", item.slug)
    try:
        audit_log(
            EVENT_SUGGESTION_DECLINED,
            f"Declined watching {item.slug} ({reason}) — removed",
            actor=actor,
            details={"slug": item.slug, "watchlist_id": item.id, "target": item.target,
                     "signal_type": item.signal_type, "reason": reason, "result": "removed"},
        )
    except Exception:
        logger.exception("watch_policy: decline audit failed slug=%s", item.slug)
    return True


def suggestions_on_card(alert: Any, *, db_path: Path | None = None) -> list[WatchlistItem] | None:
    """The pending suggestions a Needs you card names, or None when it isn't
    a watch suggestions card: the principal's pile (``_nudge_if_piled_up``)
    or a department head's (``Watch suggestions for <Department>``)."""
    from openexecutive.departments.authority import PROPOSAL_ALERT_SOURCE

    tags = list(getattr(alert, "topic_tags", None) or [])
    source = getattr(alert, "source", "")
    if source == NUDGE_ALERT_SOURCE:
        pile = [i for i in ms.list_pending_suggestions(db_path=db_path) if i.route_to_person_id is None]
    elif source == PROPOSAL_ALERT_SOURCE and "watchlist" in tags:
        slugs = {t.split(":", 1)[1] for t in tags if t.startswith("department:")}
        if not slugs:
            return None
        pile = [i for i in ms.list_pending_suggestions(db_path=db_path) if i.route_to_department in slugs]
    else:
        return None
    # Only what the card shows by name: a suggestion filed after its body
    # was last written (even one whose line reads the same), or folded into
    # "and N more", was never in front of them.
    shown = {line.strip() for line in str(getattr(alert, "body", "") or "").splitlines()}
    written = _parse(getattr(alert, "last_seen_at", None)) or _parse(getattr(alert, "created_at", None))

    def _on_card(item: WatchlistItem) -> bool:
        filed = _parse(item.created_at)
        if written is not None and filed is not None and filed > written:
            return False
        return f"• {card_line(item)}" in shown

    return [i for i in pile if _on_card(i)]


def decline_card_suggestions(alert: Any, *, db_path: Path | None = None) -> int:
    """A no to a watch suggestions card is a no to what it lists: each
    pending suggestion on it is declined (that source only), so the Watch
    list stops asking. Other cards: nothing. Never raises."""
    try:
        items = suggestions_on_card(alert, db_path=db_path) or []
    except Exception:
        logger.exception("watch_policy: reading the card's suggestions failed")
        return 0
    declined = 0
    for i in items:
        # One failure never leaves the rest of the card asking.
        try:
            declined += decline_suggestion(i, DECLINE_REASON_WRONG_SOURCE, actor="executive", db_path=db_path)
        except Exception:
            logger.exception("watch_policy: declining %s failed", i.slug)
    return declined


def _nudge_if_piled_up(now: datetime, db_path: Path | None) -> int:
    # Suggestions routed to a department HEAD were put in front of that head
    # when they were filed; everything else (the principal's own, and rows
    # for a department without a head) is the principal's pile.
    pending = [
        i for i in ms.list_pending_suggestions(db_path=db_path) if i.route_to_person_id is None
    ]
    if len(pending) < _NUDGE_MIN_PENDING:
        return 0
    created = _parse(pending[0].created_at)
    if created is None or now - created < timedelta(days=_NUDGE_MIN_AGE_DAYS):
        return 0
    from openexecutive.alerts.store import coalesce_alert, insert_alert

    # One card for the whole pile: an open card is refreshed in place
    # (coalesce), and a card the principal already dismissed is not
    # re-minted until next week — acting on one suggestion must never
    # spawn another nudge.
    dedup_key = f"{NUDGE_ALERT_SOURCE}:pending"
    year, week, _ = now.isocalendar()
    external_id = f"{dedup_key}:{year}-W{week:02d}"
    body = nudge_body(pending)
    if coalesce_alert(
        source=NUDGE_ALERT_SOURCE, dedup_key=dedup_key, severity="medium", body=body, db_path=db_path,
    ):
        return 0
    alert_id = insert_alert(
        source=NUDGE_ALERT_SOURCE,
        external_id=external_id,
        severity="medium",
        headline=nudge_headline(),
        body=body,
        suggested_action="Review the suggestions on /watchlist",
        topic_tags=["watchlist"],
        dedup_key=dedup_key,
        db_path=db_path,
    )
    return 1 if alert_id else 0


__all__ = [
    "DIRECT_THRESHOLD",
    "SUGGEST_THRESHOLD",
    "EVENT_ADDED",
    "EVENT_AUTO_DISABLED",
    "EVENT_REJECTED",
    "EVENT_SUGGESTED",
    "EVENT_SUGGESTION_APPROVED",
    "EVENT_SUGGESTION_DECLINED",
    "EVENT_SUGGESTION_EXPIRED",
    "OUTCOME_APPROVED",
    "OUTCOME_AUTO_DISABLED",
    "OUTCOME_DECLINED",
    "OUTCOME_EXPIRED",
    "TIER_DIRECT",
    "TIER_REJECT",
    "TIER_SUGGEST",
    "STRONG_GROUNDING_KINDS",
    "DECISION_LIMIT",
    "DECISION_LOOKBACK_DAYS",
    "Decision",
    "DepartmentRef",
    "PolicyContext",
    "PolicySettings",
    "VocabEntry",
    "WatchProposal",
    "apply_proposals",
    "auto_link_finding",
    "classify",
    "department_refs",
    "entity_declined",
    "entity_names",
    "ticker_symbol",
    "grounding_vocabulary",
    "load_departments",
    "match_entity",
    "match_vocab",
    "named_in_decision",
    "policy_stamp_of",
    "priority_terms",
    "quiet_defaults",
    "recent_decisions",
    "record_outcome_for",
    "sweep",
]
