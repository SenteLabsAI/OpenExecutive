"""Deterministic grounding for prose nobody reads before it ships.

Structured outputs are already grounded: the alert review closes only against
evidence ids the server issued, memory extraction drops anything not quoted
verbatim, and a research watch must name an entity in company data. This
module does the same for the prose written by unattended passes — the
morning brief, the end-of-day digest, the executive reflection's flags and
outward tool calls, and the text triage writes onto an alert.

The rule is narrow and mechanical, with no model in the loop:

- **People.** A person-shaped name in the prose (a run of two or more
  capitalised words, or a single capitalised word in a person slot such as
  "ask Marcus" / "Marcus wrote") must appear in ONE source line the pass was
  given — every token of it, so a first name from one line and a surname
  from another cannot be spliced together — or belong to a roster person
  who is mentioned in some source. Being on the roster alone is not enough:
  the model only knows what it read.
- **Figures.** Money, percentages, numbers with a k/M/B multiplier and any
  bare number of 11 or more must equal (within the precision the prose
  shows: ``$1.2M`` matches ``$1,234,567``) a number in some source or the
  company profile. Dates, times, years, durations, ids, list markers,
  numbers glued to letters (``Q3``, ``FY26``) and small bare counts are not
  checked.

Every entry point fails open, like ``orchestrator.outbound_guard``: an
internal error is logged and the input comes back unchanged, because a
checker must never turn a working brief into a crash. ``GROUNDING_CHECKS``
selects ``enforce`` (hold back / refuse), ``report`` (audit only, text
unchanged) or ``off``.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("enforce", "report", "off")

HELD_BACK_NOTE = (
    "_Held back {n} line{s} that named people or figures I couldn't match to "
    "your data — /today has the full list._"
)
HELD_BACK_ALL = (
    "Everything I drafted for this brief named people or figures I couldn't "
    "match to your data, so I've held it back — /today has the full list."
)
SOURCES_HEADER = "**Sources**"

_MAX_CITED_SOURCES = 9
_SNIPPET_CHARS = 90
_MAX_RESULT_CHARS = 4000


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GroundingSource:
    """One line of input a pass was given. ``extra_values`` are numbers the
    line implies without spelling them (a section header's bullet count)."""

    ref: str
    label: str
    text: str
    extra_values: tuple[float, ...] = ()


@dataclass(frozen=True)
class Figure:
    start: int
    end: int
    raw: str
    value: float
    tol: float
    kind: str  # money | pct | count


@dataclass
class LineCheck:
    index: int
    text: str
    names_bad: list[str] = field(default_factory=list)
    figures_bad: list[str] = field(default_factory=list)
    grounded: list[tuple[Figure, GroundingSource]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.names_bad and not self.figures_bad


@dataclass
class GroundingReport:
    lines: list[LineCheck]

    @property
    def bad_lines(self) -> list[LineCheck]:
        return [c for c in self.lines if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.bad_lines

    @property
    def names_bad(self) -> list[str]:
        return _dedupe(n for c in self.lines for n in c.names_bad)

    @property
    def figures_bad(self) -> list[str]:
        return _dedupe(f for c in self.lines for f in c.figures_bad)

    def summary(self) -> str:
        items = [f"'{x}'" for x in (self.names_bad + self.figures_bad)[:6]]
        return ", ".join(items)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_FOLDS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").translate(_FOLDS)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_fold(text).casefold()))


def _mask(text: str, pattern: re.Pattern[str]) -> str:
    """Blank each match with spaces so positions in ``text`` stay valid."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(
    r"(?P<cur>[$€£])?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<mult>bn|mm|thousand|million|billion|[kmb])(?![a-z]))?"
    r"(?:\s?(?P<pct>%|percent\b))?",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
}
_MONTHS = (
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
)
# Masked before any number is read, in prose and sources alike.
_ALWAYS_MASK = re.compile(
    r"https?://\S+"
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE,
)
# Masked in prose only: things that look like figures but never are.
_PROSE_MASK = re.compile(
    r"\[\d{1,2}\]"
    r"|^\s*\d+[.)]\s"
    r"|\b\d{1,2}\s?[ap]\.?m\b\.?"
    rf"|\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}(?![a-z])"
    r"|\b\d+(?:\.\d+)?\s?(?:minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|"
    r"months?|mos?|years?|yrs?|quarters?|business days?)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _figure_from(m: re.Match[str]) -> tuple[float, float, str]:
    num = m.group("num")
    mult_key = (m.group("mult") or "").lower()
    mult = _MULTIPLIERS.get(mult_key, 1.0)
    decimals = len(num.split(".", 1)[1]) if "." in num else 0
    value = float(num.replace(",", "")) * mult
    tol = (10.0 ** -decimals) / 2 * mult
    kind = "money" if m.group("cur") else "pct" if m.group("pct") else "count"
    return value, tol, kind


def extract_figures(text: str) -> list[Figure]:
    """The figures in ``text`` that must be grounded (see module doc)."""
    masked = _mask(_mask(text, _ALWAYS_MASK), _PROSE_MASK)
    out: list[Figure] = []
    for m in _NUM_RE.finditer(masked):
        s = m.start("num")
        before = masked[s - 1] if s > 0 else " "
        if m.group("cur"):
            before = " "
        num_end = m.end("num")
        after = masked[num_end] if num_end < len(masked) else " "
        if before.isalpha() or before in "#_.-/=" or before.isdigit():
            continue  # Q3, FY26, #42, v1.2, 2026-…, id=7
        if not (m.group("mult") or m.group("pct")) and (after.isalpha() or after == "_"):
            continue  # 3d, 5th, 2x
        value, tol, kind = _figure_from(m)
        plain = kind == "count" and not m.group("mult")
        if plain and value <= 10:
            continue
        if plain and "," not in m.group("num") and "." not in m.group("num") and 1990 <= value <= 2100:
            continue  # a year
        start = m.start("cur") if m.group("cur") else s
        out.append(Figure(start, m.end(), text[start:m.end()].strip(), value, tol, kind))
    return out


def _values_in(text: str) -> tuple[float, ...]:
    """Every number a source line holds, read generously: glued forms
    (``14d``, ``at_risk=2``) count, and a scaled figure also offers its
    unscaled digits."""
    values: list[float] = []
    for m in _NUM_RE.finditer(_mask(_fold(text), _ALWAYS_MASK)):
        value, _tol, _kind = _figure_from(m)
        values.append(value)
        if m.group("mult"):
            values.append(float(m.group("num").replace(",", "")))
    return tuple(values)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

def _wordset(*chunks: str) -> frozenset[str]:
    return frozenset(w.casefold() for chunk in chunks for w in chunk.split())


_STOP = _wordset(
        # calendar
        "Monday Tuesday Wednesday Thursday Friday Saturday Sunday Mon Tue Tues Wed "
        "Thu Thur Thurs Fri Sat Sun January February March April May June July "
        "August September October November December Jan Feb Mar Apr Jun Jul Aug "
        "Sep Sept Oct Nov Dec Today Tonight Tomorrow Yesterday Morning Afternoon "
        "Evening Weekend Week Month Quarter Year Overnight EOD"
        # the brief / digest / reflection section headers
        " Top Call Three What Changed Handled Needs Need Waiting On At Risk Due "
        "This Goals Goal Still Pending Sleep Open Decisions Decision Acted Flagged "
        "For The Brief Quiet Move Bottom Line Did Carried Over Sources Source Held "
        "Back Grounding Tool Calls Run Day Digest Update Status Summary Next Steps "
        "Action Actions Items Item Note Notes Heads Up Reminder FYI"
        # determiners, pronouns, function words
        " A An And Or But Nor So Yet If Then Than That These Those This There "
        "Their They Them We Us Our You Your Yours He She His Her Hers It Its I Me "
        "My Mine Who Whom Whose Which What When Where Why How All Any Each Every "
        "Everyone Everybody Everything Someone Somebody Something Anyone Anybody "
        "Anything Nobody Nothing None No Not Yes Both Either Neither Few Many Most "
        "Some Several Other Another Such Of To In Into Onto Off Out Up Down With "
        "Without Within From By As About Above Below After Before Between Per Via "
        "Re Fwd Hi Hey Hello Dear Thanks Thank Please Also Just Only Still New Old "
        "Here Now Soon Later Again Once"
        # imperative openers
        " Ask Approve Tell Ping Nudge Chase Reply Review Sign Send Book Decide "
        "Confirm Follow Check Loop Hold Push Clear Close Escalate Share Draft Focus "
        "Protect Delegate Prep Prepare Read Skim Block Schedule Reschedule Cancel "
        "Accept Decline Answer Respond Consider Keep Watch Expect Note Plan Start "
        "Finish Wrap Get Give Make Take Let Go Set Put Use Try Call Meet Talk "
        "Discuss Sync Align Update Fix Resolve Unblock Flag Raise Move Pick Grab"
        # product and org nouns
        " Open Executive OE Slack Discord Telegram Gmail Google Calendar Drive Docs "
        "Sheets Notion Zoom Meet Teams Email Inbox Chat Web Watchlist Board Finance "
        "Sales Marketing Engineering Product Operations Ops Legal People HR Support "
        "Team Company Department Departments Area Areas Customer Customers Revenue "
        "Pipeline Budget Hiring Security Design Data Research Strategy CEO CFO COO "
        "CTO CMO VP Head Director Manager Lead Principal Owner Founder"
)
# Words inside a title-cased run that split it (real names never hold them).
_SPLITTERS = _wordset("the and of for on at to with in your you this that a an or from by")
_PARTICLES = _wordset("van von de da di del della la le du al bin ibn st")
_BEFORE_SLOT = _wordset(
    "ask asked asking tell told ping pinged nudge nudged chase chased remind "
    "reminded email emailed message messaged dm call called cc cc'd meet met "
    "with from by to and for thank thanks per"
)
_AFTER_SLOT = _wordset(
    "wrote writes asked asks said says replied replies sent sends emailed "
    "messaged pinged called texted confirmed approved agreed declined promised "
    "mentioned requested signed owes flagged hasn't hasnt isn't"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['\-][^\W\d_]+)*'?", re.UNICODE)
_SEGMENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\s[-]\s|[()\[\]\"•|,]|\s-{2,}\s")
_MD_MASK = re.compile(
    r"\*\*|__|`|^\s*#{1,6}\s|^\s*>+\s?|^\s*[-*+]\s|\]\([^)]*\)|https?://\S+"
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    re.MULTILINE,
)


def _is_name_token(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    if len(letters) < 2 or not word[0].isupper():
        return False
    if not any(c.islower() for c in word):
        return False
    for i, part in enumerate(re.split(r"['\-]", word)):
        if not part:
            continue
        tail = part[1:]
        if tail and not tail.islower():
            return False  # OKR, iPhone, McKinsey — never a name token
        if i == 0 and not part[0].isupper():
            return False
    return True


@dataclass(frozen=True)
class _Word:
    text: str       # possessive stripped
    start: int
    end: int
    possessive: bool


def _segment_words(segment: str) -> list[_Word]:
    words: list[_Word] = []
    for m in _WORD_RE.finditer(segment):
        raw = m.group(0)
        possessive = False
        if raw.lower().endswith("'s"):
            raw, possessive = raw[:-2], True
        elif raw.endswith("'"):
            raw = raw[:-1]
            possessive = raw.lower().endswith("s")
        if raw:
            words.append(_Word(raw, m.start(), m.start() + len(raw), possessive))
    return words


def extract_names(text: str) -> list[str]:
    """Person-shaped names in ``text`` that must be grounded."""
    cleaned = _mask(_fold(text), _MD_MASK)
    out: list[str] = []
    for line in cleaned.splitlines():
        for segment in _SEGMENT_SPLIT.split(line):
            if segment and segment.strip():
                out.extend(_segment_names(segment))
    return _dedupe(out)


def _segment_names(segment: str) -> list[str]:
    words = _segment_words(segment)
    names: list[str] = []
    i = 0
    while i < len(words):
        if not _is_name_token(words[i].text):
            i += 1
            continue
        run = [i]
        j = i + 1
        while j < len(words):
            if words[j - 1].possessive or segment[words[j - 1].end:words[j].start] != " ":
                break
            w = words[j].text
            particle = (
                w.casefold() in _PARTICLES
                and j + 1 < len(words)
                and _is_name_token(words[j + 1].text)
            )
            if not (_is_name_token(w) or particle):
                break
            run.append(j)
            j += 1
        names.extend(_run_candidates(words, run))
        i = j
    return names


def _run_candidates(words: list[_Word], run: list[int]) -> list[str]:
    """Split a title-cased run at function words, trim STOP words from each
    piece's edges, and keep what is person-shaped."""
    pieces: list[list[int]] = [[]]
    for idx in run:
        if words[idx].text.casefold() in _SPLITTERS:
            pieces.append([])
        else:
            pieces[-1].append(idx)
    out: list[str] = []
    for piece in pieces:
        while piece and (words[piece[0]].text.casefold() in _STOP or words[piece[0]].text.casefold() in _PARTICLES):
            piece = piece[1:]
        while piece and (words[piece[-1]].text.casefold() in _STOP or words[piece[-1]].text.casefold() in _PARTICLES):
            piece = piece[:-1]
        if not piece:
            continue
        if len(piece) >= 2:
            out.append(" ".join(words[k].text for k in piece))
            continue
        k = piece[0]
        prev_word = words[k - 1].text.casefold() if k > 0 else ""
        prev2 = words[k - 2].text.casefold() if k > 1 else ""
        next_word = words[k + 1].text.casefold() if k + 1 < len(words) else ""
        in_slot = (
            (k > 0 and prev_word in _BEFORE_SLOT)
            or (prev2 == "loop" and prev_word == "in")
            or next_word in _AFTER_SLOT
            or words[k].possessive
        )
        if in_slot:
            out.append(words[k].text)
    return out


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


class _Index:
    """Sources pre-digested for lookups: token sets, numbers, and which
    roster people some source mentions."""

    def __init__(self, sources: list[GroundingSource], roster: list[str]) -> None:
        self.sources = sources
        self.tokens = [_tokens(s.text) | _tokens(s.label) for s in sources]
        self.values = [_values_in(s.text) + s.extra_values for s in sources]
        people = [(_tokens(n), n) for n in roster if n and n.strip()]
        firsts: dict[str, int] = {}
        for _toks, name in people:
            first = _fold(name).split()[0].casefold() if name.split() else ""
            if first:
                firsts[first] = firsts.get(first, 0) + 1
        self.present: list[set[str]] = []
        for toks, name in people:
            if not toks:
                continue
            first = _fold(name).split()[0].casefold()
            for src in self.tokens:
                if toks <= src or (firsts.get(first) == 1 and first in src):
                    self.present.append(toks)
                    break

    def name_ok(self, name: str) -> bool:
        toks = _tokens(name)
        if not toks:
            return True
        if any(toks <= src for src in self.tokens):
            return True
        return any(toks <= person for person in self.present)

    def figure_sources(self, fig: Figure) -> list[int]:
        target = abs(fig.value)
        hits = [
            i for i, values in enumerate(self.values)
            if any(abs(abs(v) - target) <= fig.tol + 1e-9 * max(1.0, target) for v in values)
        ]
        if hits or fig.kind != "pct":
            return hits
        # A percentage the model worked out from two numbers on one line
        # ("40 of 100" grounds 40%).
        for i, values in enumerate(self.values):
            small = values[:12]
            if any(
                b and abs(100.0 * a / b - target) <= fig.tol + 1e-9
                for a in small for b in small if a is not b
            ):
                hits.append(i)
        return hits


_CLAUSE_BREAK = re.compile(r"[;,.!?()]\s|\s[-–—]\s|[()]")


def _clause(line: str, fig: Figure) -> str:
    """The clause of ``line`` a figure sits in — what its citation is about."""
    start = 0
    end = len(line)
    for m in _CLAUSE_BREAK.finditer(line):
        if m.end() <= fig.start:
            start = m.end()
        elif m.start() >= fig.end:
            end = m.start()
            break
    return line[start:end]


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if len(t) >= 3 and not t.isdigit() and t not in _SPLITTERS}


def check_text(
    text: str, sources: list[GroundingSource], roster: list[str] | None = None,
    *, index: _Index | None = None,
) -> GroundingReport:
    """Check every line of ``text``. Pure: no I/O."""
    idx = index or _Index(sources, roster or [])
    lines: list[LineCheck] = []
    for n, line in enumerate(text.splitlines()):
        check = LineCheck(index=n, text=line)
        for name in extract_names(line):
            if not idx.name_ok(name):
                check.names_bad.append(name)
        for fig in extract_figures(line):
            hits = idx.figure_sources(fig)
            if not hits:
                check.figures_bad.append(fig.raw)
                continue
            near = _content_tokens(_clause(line, fig))
            whole = _content_tokens(line)
            best = max(
                hits,
                key=lambda i: (len(near & idx.tokens[i]), len(whole & idx.tokens[i]), -i),
            )
            check.grounded.append((fig, idx.sources[best]))
        lines.append(check)
    return GroundingReport(lines)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

_HEADER_HEAD = re.compile(r"^([A-Z][A-Z0-9 '’&/\-—]*[A-Z])\b")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]|>|\d+[.)])\s*")


def _header_label(line: str) -> str | None:
    """The label of a rendered-context section header (``INBOUND SINCE THE
    LAST BRIEF (3 messages…):`` → "Inbound since the last brief"), or None."""
    if line.startswith((" ", "-", "\t")):
        return None
    m = _HEADER_HEAD.match(line)
    if not m:
        return None
    head = m.group(1).split(" — ")[0].strip(" -—")
    if sum(c.isalpha() for c in head) < 3:
        return None
    rest = line[m.end():].lstrip()
    if not (rest.startswith(("(", ":", "—", "-")) or not rest):
        return None
    return head[:1] + head[1:].lower()


def sources_from_context(rendered: str, prefix: str = "ctx") -> list[GroundingSource]:
    """One source per non-empty line of a rendered context, labelled with the
    section it sits under. A header also carries its bullet count, so "three
    new messages" can be grounded by the list itself."""
    label = "Context"
    out: list[GroundingSource] = []
    header_at: int | None = None
    count = 0

    def close_header() -> None:
        if header_at is not None and count:
            h = out[header_at]
            out[header_at] = GroundingSource(h.ref, h.label, h.text, h.extra_values + (float(count),))

    for raw in (rendered or "").splitlines():
        if not raw.strip():
            continue
        head = _header_label(raw)
        if head is not None:
            close_header()
            label, count = head, 0
            header_at = len(out)
            out.append(GroundingSource(f"{prefix}{len(out)}", label, raw.strip()))
            continue
        if _BULLET_PREFIX.match(raw):
            count += 1
        text = _BULLET_PREFIX.sub("", raw).strip()
        if text:
            out.append(GroundingSource(f"{prefix}{len(out)}", label, text))
    close_header()
    return out


def sources_from_text(text: str, label: str, prefix: str) -> list[GroundingSource]:
    return [
        GroundingSource(f"{prefix}{i}", label, line.strip())
        for i, line in enumerate((text or "").splitlines())
        if line.strip()
    ]


def profile_sources() -> list[GroundingSource]:
    """The company profile's lines (name, ARR, burn, runway, key metrics,
    leadership, vendors…), labelled "Company profile". [] when unavailable."""
    try:
        from openexecutive.onboarding.profile_builder import load_or_create_profile

        block = load_or_create_profile().to_prompt_block()
    except Exception:
        logger.debug("grounding: profile unavailable", exc_info=True)
        return []
    lines = [ln for ln in block.splitlines() if ln.strip() and not ln.startswith("## ")]
    return sources_from_text("\n".join(lines), "Company profile", "profile")


def org_sources() -> list[GroundingSource]:
    """Department titles and slugs — names the prose may use for an area
    without the context spelling them. [] when unavailable."""
    try:
        from openexecutive.departments.registry import list_states

        titles = [f"{d.config.title} ({d.config.slug})" for d in list_states()]
    except Exception:
        logger.debug("grounding: departments unavailable", exc_info=True)
        return []
    return sources_from_text("\n".join(titles), "Departments", "dept")


def roster() -> list[str]:
    """Full names of everyone on the roster, contacts included."""
    try:
        from openexecutive.people.store import list_people

        return [p.full_name for p in list_people(include_contacts=True) if p.full_name]
    except Exception:
        logger.debug("grounding: roster unavailable", exc_info=True)
        return []


def grounding_mode() -> str:
    try:
        from openexecutive.config import get_settings

        mode = str(get_settings().grounding_checks)
    except Exception:
        return "enforce"
    return mode if mode in MODES else "enforce"


def citations_enabled() -> bool:
    try:
        from openexecutive.config import get_settings

        return bool(get_settings().grounding_citations)
    except Exception:
        return True


def _audit(summary: str, details: dict[str, Any], *, private: bool) -> None:
    try:
        from openexecutive.audit import log_event

        log_event("grounding", summary, actor="executive", details=details, private=private)
    except Exception:
        logger.debug("grounding: audit failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Briefs: drop, note, cite
# --------------------------------------------------------------------------- #

_HEADER_LINE = re.compile(r"^\s*(?:#{1,6}\s+\S.*|\*\*[^*]+\*\*:?|__[^_]+__:?)\s*$")


def _is_header(line: str) -> bool:
    return bool(_HEADER_LINE.match(line))


def _drop_lines(text: str, bad: set[int]) -> tuple[str, list[str]]:
    """Remove the ``bad`` line indexes, then any header left with nothing
    under it, then runs of blank lines."""
    lines = text.splitlines()
    held = [lines[i] for i in sorted(bad) if i < len(lines)]
    kept = [ln for i, ln in enumerate(lines) if i not in bad]
    out: list[str] = []
    for i, ln in enumerate(kept):
        if _is_header(ln):
            rest = kept[i + 1:]
            has_body = False
            for nxt in rest:
                if _is_header(nxt):
                    break
                if nxt.strip():
                    has_body = True
                    break
            if not has_body:
                continue
        if not ln.strip() and (not out or not out[-1].strip()):
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out), held


_SNIPPET_STRIP = re.compile(r"[\[\]()<>*_`|\\]")
_LINKISH = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w.])@(?=\w)")


def _plain(text: str) -> str:
    """Quoted text made inert for every channel the prose reaches: links
    masked (the Sources list and a fallback quote attacker-written mail, and
    Gmail / Slack / Discord linkify bare URLs), Markdown and Slack's
    ``<!channel>`` / ``<url|text>`` syntax stripped, @-mentions broken."""
    text = _SNIPPET_STRIP.sub(" ", _LINKISH.sub("\x00", text))
    text = _MENTION.sub("", text).replace("\x00", "(link removed)")
    return " ".join(text.split())


def _snippet(text: str, limit: int = _SNIPPET_CHARS) -> str:
    clean = _plain(text)
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _cite(text: str, report: GroundingReport) -> tuple[str, int]:
    """Put ``[n]`` after each grounded figure and a Sources list at the end.
    Numbered by first appearance; at most ``_MAX_CITED_SOURCES`` sources."""
    numbers: dict[str, int] = {}
    listed: list[GroundingSource] = []
    lines = text.splitlines()
    by_index = {c.index: c for c in report.lines}
    for i, line in enumerate(lines):
        check = by_index.get(i)
        if check is None or not check.grounded or _is_header(line):
            continue
        inserts: list[tuple[int, str]] = []
        for fig, src in check.grounded:
            n = numbers.get(src.ref)
            if n is None:
                if len(listed) >= _MAX_CITED_SOURCES:
                    continue
                listed.append(src)
                n = numbers[src.ref] = len(listed)
            if line[fig.end:fig.end + 1] == "[" or line[fig.end:fig.end + 2] == " [":
                continue
            tail = " " if line[fig.end:fig.end + 1] == "(" else ""
            inserts.append((fig.end, f" [{n}]{tail}"))
        for pos, mark in sorted(inserts, reverse=True):
            line = line[:pos] + mark + line[pos:]
        lines[i] = line
    if not listed:
        return text, 0
    footer = [
        "", SOURCES_HEADER,
        *(f"- [{n}] {_snippet(src.label, 40)} — {_snippet(src.text)}" for n, src in enumerate(listed, 1)),
    ]
    return "\n".join(lines + footer), len(listed)


_REPAIR_INSTRUCTION = (
    "Below is the context you were given and a draft you wrote from it. The "
    "draft names people or figures that do not appear anywhere in the "
    "context: {items}. Rewrite the draft in the same format, removing each "
    "of those or correcting it to exactly what the context says. Change "
    "nothing else. Output only the rewritten message."
)


async def _repair(draft: str, context: str, system: str, report: GroundingReport) -> str:
    from openexecutive.agents.utility_fast import get_fast_model
    from openexecutive.providers import get_provider

    model = get_fast_model()
    content = (
        f"{context}\n\n---\nDRAFT:\n{draft}\n\n"
        + _REPAIR_INSTRUCTION.format(items=report.summary())
    )
    response = await get_provider(model).messages_create(
        model=model,
        max_tokens=700,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    blocks = [b for b in response.content if getattr(b, "type", "") == "text"]
    return blocks[0].text.strip() if blocks else ""


def context_sources(context: str) -> list[GroundingSource]:
    return sources_from_context(context) + profile_sources() + org_sources()


@dataclass
class BriefGrounding:
    """What the pass did to one brief."""

    mode: str
    held: list[str] = field(default_factory=list)
    names_bad: list[str] = field(default_factory=list)
    figures_bad: list[str] = field(default_factory=list)
    cited: int = 0
    repaired: bool = False


async def ground_brief(
    text: str,
    *,
    context: str,
    kind: str,
    private: bool = False,
    system: str | None = None,
    surface: str = "brief",
) -> tuple[str, BriefGrounding | None]:
    """Ground a brief or digest against the context it was written from.

    ``enforce``: one repair call when something is ungrounded (only when
    ``system`` is given), then drop the lines still ungrounded, drop headers
    left empty, add the held-back note and the citations. ``report``: audit
    only, text unchanged. ``off``: nothing. Never raises; on an internal
    error the text comes back unchanged."""
    mode = grounding_mode()
    if mode == "off" or not text.strip():
        return text, None
    try:
        sources = context_sources(context)
        idx = _Index(sources, roster())
        report = check_text(text, sources, index=idx)
        result = BriefGrounding(mode=mode)
        if not report.ok and mode == "enforce" and system:
            try:
                fixed = await _repair(text, context, system, report)
            except Exception:
                logger.warning("grounding: repair call failed; dropping lines instead", exc_info=True)
                fixed = ""
            if fixed:
                fixed_report = check_text(fixed, sources, index=idx)
                if len(fixed_report.bad_lines) <= len(report.bad_lines):
                    text, report, result.repaired = fixed, fixed_report, True
        result.names_bad = report.names_bad[:10]
        result.figures_bad = report.figures_bad[:10]
        out = text
        if mode == "enforce":
            if not report.ok:
                out, result.held = _drop_lines(text, {c.index for c in report.bad_lines})
                if any(ln.strip() and not _is_header(ln) for ln in out.splitlines()):
                    report = check_text(out, sources, index=idx)
                else:
                    out = HELD_BACK_ALL
            if citations_enabled() and out != HELD_BACK_ALL:
                out, result.cited = _cite(out, report)
            if result.held and out != HELD_BACK_ALL:
                n = len(result.held)
                note = HELD_BACK_NOTE.format(n=n, s="" if n == 1 else "s")
                body, sep, footer = out.partition("\n\n" + SOURCES_HEADER)
                out = f"{body}\n\n{note}" + (sep + footer if sep else "")
        else:
            result.held = [c.text for c in report.bad_lines]
        if result.held or result.repaired:
            verb = "held back" if mode == "enforce" else "found (report only)"
            _audit(
                f"Grounding: {verb} {len(result.held)} line(s) of the {surface}",
                {
                    "surface": surface, "kind": kind, "mode": mode,
                    "repaired": result.repaired, "cited": result.cited,
                    "names_bad": result.names_bad, "figures_bad": result.figures_bad,
                    "held_back": [h[:200] for h in result.held[:5]],
                },
                private=private,
            )
        return out, result
    except Exception:
        logger.exception("grounding: brief check failed; delivering unchanged")
        return text, None


# --------------------------------------------------------------------------- #
# Unattended tool loops: refuse ungrounded outward prose
# --------------------------------------------------------------------------- #

OUTWARD_FIELDS: dict[str, tuple[str, ...]] = {
    "create_alert": ("subject", "body"),
    "message_person": ("text",),
    "send_department_message": ("text",),
    "send_company_broadcast": ("text",),
    "schedule_followup": ("intent",),
}

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class GroundingScope:
    """The inputs of one unattended tool loop. Outward tools refuse prose
    that names people or figures not in them; every tool result joins them,
    so a ``lookup_person`` answer grounds the DM that follows it."""

    def __init__(
        self,
        sources: list[GroundingSource],
        roster_names: list[str],
        *,
        surface: str,
        private: bool = False,
    ) -> None:
        self.sources = list(sources)
        self.roster = list(roster_names)
        self.surface = surface
        self.private = private
        self.mode = grounding_mode()
        self._results = 0
        self._index: _Index | None = None

    def _idx(self) -> _Index:
        if self._index is None:
            self._index = _Index(self.sources, self.roster)
        return self._index

    def add_text(self, label: str, text: str) -> None:
        self._results += 1
        self.sources.extend(
            sources_from_text(text[:_MAX_RESULT_CHARS], label, f"r{self._results}.")
        )
        self._index = None

    def check(self, text: str) -> GroundingReport:
        return check_text(text, self.sources, index=self._idx())

    def refusal(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        """The reason ``tool`` must not run, or None. Audits every finding;
        only ``enforce`` refuses."""
        if self.mode == "off" or tool not in OUTWARD_FIELDS:
            return None
        try:
            prose = "\n".join(
                str(tool_input.get(f) or "") for f in OUTWARD_FIELDS[tool]
            ).strip()
            if not prose:
                return None
            report = self.check(prose)
            if report.ok and tool == "message_person":
                return None
            if not report.ok and tool == "message_person":
                # The recipient's own name is grounded by the call itself.
                recipient = _person_name(tool_input.get("person_id"))
                if recipient:
                    extra = self.sources + [GroundingSource("recipient", "Recipient", recipient)]
                    report = check_text(prose, extra, self.roster)
            if report.ok:
                return None
            reason = (
                f"Not sent — couldn't find {report.summary()} in your inputs. Use "
                "only people and figures from the context or a tool result, or "
                "flag it for the brief instead."
            )
            _audit(
                f"Grounding: {'refused' if self.mode == 'enforce' else 'would refuse'} {tool}",
                {
                    "surface": self.surface, "tool": tool, "mode": self.mode,
                    "names_bad": report.names_bad[:10],
                    "figures_bad": report.figures_bad[:10],
                    "text_preview": prose[:200],
                },
                private=self.private,
            )
            return reason if self.mode == "enforce" else None
        except Exception:
            logger.exception("grounding: tool check failed; allowing %s", tool)
            return None

    def guard(self, handlers: dict[str, Handler]) -> dict[str, Handler]:
        return {name: self._wrap(name, fn) for name, fn in handlers.items()}

    def _wrap(self, name: str, fn: Handler) -> Handler:
        async def guarded(tool_input: dict[str, Any]) -> Any:
            reason = self.refusal(name, tool_input or {})
            if reason:
                return json.dumps({"error": reason})
            result = await fn(tool_input)
            try:
                self.add_text(f"Tool result — {name}", str(result))
            except Exception:
                logger.debug("grounding: could not record %s result", name, exc_info=True)
            return result

        return guarded

    def filter_section(self, text: str, heading: str) -> tuple[str, list[str]]:
        """Drop the ungrounded lines of the ``**heading:**`` section (through
        the next bold header or ``---``). Returns (text, held-back lines)."""
        if self.mode != "enforce":
            return text, []
        try:
            pattern = re.compile(
                rf"(\*\*{re.escape(heading)}:?\*\*:?)(.*?)(?=\n\s*\*\*[^*\n]+:?\*\*|\n---|\Z)",
                re.DOTALL | re.IGNORECASE,
            )
            m = pattern.search(text)
            if m is None:
                return text, []
            body = m.group(2)
            report = self.check(body)
            if report.ok:
                return text, []
            bad = {c.index for c in report.bad_lines}
            lines = body.split("\n")
            held = [lines[i].strip() for i in sorted(bad) if lines[i].strip()]
            kept = "\n".join(ln for i, ln in enumerate(lines) if i not in bad)
            if not kept.strip():
                kept = " (nothing I could ground)"
            _audit(
                f"Grounding: held back {len(held)} line(s) of the {self.surface}",
                {
                    "surface": self.surface, "section": heading, "mode": self.mode,
                    "names_bad": report.names_bad[:10],
                    "figures_bad": report.figures_bad[:10],
                    "held_back": [h[:200] for h in held[:5]],
                },
                private=self.private,
            )
            return text[: m.start(2)] + kept + text[m.end(2):], held
        except Exception:
            logger.exception("grounding: section filter failed; keeping it")
            return text, []


def _person_name(person_id: Any) -> str:
    try:
        from openexecutive.people.store import get_person

        person = get_person(int(person_id))
    except Exception:
        return ""
    return person.full_name if person is not None else ""


# --------------------------------------------------------------------------- #
# Alert text
# --------------------------------------------------------------------------- #


def ungrounded(text: str, sources: list[GroundingSource]) -> list[str]:
    """The names and figures in ``text`` that no source holds ([] when all
    are grounded, and on any error)."""
    try:
        report = check_text(text or "", sources)
        return report.names_bad + report.figures_bad
    except Exception:
        logger.exception("grounding: check failed; treating as grounded")
        return []

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def ground_alert_text(
    headline: str,
    body: str,
    suggested_action: str,
    *,
    sources: list[GroundingSource],
    fallback_headline: str,
    fallback_body: str,
    surface: str,
    private: bool = False,
) -> tuple[str, str, str, bool]:
    """Ground model-written alert text against what it was written from.

    An ungrounded headline becomes ``fallback_headline``; ungrounded body
    sentences are dropped (all of them → ``fallback_body`` cut to 280); both
    fallbacks are made inert first (links masked, markup and mentions
    stripped), because they are raw event text; an
    ungrounded suggested action is dropped. Returns ``(headline, body,
    suggested_action, changed)``. Fails open."""
    mode = grounding_mode()
    if mode == "off":
        return headline, body, suggested_action, False
    try:
        idx = _Index(sources, [])
        new_headline, new_body, new_action = headline, body, suggested_action
        bad: list[str] = []
        head_report = check_text(headline, sources, index=idx)
        if not head_report.ok and fallback_headline.strip():
            bad += head_report.names_bad + head_report.figures_bad
            # The fallback is raw event text (inbound mail, a feed): quote it
            # inert, never verbatim, since broadcasts post it as written.
            new_headline = _plain(fallback_headline)[:100]
        sentences = [s for s in _SENTENCE_SPLIT.split(body.strip()) if s]
        kept: list[str] = []
        for sentence in sentences:
            r = check_text(sentence, sources, index=idx)
            if r.ok:
                kept.append(sentence)
            else:
                bad += r.names_bad + r.figures_bad
        if len(kept) != len(sentences):
            new_body = " ".join(kept) if kept else _plain(fallback_body)[:280]
        action_report = check_text(suggested_action, sources, index=idx)
        if not action_report.ok:
            bad += action_report.names_bad + action_report.figures_bad
            new_action = ""
        changed = (new_headline, new_body, new_action) != (headline, body, suggested_action)
        if bad:
            _audit(
                f"Grounding: {'rewrote' if mode == 'enforce' else 'would rewrite'} {surface} text",
                {
                    "surface": surface, "mode": mode, "items": _dedupe(bad)[:10],
                    "headline_preview": headline[:160],
                },
                private=private,
            )
        if mode != "enforce":
            return headline, body, suggested_action, False
        return new_headline, new_body, new_action, changed
    except Exception:
        logger.exception("grounding: alert text check failed; keeping it")
        return headline, body, suggested_action, False


__all__ = [
    "HELD_BACK_ALL",
    "HELD_BACK_NOTE",
    "OUTWARD_FIELDS",
    "SOURCES_HEADER",
    "BriefGrounding",
    "GroundingReport",
    "GroundingScope",
    "GroundingSource",
    "check_text",
    "context_sources",
    "extract_figures",
    "extract_names",
    "ground_alert_text",
    "ground_brief",
    "org_sources",
    "profile_sources",
    "roster",
    "sources_from_context",
    "sources_from_text",
    "ungrounded",
]
