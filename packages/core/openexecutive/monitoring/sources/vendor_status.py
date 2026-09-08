"""Vendor status-page adapter.

Polls Statuspage-compatible Atom history feeds for vendors a company
depends on (AWS, Stripe, Twilio, GitHub, Cloudflare, etc.) and emits one
``Signal`` per new incident. The Atom 1.0 history feed each vendor
publishes is the same shape — entry/id, entry/title, entry/updated,
entry/link — so a single adapter handles them all.

Watchlist row shape this adapter expects:

  - ``signal_type``: ``"vendor_status"``
  - ``target``: the full Atom URL (e.g. ``https://status.aws.amazon.com/rss/all.rss``)
  - ``config_json``: optional ``{"vendor_label": "AWS"}`` — used as a
    prefix in the normalized summary; falls back to a host-derived label
    when missing so legacy rows still render readably.
  - ``trigger_json``: optional ``{"keywords": ["payment", "us-east-1"]}``
    — when present, only entries whose title/summary contain at least one
    keyword match. Otherwise every new entry surfaces.

Severity hint: defaults to ``HIGH`` for any new incident. The triage
pipeline downstream still has final say — a HIGH hint with a low-trust
score from the watchlist (configured in PR-B's tuning loop) gets damped
before the principal sees it.

Freshness (issue #90): a history feed replays its whole archive on the
first poll, so a new watch used to promote up to 100 resolved incidents
at HIGH. The fix is one change with three consequences: the ``dedup_key``
is keyed on ``(entry id, <updated>)`` rather than the entry id alone.
Statuspage bumps ``<updated>`` on every incident update, so a state
change mints a new key and re-fires while an identical poll still dedups
— and because a suppressed insert now burns only ONE state of an
incident, both #80 freshness gates become safe here:

  - the row is seeded on its first poll (``seed_on_first_poll``), with
    the incidents that are still OPEN exempted from the baseline by
    ``promote_on_baseline`` so a live outage surfaces immediately while
    the resolved archive is recorded and never promoted. Openness is read
    from the label the vendor marked up on its newest update, never
    inferred from prose, so it is only detectable on feeds that publish
    per-update status labels — see ``promote_on_baseline``;
  - ``published_at`` is parsed from ``<updated>`` / ``<pubDate>``, so the
    age gate and the future-date deferral judge vendor incidents the same
    way they judge ``rss`` and ``edgar`` entries.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree.ElementTree import Element, ParseError

import httpx

# defusedxml hardens the stdlib ElementTree parser against billion-laughs
# / quadratic-blowup / external-entity attacks. Adapters fetch arbitrary
# URLs from user-supplied watchlist rows, so the parser sees attacker-
# controlled XML in the wild — defusedxml is the right default.
from defusedxml.ElementTree import fromstring as defused_fromstring

from openexecutive.alerts.models import AlertSeverity
from openexecutive.config import get_settings
from openexecutive.monitoring.models import (
    SOURCE_KIND_VENDOR_STATUS,
    Signal,
    WatchlistItem,
)
from openexecutive.monitoring.sources._http import (
    FetchOverflowError,
    fetch_bounded,
    strip_url_query,
    validate_target_url,
)
from openexecutive.monitoring.sources.base import (
    collapse_whitespace,
    feed_text_published_at,
)

logger = logging.getLogger(__name__)

# Atom namespace. Most Statuspage instances publish a pure Atom 1.0 feed
# at /history.atom; a few publish RSS 2.0 at /rss/all.rss. We support
# both: see _parse_feed.
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
# RSS feeds that carry HTML bodies use content:encoded; ElementTree needs
# the expanded name (a bare "content:encoded" raises on the unknown prefix).
_CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"

# The documented Statuspage history feed is bounded (~25 entries) — a
# runaway count here means the feed is malformed and we should stop
# reading rather than process megabytes of garbage. Applied in the
# parsers, before each entry's body is serialised and parsed.
_MAX_ENTRIES_PER_FEED = 100

# Incident status vocabulary. Statuspage renders each update in the entry
# body as "<strong>Resolved</strong> - ...", newest update first, and uses
# one of these labels: incidents run Investigating → Identified →
# Monitoring → Resolved (with "Update" for interim notes and "Postmortem"
# after the fact), scheduled maintenances run Scheduled → In progress →
# Verifying → Completed. OPEN means "still happening" — the operator wants
# to hear about it the moment the watch is added.
_OPEN_STATUSES = frozenset({
    "investigating", "identified", "monitoring", "update",
    "scheduled", "in progress", "verifying",
})
_CLOSED_STATUSES = frozenset({"resolved", "completed", "postmortem"})

# AWS's rss/all.rss carries no per-update markup; it stamps the resolution
# into the title instead ("Service is operating normally: [RESOLVED] …").
_TITLE_MARKER_RE = re.compile(r"\[\s*(resolved|completed)\s*\]", re.IGNORECASE)
# The newest update sits at the top of the body; a few KB is far more than
# enough to find it and bounds the work regardless of body size.
_BODY_SCAN_CHARS = 8_000
# A status label is a word or two. Anything longer is not a label.
_MAX_LABEL_CHARS = 200
# Tags a vendor marks an update's status label with.
_LABEL_TAGS = frozenset({"strong", "b"})
# Evidence that the incident is over, taken from ANYWHERE in the body —
# label or prose. See _latest_status for why this direction may read prose
# and the OPEN direction may not.
_CLOSED_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_CLOSED_STATUSES)) + r")\b", re.IGNORECASE
)
# Labels to read from one body. The body is already capped; this bounds
# the list a pathological body can build.
_MAX_LABELS = 50
# Nesting depth _element_markup will serialise. Deeper than any real feed,
# and shallow enough that the walk cannot exhaust the stack.
_MAX_ELEMENT_DEPTH = 100


class VendorStatusSource:
    kind: str = SOURCE_KIND_VENDOR_STATUS
    default_poll_interval_minutes: int = 5
    # A history feed returns its whole archive on every poll, so the first
    # poll is a baseline — otherwise every incident the vendor ever had
    # fires as news when the watch is added (issue #90). Safe here ONLY
    # because the dedup key carries <updated> (see _make_dedup_key): a
    # baselined entry burns one STATE of an incident, not the incident, so
    # the next update surfaces. Incidents that are still open are exempted
    # from the baseline entirely — see promote_on_baseline.
    seed_on_first_poll: bool = True

    async def poll(
        self, item: WatchlistItem, *, db_path: Path | None = None
    ) -> list[Signal]:
        if not item.target:
            logger.warning(
                "vendor_status: watchlist %r has empty target — skipping", item.slug
            )
            return []

        # SSRF guard — see monitoring.sources._http.validate_target_url
        # for the full rationale. Watchlist rows are user-supplied, so
        # this is the only spot that turns a string into an HTTP request.
        ok, reason = validate_target_url(item.target)
        if not ok:
            logger.warning(
                "vendor_status: rejecting watchlist %r target — %s",
                item.slug, reason,
            )
            return []

        max_bytes = get_settings().external_monitor_max_fetch_bytes
        try:
            body = await fetch_bounded(item.target, max_bytes)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning(
                "vendor_status: fetch failed for %s (%s): %s",
                item.slug, item.target, exc,
            )
            return []
        except FetchOverflowError:
            logger.warning(
                "vendor_status: feed %s exceeded %d bytes — dropping tick",
                item.target, max_bytes,
            )
            return []

        try:
            # Off the event loop: parsing is CPU-bound over bytes a
            # watched URL controls, and the scan runs inside the API
            # process. A body of 8k "<" costs ~5ms per entry to tokenize —
            # half a second for a 100-entry feed — which is not a bug to
            # fix in the parser but latency no HTTP handler should eat.
            entries = await asyncio.to_thread(_parse_feed, body)
        except ParseError as exc:
            logger.warning(
                "vendor_status: feed %s failed to parse: %s", item.target, exc
            )
            return []

        if not entries:
            return []

        vendor_label = (
            item.config_json.get("vendor_label")
            or _host_label(item.target)
        )
        built = (
            self._build_signal(entry, item, vendor_label)
            for entry in entries
        )
        return [signal for signal in built if signal is not None]

    def _build_signal(
        self, entry: dict[str, str], item: WatchlistItem, vendor_label: str,
    ) -> Signal | None:
        """One parsed feed entry → one Signal, or None when it can't dedup."""
        entry_id = entry.get("id") or entry.get("link") or ""
        if not entry_id:
            # Without a stable upstream id we cannot dedup — skip rather
            # than emit a noisy hash-of-title signal that would re-fire on
            # every minor edit.
            return None
        title = collapse_whitespace(entry.get("title") or "") or "(untitled incident)"
        updated = entry.get("updated", "")
        return Signal(
            watchlist_id=item.id or 0,
            source_kind=self.kind,
            # The incident, not the state — two states of one incident
            # share it, which is what makes an operator's "show me this
            # incident" query work. Uniqueness lives on dedup_key.
            source_external_id=entry_id[:500],
            captured_at=datetime.now(UTC).isoformat(),
            published_at=feed_text_published_at(updated),
            normalized_summary=f"[{vendor_label}] {title}"[:500],
            raw_payload={
                "vendor_label": vendor_label,
                "target_url": item.target,
                "entry_id": entry_id,
                "title": title,
                "link": entry.get("link", ""),
                "updated": updated,
                "status": _latest_status(entry.get("body", ""), title),
            },
            provenance_url=entry.get("link") or item.target,
            severity_hint=AlertSeverity.HIGH,
            dedup_key=_make_dedup_key(item.slug, entry_id, updated),
        )

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        """Optional keyword filter — see module docstring."""
        keywords = item.trigger_json.get("keywords") or []
        if not keywords:
            return True
        haystack = (signal.normalized_summary or "").lower()
        return any(kw.lower() in haystack for kw in keywords)

    def promote_on_baseline(self, signal: Signal, item: WatchlistItem) -> bool:
        """First poll only: True for an incident that is still OPEN.

        The optional hook documented on ``sources.base.Source``. A history
        feed's archive is what the baseline exists to swallow, but an
        incident that is open the moment the watch is added is live news —
        the operator adding a Stripe watch during a Stripe outage must hear
        about it now, not at the vendor's next update.

        Unrecognised status counts as CLOSED, deliberately: a vendor that
        changes its feed format must not be able to promote its whole
        archive at HIGH. Nothing is lost for good — that entry is recorded
        as baseline, and the next ``<updated>`` bump mints a new dedup key
        and surfaces normally.

        That fail-closed default is the whole behaviour for a feed with no
        per-update markup, AWS's ``rss/all.rss`` among them: its resolution
        marker lives in the title, and an OPEN state there is only
        inferable from the ABSENCE of one, which is exactly the guess this
        refuses to make. So a watch on such a feed added mid-incident
        baselines that incident and reports the vendor's next update
        instead — AWS publishes one item per update, each with its own
        guid, so that update is a new entry the baseline no longer covers.
        """
        return _is_open(str(signal.raw_payload.get("status") or ""))


def _parse_feed(body: bytes) -> list[dict[str, str]]:
    """Parse a feed into {id, title, link, updated, body} dicts.

    Uses ``defusedxml.ElementTree`` to block entity-expansion attacks
    (billion-laughs, quadratic-blowup) — see the module-level import
    comment for the full rationale. Returns ``[]`` on unknown shapes
    so a vendor switching feed formats doesn't crash the scan.
    """
    root = defused_fromstring(body)
    tag = root.tag.lower()

    # Atom 1.0 feed root: <feed xmlns="http://www.w3.org/2005/Atom"> with <entry>s
    if tag.endswith("feed"):
        return _parse_atom(root)

    # RSS 2.0: <rss><channel><item>...
    if tag == "rss":
        channel = root.find("channel")
        return _parse_rss(channel) if channel is not None else []

    return []


def _parse_atom(feed: Element) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    # Capped HERE, not after parsing: each entry's body is re-serialised
    # and parsed, so a 5000-entry feed would pay that cost 5000 times
    # before the cap dropped all but the first 100.
    for entry in feed.findall(f"{_ATOM_NS}entry")[:_MAX_ENTRIES_PER_FEED]:
        entry_id = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        updated = (entry.findtext(f"{_ATOM_NS}updated") or "").strip()
        link = ""
        link_el = entry.find(f"{_ATOM_NS}link")
        if link_el is not None:
            link = (link_el.get("href") or "").strip()
        link = strip_url_query(link) if link else ""
        out.append({
            "id": entry_id, "title": title, "link": link, "updated": updated,
            "body": _entry_body(entry, (f"{_ATOM_NS}content", f"{_ATOM_NS}summary")),
        })
    return out


def _parse_rss(channel: Element) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in channel.findall("item")[:_MAX_ENTRIES_PER_FEED]:
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        link = strip_url_query(link) if link else ""
        # Prefer guid for id (stable upstream identifier); fall back to
        # the cleaned link so dedup remains stable.
        entry_id = guid or link
        out.append({
            "id": entry_id, "title": title, "link": link, "updated": pub,
            "body": _entry_body(item, ("description", _CONTENT_ENCODED)),
        })
    return out


def _entry_body(parent: Element, tags: tuple[str, ...]) -> str:
    """The entry's update log, as MARKUP, from ONE element.

    Body and status label must come from the same place: reading the text
    of ``<content>`` while taking the label from ``<summary>`` let an
    entry whose body says "Resolved" report the status of a different
    element entirely.

    Feeds carry that markup two ways. Usually ``<content type="html">``
    holds escaped HTML, so the XML parser hands us the tags as text and
    they are already markup. An xhtml-typed ``<content>`` instead holds
    real child elements, so the tags are consumed by the XML parser and
    ``itertext`` would glue the update's timestamp onto its label; those
    are re-serialised (local names only) so both shapes reach the HTML
    parser in the same form.
    """
    for tag in tags:
        el = parent.find(tag)
        if el is None:
            continue
        # Chosen on the element's TEXT: an empty <content><div/></content>
        # serialises to "<div></div>", which is truthy markup carrying no
        # content, and would shadow a <summary> holding the real log.
        if not "".join(el.itertext()).strip():
            continue
        return _element_markup(el) if len(el) else (el.text or "")
    return ""


def _element_markup(el: Element) -> str:
    """Re-serialise an element's children as simple HTML, local names only.

    Text is HTML-escaped on the way out. The XML parser has already turned
    ``&lt;strong&gt;`` in an update's prose back into literal ``<strong>``,
    and emitting that raw would hand the HTML parser a status label the
    vendor never marked up — letting an entry that merely *mentions* a
    label forge one.

    Iterative with an explicit depth cap: a body nested a few thousand
    elements deep would otherwise raise RecursionError out of the parse,
    which the pipeline catches as an adapter crash — the row would then
    fail every tick, never baseline, and emit nothing, silencing its own
    watch. Bounded by ``_BODY_SCAN_CHARS`` as well: the newest update is at
    the top, and serialising an entire archive is work no answer needs.
    """
    out: list[str] = []
    size = 0
    # (element, depth) to open, or a literal string to emit as-is.
    stack: list[tuple[Element, int] | str] = []

    def push(text: str) -> None:
        nonlocal size
        out.append(text)
        size += len(text)

    if el.text:
        push(escape(el.text))
    stack.extend(reversed([(child, 1) for child in el]))
    while stack and size < _BODY_SCAN_CHARS:
        item = stack.pop()
        if isinstance(item, str):
            push(item)
            continue
        node, depth = item
        local = (
            node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""
        )
        push(f"<{local}>")
        if node.text:
            push(escape(node.text))
        tail = escape(node.tail) if node.tail else ""
        # Children first, then this element's close tag, then its tail.
        pending: list[tuple[Element, int] | str] = [f"</{local}>"]
        if tail:
            pending.append(tail)
        if depth < _MAX_ELEMENT_DEPTH:
            children: list[tuple[Element, int] | str] = [
                (child, depth + 1) for child in node
            ]
            pending = children + pending
        stack.extend(reversed(pending))
    return "".join(out)


def _latest_status(body: str, title: str) -> str:
    """Whether this incident is still happening, lowercased, or "" .

    Read the asymmetry here first, because it is the safety property:

      * evidence that the incident is OVER counts from anywhere — the
        title's ``[RESOLVED]`` marker, a status label, or the prose of an
        update;
      * evidence that it is still OPEN counts only from a status label
        the vendor marked up as one (``<strong>Investigating</strong>``).

    Nothing here tries to work out which update is the newest, and that is
    deliberate. Four review rounds each found a different way to get that
    wrong — scanning past an unrecognised label, mining prose for status
    words, skipping an unlabelled update in the element tree, and reading
    past a block whose end tag HTML lets you omit — and every one of them
    reported a long-resolved incident as live. An "open" verdict promotes
    at HIGH and skips the first-poll baseline, so reading an older, open
    label replays exactly the archive this adapter exists to stop
    replaying. Position in the body is not a property tag soup can be
    trusted to express, so the rule no longer depends on it: ANY sign of
    closure closes, wherever it sits, and that is monotone — more evidence
    can only ever make the answer safer.

    The cost is that a live incident whose prose happens to say "resolved"
    is read as closed, so a watch added mid-outage reports it on its next
    update rather than immediately. That is the direction to be wrong in.
    """
    marker = _TITLE_MARKER_RE.search(title)
    if marker is not None:
        return marker.group(1).lower()
    reader = _read_body(body[:_BODY_SCAN_CHARS])
    statuses = [_known_status(raw) for raw in reader.labels]
    # Labels first, exactly: a label whose own text is split across inline
    # tags survives here even though the text probe below would see a
    # space through the middle of the word.
    for status in statuses:
        if status in _CLOSED_STATUSES:
            return status
    closed = _CLOSED_WORD_RE.search(reader.text)
    if closed is not None:
        return closed.group(1).lower()
    for status in statuses:
        if status in _OPEN_STATUSES:
            return status
    return ""


class _BodyReader(HTMLParser):
    """Splits an update log into its plain text and its status labels.

    A real parser, so a label inside a comment, a script, or an attribute
    value is not a label, ``<b>`` counts as much as ``<strong>``, nested
    emphasis inside a label is kept, and ``</strong >`` closes normally.
    No notion of where one update ends and the next begins — see
    ``_latest_status``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: str = ""
        self.labels: list[str] = []
        self._chunks: list[str] = []
        self._label: list[str] | None = None
        self._tag: str | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if self._label is None:
            if tag in _LABEL_TAGS and len(self.labels) < _MAX_LABELS:
                self._label, self._tag, self._depth = [], tag, 1
        elif tag == self._tag:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._label is not None and tag == self._tag:
            self._depth -= 1
            if self._depth == 0:
                self.labels.append("".join(self._label).strip())
                self._label, self._tag = None, None

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)
        if self._label is not None:
            self._label.append(data)

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        # <strong/> and friends: a self-closing label opens and closes at
        # once, so it must not swallow the rest of the body as its text.
        return

    def close(self) -> None:
        super().close()
        # Joined with a SPACE, not "": a tag boundary is a token boundary,
        # and "".join glues the words either side of one
        # ("<small>12:00 UTC</small><strong>Resolved</strong>" → "UTCResolved"),
        # which hides the word from a \b-anchored search. Adding
        # boundaries can only reveal evidence of closure, never conceal it.
        self.text = " ".join(self._chunks)


def _read_body(body: str) -> _BodyReader:
    """``_BodyReader`` over one body. Lenient: a parse that gives up keeps
    whatever it read, which can only mean less evidence, never more."""
    reader = _BodyReader()
    try:
        reader.feed(body)
        reader.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient by design
        logger.debug("vendor_status: body parse failed", exc_info=True)
        reader.text = "".join(reader._chunks)
    return reader


def _known_status(raw: str) -> str:
    """``raw`` normalised to a status we know, or "" — never a guess."""
    label = collapse_whitespace(raw).lower()
    return label if label in _OPEN_STATUSES or label in _CLOSED_STATUSES else ""


def _is_open(status: str) -> bool:
    """True only for a status we recognise AND that means "still happening".

    Fails closed on "" (unknown): see ``VendorStatusSource.promote_on_baseline``.
    """
    return status in _OPEN_STATUSES


def _host_label(url: str) -> str:
    """Best-effort vendor name from a URL host when config doesn't set one."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "vendor"
    # status.aws.amazon.com → aws; status.stripe.com → stripe
    parts = [p for p in host.split(".") if p and p != "status" and p != "www"]
    return parts[0] if parts else host or "vendor"


def _make_dedup_key(slug: str, entry_id: str, updated: str) -> str:
    """Deterministic dedup_key over (watchlist slug, entry id, ``<updated>``).

    Hashed because Atom ids can be 200+ char tag URIs; the watchlist
    slug prefix ensures two different watchlist rows pointing at the
    same vendor still produce distinct keys (so each row's own
    trigger / routing fires independently).

    ``<updated>`` is in the key on purpose (issue #90). An incident's id
    is stable across its whole lifetime, so keying on the id alone made
    every suppression permanent — one baselined or stale insert and that
    incident could never surface again, which is why this adapter used to
    sit outside both freshness gates. Keyed on the id AND the update
    stamp, a suppressed insert burns a single state: re-polling an
    unchanged feed still dedups (same id, same stamp), while an incident
    moving Investigating → Resolved mints a new key and surfaces.

    The stamp is NORMALISED before hashing (the same parse that fills
    ``published_at``), so the same instant spelled two ways — ``Z`` vs
    ``+00:00``, ``GMT`` vs ``+0000`` — is one key. A vendor changing how
    it serialises its dates must not rekey, and thereby re-alert, every
    open incident it has. An unparseable stamp is hashed as-is.

    A feed that omits ``<updated>`` degrades to a stable per-incident key
    (the pre-#90 behaviour) rather than to one that changes every poll —
    an entry with no stamp has nothing to change, and a per-poll key
    would re-alert forever. A feed that regenerates a real stamp on every
    request is the one shape this cannot defend against; nothing in the
    promotion path rate-limits a row, so such a feed re-alerts each tick.
    """
    stamp = feed_text_published_at(updated) or (updated or "").strip()
    payload = f"{slug}\x00{entry_id}\x00{stamp}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"vendor_status:{digest}"
