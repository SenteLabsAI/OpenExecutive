"""Unit tests for the vendor_status adapter (issue #90).

The adapter used to key its ``dedup_key`` on the incident id alone, which
made every suppression permanent and kept it outside both #80 freshness
gates — so a new watch replayed the vendor's whole resolved archive as
HIGH alerts. These tests pin the three moving parts of the fix: the key
now carries ``<updated>``, ``published_at`` is parsed from the feed, and
``promote_on_baseline`` exempts incidents that are still open from the
first-poll baseline.
"""
from __future__ import annotations

import pytest

from openexecutive.alerts.models import AlertSeverity
from openexecutive.monitoring.models import WatchlistItem
from openexecutive.monitoring.sources.base import feed_text_published_at
from openexecutive.monitoring.sources.vendor_status import (
    VendorStatusSource,
    _parse_feed,
    _is_open,
    _latest_status,
    _make_dedup_key,
)

# --------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------- #


def _make_item(
    *,
    slug: str = "vendor-stripe",
    target: str = "https://status.stripe.com/history.atom",
    config: dict | None = None,
    trigger: dict | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        id=1,
        slug=slug,
        signal_type="vendor_status",
        target=target,
        config_json=config or {},
        trigger_json=trigger or {},
    )


def _atom_entry(
    *, incident: str, title: str, updated: str, body: str,
) -> str:
    return f"""
  <entry>
    <id>tag:status.stripe.com,2005:Incident/{incident}</id>
    <published>2026-09-05T09:00:00Z</published>
    <updated>{updated}</updated>
    <link rel="alternate" type="text/html"
          href="https://status.stripe.com/incidents/{incident}?utm_source=feed"/>
    <title>{title}</title>
    <content type="html">{body}</content>
  </entry>"""


def _atom_feed(*entries: str) -> bytes:
    joined = "".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Stripe Status</title>
  <updated>2026-09-07T12:00:00Z</updated>{joined}
</feed>
""".encode()


# An incident still in progress: Statuspage lists the newest update first,
# so "Monitoring" is its current state even though "Investigating" follows.
_OPEN_BODY = (
    "&lt;p&gt;&lt;small&gt;Sep 7, 11:30 UTC&lt;/small&gt;&lt;br&gt;"
    "&lt;strong&gt;Monitoring&lt;/strong&gt; - A fix has been applied.&lt;/p&gt;"
    "&lt;p&gt;&lt;strong&gt;Investigating&lt;/strong&gt; - Looking into it.&lt;/p&gt;"
)
_RESOLVED_BODY = (
    "&lt;p&gt;&lt;strong&gt;Resolved&lt;/strong&gt; - This incident is resolved.&lt;/p&gt;"
    "&lt;p&gt;&lt;strong&gt;Investigating&lt;/strong&gt; - Looking into it.&lt;/p&gt;"
)

_OPEN_ENTRY = _atom_entry(
    incident="2001", title="Elevated API error rates",
    updated="2026-09-07T11:30:00Z", body=_OPEN_BODY,
)
_RESOLVED_ENTRY = _atom_entry(
    incident="1004", title="Dashboard latency",
    updated="2026-03-02T18:00:00Z", body=_RESOLVED_BODY,
)

_SAMPLE_ATOM = _atom_feed(_OPEN_ENTRY, _RESOLVED_ENTRY)

_SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Twilio Status</title>
    <item>
      <guid>https://status.twilio.com/incidents/xyz</guid>
      <title>SMS delivery delays</title>
      <link>https://status.twilio.com/incidents/xyz?utm_source=feed</link>
      <pubDate>Mon, 07 Sep 2026 11:30:00 +0000</pubDate>
      <description>&lt;p&gt;&lt;strong&gt;Identified&lt;/strong&gt; - Root cause found.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""

# AWS publishes one item per update and carries the resolution in the
# title instead of Statuspage's per-update markup.
_AWS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Amazon Web Services Service Status</title>
    <item>
      <guid>http://status.aws.amazon.com/#ec2-us-east-1_1757251800</guid>
      <title>Service is operating normally: [RESOLVED] Increased error rates</title>
      <link>http://status.aws.amazon.com/</link>
      <pubDate>Mon, 07 Sep 2026 12:10:00 PDT</pubDate>
      <description>Between 9:00 AM and 11:30 AM PDT we experienced errors.</description>
    </item>
  </channel>
</rss>
"""


# --------------------------------------------------------------------- #
# Status parsing
# --------------------------------------------------------------------- #


# _latest_status sees the body AFTER the XML parser has unescaped it, so
# these are the literal-markup forms of _OPEN_BODY / _RESOLVED_BODY above.
_OPEN_BODY_TEXT = (
    "<p><small>Sep 7, 11:30 UTC</small><br>"
    "<strong>Monitoring</strong> - A fix has been applied.</p>"
    "<p><strong>Investigating</strong> - Looking into it.</p>"
)
_RESOLVED_BODY_TEXT = (
    "<p><strong>Resolved</strong> - This incident is resolved.</p>"
    "<p><strong>Investigating</strong> - Looking into it.</p>"
)


@pytest.mark.parametrize(
    ("body", "title", "expected"),
    [
        (_OPEN_BODY_TEXT, "Elevated API error rates", "monitoring"),
        (_RESOLVED_BODY_TEXT, "Dashboard latency", "resolved"),
        ("<p><strong>Investigating</strong> - digging in.</p>", "x", "investigating"),
        ("<p><strong>Identified</strong> - found it.</p>", "x", "identified"),
        ("<p><strong>Scheduled</strong> - maintenance window.</p>", "x", "scheduled"),
        ("<p><strong>Completed</strong> - maintenance done.</p>", "x", "completed"),
        ("<p><strong>Postmortem</strong> - write-up.</p>", "x", "postmortem"),
        # Case and whitespace are the vendor's business, not ours.
        ("<p><STRONG> resolved </STRONG> - done.</p>", "x", "resolved"),
        # Closure counts from prose as well as from a label — an update
        # that says it is resolved closes the incident even though no
        # label says so.
        ("This incident has been fully resolved. Update: no further action.",
         "x", "resolved"),
        ("<p>Resolved - this incident has been resolved.</p>"
         "<p><strong>Investigating</strong> - looking.</p>", "x", "resolved"),
        # …but an OPEN reading needs a marked-up label. Prose alone never
        # opens an incident: "Update:" in a sentence is not a status.
        ("We were monitoring - then it recovered.", "x", ""),
        ("Sep 7, 19:07 UTC Investigating - looking into it.", "x", ""),
        # No markup anywhere: the title marker decides, and it can only
        # ever close.
        ("plain text body", "[RESOLVED] Increased error rates", "resolved"),
        ("9:16 AM PDT We are investigating: increased error rates.",
         "Service is operating normally: [RESOLVED] Increased error rates",
         "resolved"),
        # A <strong> that isn't a status label is not a status.
        ("<p><strong>Note</strong> - unrelated bold text.</p>", "x", ""),
        ("", "", ""),
    ],
)
def test_status_reads_closure_from_anywhere_and_openness_only_from_a_label(
    body: str, title: str, expected: str,
) -> None:
    assert _latest_status(body, title) == expected


@pytest.mark.parametrize(
    "body",
    [
        # An unrecognised or empty label on the newest update, with an
        # older open label and NO evidence of closure anywhere. This reads
        # as open, and that is the rule's known residual: it is why the
        # exemption does not also buy an entry past the age gate, so the
        # blast radius is an entry the feed dates inside the age window
        # rather than a years-old archive.
        "<p><strong>Fixed</strong> - all good.</p>"
        "<p><strong>Investigating</strong> - looking into it.</p>",
        "<p><strong></strong> - x.</p>"
        "<p><strong>Investigating</strong> - looking.</p>",
    ],
)
def test_known_residual_an_unreadable_newest_label_reads_as_open(body: str) -> None:
    assert _latest_status(body, "") == "investigating"


def test_a_label_past_the_scan_head_is_not_read() -> None:
    """The body is capped, so a label buried past the head cannot decide
    anything — in either direction."""
    body = "Update: we are on it. " + ("filler " * 1_200) + "<strong>Investigating</strong>"
    assert _latest_status(body, "") == ""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Position is not a property tag soup can express, so the rule no
        # longer depends on it. Each of these was a shape that made a
        # position-based rule read an OLDER, open label and report a
        # resolved incident as live — implicit <p> close, one block with
        # <br> separators, no wrapper, a block tag outside any list we
        # could keep, and a close tag swallowed by a script or a comment.
        ("<p>Resolved - this incident has been resolved."
         "<p><strong>Investigating</strong> - looking.</p>", "resolved"),
        ("<div>Sep 7 12:00 UTC - Resolved - all clear.<br/><br/>"
         "Sep 7 09:00 UTC - <strong>Investigating</strong> - looking.</div>",
         "resolved"),
        ("Sep 7 12:00 UTC Resolved - all clear.<br/>"
         "Sep 7 09:00 UTC <strong>Investigating</strong> - looking.", "resolved"),
        ("<h3>Sep 7 - Resolved - all clear.</h3>"
         "<h3><strong>Investigating</strong></h3>", "resolved"),
        ("<header>Resolved - all clear.</header>"
         "<p><strong>Investigating</strong></p>", "resolved"),
        ('<p>Resolved - all clear.<script>var s = "</p>";</script>'
         "<p><strong>Investigating</strong></p>", "resolved"),
        ("<p>Resolved - all clear.<!-- </p> -->"
         "<p><strong>Investigating</strong></p>", "resolved"),
        ("<p><em>Resolved</em> - fixed.<p><strong>Investigating</strong></p>",
         "resolved"),
        # A real parser, so a label in a comment or an attribute value is
        # not a label, <b> counts as much as <strong>, nested emphasis is
        # kept, and a spaced close tag closes.
        ("<p><b>Resolved</b> - fixed.</p>"
         "<p><strong>Investigating</strong> - looking.</p>", "resolved"),
        ('<p><a href="/x?q=<strong>Investigating</strong>">Resolved</a>'
         " - fixed.</p>", "resolved"),
        ("<p><strong>Investigating</strong > - live.</p>", "investigating"),
        ("<p><strong><em>Monitoring</em></strong> - a fix is applied.</p>",
         "monitoring"),
    ],
)
def test_closure_is_read_however_the_body_is_shaped(
    body: str, expected: str,
) -> None:
    assert _latest_status(body, "") == expected


def test_xhtml_body_reads_only_its_newest_update() -> None:
    """The shape `_entry_body` re-serialises: an xhtml <content> holds real
    elements, so the tags never reach us as text. A multi-update body is
    the case that matters — with one update, "read only the newest" is
    vacuous."""
    def _feed(newest: str) -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:status.example.com,2005:Incident/5</id>
    <updated>2026-09-07T11:30:00Z</updated>
    <link rel="alternate" href="https://status.example.com/incidents/5"/>
    <title>Elevated errors</title>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <p><small>12:00 UTC</small><br/>{newest}</p>
      <p><small>09:00 UTC</small><br/><strong>Investigating</strong> - looking.</p>
    </div></content>
  </entry>
</feed>""".encode()

    labelled = _parse_feed(_feed("<strong>Resolved</strong> - fixed."))[0]
    assert _latest_status(labelled["body"], labelled["title"]) == "resolved"

    # Newest update unlabelled: its prose still closes the incident, so it
    # cannot fall through to the older "Investigating" and promote at HIGH.
    unlabelled = _parse_feed(_feed("Resolved - this incident has been resolved."))[0]
    assert _latest_status(unlabelled["body"], unlabelled["title"]) == "resolved"


def test_label_never_comes_from_a_different_element_than_the_body() -> None:
    """Body and label must be one element's: reading text from <content>
    while taking the label from <summary> reported the status of an
    element whose text was never used."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:status.example.com,2005:Incident/6</id>
    <updated>2026-09-07T11:30:00Z</updated>
    <link rel="alternate" href="https://status.example.com/incidents/6"/>
    <title>Elevated errors</title>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <p>Jun 1 12:00 UTC Resolved - all clear.</p>
    </div></content>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <p><b>Monitoring</b> - watching.</p>
    </div></summary>
  </entry>
</feed>"""
    entry = _parse_feed(feed)[0]
    assert _latest_status(entry["body"], entry["title"]) == "resolved"


def test_is_open_fails_closed_on_unknown_status() -> None:
    """An unrecognised status must NOT count as open: a vendor changing its
    feed format must not be able to promote its whole archive at HIGH."""
    assert _is_open("investigating") is True
    assert _is_open("monitoring") is True
    assert _is_open("resolved") is False
    assert _is_open("postmortem") is False
    assert _is_open("") is False
    assert _is_open("nonsense") is False


# --------------------------------------------------------------------- #
# published_at parsing (sources.base.feed_text_published_at)
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-07T11:30:00Z", "2026-09-07T11:30:00+00:00"),
        ("2026-09-07T04:30:00-07:00", "2026-09-07T11:30:00+00:00"),
        ("Mon, 07 Sep 2026 11:30:00 +0000", "2026-09-07T11:30:00+00:00"),
        ("Mon, 07 Sep 2026 11:30:00 GMT", "2026-09-07T11:30:00+00:00"),
        # Naive values are read as UTC, like every other date path here.
        ("2026-09-07T11:30:00", "2026-09-07T11:30:00+00:00"),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_feed_text_published_at(raw: str | None, expected: str | None) -> None:
    assert feed_text_published_at(raw) == expected


# --------------------------------------------------------------------- #
# Dedup key
# --------------------------------------------------------------------- #


def test_dedup_key_changes_with_updated() -> None:
    """The whole fix: an incident id is stable for the incident's life, so
    the key must carry its update stamp or a suppression is permanent."""
    first = _make_dedup_key("vendor-stripe", "Incident/2001", "2026-09-07T09:00:00Z")
    same = _make_dedup_key("vendor-stripe", "Incident/2001", "2026-09-07T09:00:00Z")
    later = _make_dedup_key("vendor-stripe", "Incident/2001", "2026-09-07T11:30:00Z")
    other_row = _make_dedup_key("vendor-aws", "Incident/2001", "2026-09-07T09:00:00Z")

    assert first == same  # an unchanged feed still dedups
    assert first != later  # a status change surfaces again
    assert first != other_row  # rows stay independent
    assert later.startswith("vendor_status:")


def test_dedup_key_without_updated_is_stable_per_incident() -> None:
    """A feed with no <updated> degrades to a per-incident key rather than
    one that changes every poll — which would re-alert forever."""
    a = _make_dedup_key("vendor-x", "Incident/7", "")
    b = _make_dedup_key("vendor-x", "Incident/7", "")
    assert a == b


# --------------------------------------------------------------------- #
# poll()
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_poll_emits_status_published_at_and_keys(install_source_feed) -> None:
    install_source_feed("vendor_status", _SAMPLE_ATOM)
    src = VendorStatusSource()
    item = _make_item(config={"vendor_label": "Stripe"})

    signals = await src.poll(item)

    assert len(signals) == 2
    live, archived = signals
    assert live.normalized_summary == "[Stripe] Elevated API error rates"
    assert live.raw_payload["status"] == "monitoring"
    assert live.published_at == "2026-09-07T11:30:00+00:00"
    assert live.severity_hint is AlertSeverity.HIGH
    # Tracking query stripped from the click-through URL.
    assert live.provenance_url == "https://status.stripe.com/incidents/2001"
    assert archived.raw_payload["status"] == "resolved"
    assert archived.published_at == "2026-03-02T18:00:00+00:00"
    # source_external_id stays the incident, so both states of one incident
    # remain findable together; only dedup_key distinguishes them.
    assert live.source_external_id.endswith("Incident/2001")
    assert live.dedup_key != archived.dedup_key


@pytest.mark.asyncio
async def test_identical_poll_produces_identical_dedup_keys(install_source_feed) -> None:
    """Re-polling an unchanged feed must dedup — the status page returns
    the same entries every five minutes."""
    install_source_feed("vendor_status", _SAMPLE_ATOM)
    src = VendorStatusSource()
    item = _make_item()

    first = await src.poll(item)
    second = await src.poll(item)

    assert [s.dedup_key for s in first] == [s.dedup_key for s in second]


@pytest.mark.asyncio
async def test_incident_update_mints_a_new_key(install_source_feed) -> None:
    """Same incident, one status update later: a new key, so it re-fires."""
    src = VendorStatusSource()
    item = _make_item()

    install_source_feed("vendor_status", _atom_feed(_OPEN_ENTRY))
    before = (await src.poll(item))[0]

    resolved_now = _atom_entry(
        incident="2001", title="Elevated API error rates",
        updated="2026-09-07T13:05:00Z", body=_RESOLVED_BODY,
    )
    install_source_feed("vendor_status", _atom_feed(resolved_now))
    after = (await src.poll(item))[0]

    assert before.source_external_id == after.source_external_id
    assert before.dedup_key != after.dedup_key
    assert before.raw_payload["status"] == "monitoring"
    assert after.raw_payload["status"] == "resolved"


@pytest.mark.asyncio
async def test_promote_on_baseline_exempts_only_open_incidents(install_source_feed) -> None:
    """The first-poll contract: an open incident is live news, the resolved
    archive is not."""
    install_source_feed("vendor_status", _SAMPLE_ATOM)
    src = VendorStatusSource()
    item = _make_item()

    live, archived = await src.poll(item)

    assert src.seed_on_first_poll is True
    assert src.promote_on_baseline(live, item) is True
    assert src.promote_on_baseline(archived, item) is False


@pytest.mark.asyncio
async def test_unknown_status_is_baselined_on_first_poll(install_source_feed) -> None:
    """A feed whose format we don't recognise must not promote its archive.
    Nothing is lost: the next <updated> bump mints a new key."""
    unknown = _atom_entry(
        incident="9", title="Something happened",
        updated="2026-09-07T11:30:00Z", body="plain text, no markup",
    )
    install_source_feed("vendor_status", _atom_feed(unknown))
    src = VendorStatusSource()
    item = _make_item()

    signal = (await src.poll(item))[0]

    assert signal.raw_payload["status"] == ""
    assert src.promote_on_baseline(signal, item) is False


@pytest.mark.asyncio
async def test_rss_status_and_pubdate(install_source_feed) -> None:
    """Statuspage's RSS variant carries the same markup in <description>."""
    install_source_feed("vendor_status", _SAMPLE_RSS)
    src = VendorStatusSource()
    item = _make_item(target="https://status.twilio.com/history.rss")

    signal = (await src.poll(item))[0]

    assert signal.raw_payload["status"] == "identified"
    assert signal.published_at == "2026-09-07T11:30:00+00:00"
    assert src.promote_on_baseline(signal, item) is True
    # No vendor_label configured → derived from the host.
    assert signal.normalized_summary.startswith("[twilio] ")


@pytest.mark.asyncio
async def test_aws_rss_title_marker(install_source_feed) -> None:
    install_source_feed("vendor_status", _AWS_RSS)
    src = VendorStatusSource()
    item = _make_item(target="https://status.aws.amazon.com/rss/all.rss")

    signal = (await src.poll(item))[0]

    assert signal.raw_payload["status"] == "resolved"
    assert src.promote_on_baseline(signal, item) is False
    assert signal.published_at is not None


@pytest.mark.asyncio
async def test_xhtml_content_still_yields_a_status(install_source_feed) -> None:
    """An Atom entry whose <content> holds real child elements (type=xhtml)
    rather than escaped markup: findtext would return only the leading text
    and lose the label, and itertext glues the update's timestamp onto the
    label — so the text fallback must not be anchored at the start."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:status.example.com,2005:Incident/5</id>
    <updated>2026-09-07T11:30:00Z</updated>
    <link rel="alternate" href="https://status.example.com/incidents/5"/>
    <title>Partial outage</title>
    <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p><small>Sep 7, 19:07 UTC</small><br/><strong>Investigating</strong> - looking.</p></div></content>
  </entry>
</feed>
"""
    install_source_feed("vendor_status", feed)
    src = VendorStatusSource()

    signal = (await src.poll(_make_item()))[0]

    assert signal.raw_payload["status"] == "investigating"


@pytest.mark.asyncio
async def test_entry_without_id_or_link_is_skipped(install_source_feed) -> None:
    """Unchanged behaviour: no stable upstream id → no reliable dedup."""
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Anonymous incident</title>
    <updated>2026-09-07T11:30:00Z</updated>
  </entry>
</feed>
"""
    install_source_feed("vendor_status", feed)
    assert await VendorStatusSource().poll(_make_item()) == []


@pytest.mark.asyncio
async def test_keyword_trigger_still_filters_on_title(install_source_feed) -> None:
    install_source_feed("vendor_status", _SAMPLE_ATOM)
    src = VendorStatusSource()
    item = _make_item(trigger={"keywords": ["api"]})

    live, archived = await src.poll(item)

    assert src.matches_trigger(live, item) is True
    assert src.matches_trigger(archived, item) is False


def test_dedup_key_is_stable_across_date_spellings() -> None:
    """The same instant spelled two ways is ONE key. A vendor changing how
    it serialises dates must not rekey — and so re-alert — every open
    incident it has."""
    spellings = [
        "2026-09-07T19:07:00Z",
        "2026-09-07T19:07:00+00:00",
        "2026-09-07T12:07:00-07:00",
    ]
    keys = {_make_dedup_key("vendor-x", "Incident/7", raw) for raw in spellings}
    assert len(keys) == 1

    rfc822 = {
        _make_dedup_key("vendor-x", "Incident/7", raw)
        for raw in ("Mon, 07 Sep 2026 19:07:00 GMT", "Mon, 07 Sep 2026 19:07:00 +0000")
    }
    assert rfc822 == keys  # …and the two dialects agree with each other

    # An unparseable stamp is still hashed, so such a feed keeps working.
    assert _make_dedup_key("vendor-x", "Incident/7", "whenever") != keys.pop()
