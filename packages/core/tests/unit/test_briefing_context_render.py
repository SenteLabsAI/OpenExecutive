"""render_briefing_context / _render_eod_context: the since-bounded delta view."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from openexecutive.briefing.narrative import render_briefing_context
from openexecutive.workflows.end_of_day_digest import _render_eod_context


def _proposal(alert_id: int, *, hours_ago: float, **extra: object) -> dict:
    return {
        "alert_id": alert_id,
        "headline": f"item {alert_id}",
        "created_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
        "category": "action",
        **extra,
    }


def _today() -> dict:
    return {
        "departments": [{"title": "Sales", "slug": "sales", "at_risk_count": 1, "off_track_count": 0, "awaiting_count": 0}],
        "proposals": [
            _proposal(1, hours_ago=2, why_now="SLA ends today", recommended_move="nudge"),
            _proposal(2, hours_ago=60, review_verdict="likely_stale"),
            _proposal(3, hours_ago=90),
        ],
        "people": [{"full_name": "Dana", "role": "CFO", "awaiting_count": 1, "soonest_sla_at": "x"}],
    }


def test_render_context_without_since_is_the_legacy_single_list() -> None:
    text = render_briefing_context(period_label="2026-09-11", today_data=_today(), activity=[])
    assert "PROPOSALS AWAITING DECISION:" in text
    assert "- item 1" in text and "- item 2" in text and "- item 3" in text
    assert "NEW SINCE LAST BRIEF" not in text
    assert "STILL OPEN FROM BEFORE" not in text
    assert "HANDLED OVERNIGHT" not in text


def test_render_context_splits_new_vs_carried_and_lists_handled() -> None:
    since = datetime.now(UTC) - timedelta(hours=24)
    handled = [{"kind": "routed", "summary": "Routed 'Acme renewal' to Dana", "at": "2026-09-11T07:00:00+00:00"}]
    text = render_briefing_context(
        period_label="2026-09-11", today_data=_today(), activity=[], since=since, handled=handled,
    )
    assert "NEEDS YOU — NEW SINCE LAST BRIEF:" in text
    assert "- item 1 (why now: SLA ends today) [next move: nudge]" in text
    # Older items are each named, never only counted.
    assert "STILL OPEN FROM BEFORE" in text
    assert "- item 2 (open 2d, likely stale)" in text
    assert "- item 3 (open 3d)" in text
    assert "older item(s)" not in text
    assert "HANDLED OVERNIGHT BY THE EXECUTIVE" in text
    assert "routed: Routed 'Acme renewal' to Dana" in text
    assert "PROPOSALS AWAITING DECISION:" not in text
    # Sections that are unchanged by the window still render.
    assert "DEPARTMENTS WITH RISK:" in text
    assert "PEOPLE YOU ARE WAITING ON (a workflow is paused for their reply):" in text


def test_render_context_with_since_and_nothing_new_still_reports_carried() -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    today = {"departments": [], "proposals": [_proposal(7, hours_ago=50)], "people": []}
    text = render_briefing_context(period_label="p", today_data=today, activity=[], since=since)
    assert "NEW SINCE LAST BRIEF" not in text
    assert "- item 7 (open 2d)" in text


def test_eod_context_splits_and_lists_handled() -> None:
    since = datetime.now(UTC) - timedelta(hours=24)
    handled = [{"kind": "closed", "summary": "Resolved 'Stripe incident'", "at": "2026-09-11T12:00:00+00:00"}]
    text = _render_eod_context(
        period_label="2026-09-11", today_data=_today(), activity=[
            {"kind": "dm_sent", "summary": "DM'd Dana", "at": "2026-09-11T10:00:00+00:00"}
        ], since=since, handled=handled,
    )
    assert "WHAT OE DID TODAY" in text
    assert "ALERTS I HANDLED TODAY" in text and "closed: Resolved 'Stripe incident'" in text
    assert "STILL AWAITING DECISION — NEW SINCE LAST BRIEF:" in text
    assert "- item 1" in text
    assert "STILL OPEN FROM BEFORE" in text and "- item 3 (open 3d)" in text
    legacy = _render_eod_context(period_label="p", today_data=_today(), activity=[])
    assert "STILL AWAITING DECISION:" in legacy and "STILL OPEN FROM BEFORE" not in legacy


def test_render_context_mentions_pending_watch_suggestions_once() -> None:
    since = datetime.now(UTC) - timedelta(hours=24)
    text = render_briefing_context(
        period_label="p", today_data=_today(), activity=[], since=since, pending_watch_suggestions=2,
    )
    assert "WATCH SUGGESTIONS WAITING: 2 sources" in text and "/watchlist" in text
    quiet = render_briefing_context(period_label="p", today_data=_today(), activity=[], since=since)
    assert "WATCH SUGGESTIONS" not in quiet


def test_render_context_lists_rewritten_open_items_separately_from_handled() -> None:
    since = datetime.now(UTC) - timedelta(hours=24)
    today = _today()
    reviewed = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    # A carried item the review rewrote inside the window: still open, so it
    # must surface as "what changed", never under HANDLED.
    today["proposals"].append(_proposal(
        4, hours_ago=70, review_verdict="changed",
        review_note="payouts now delayed for 7 merchants", last_reviewed_at=reviewed,
    ))
    handled = [{"kind": "closed", "summary": "Resolved 'Stripe incident' — vendor marked resolved",
                "at": "2026-09-11T07:00:00+00:00"}]
    text = render_briefing_context(
        period_label="2026-09-11", today_data=today, activity=[], since=since, handled=handled,
    )
    assert "REWRITTEN BY THE EXECUTIVE SINCE LAST BRIEF" in text
    assert "- item 4 — payouts now delayed for 7 merchants" in text
    # Listed exactly once, under REWRITTEN — not re-listed as new or carried.
    assert text.count("item 4") == 1
    assert text.index("REWRITTEN BY THE EXECUTIVE") < text.index("HANDLED OVERNIGHT BY THE EXECUTIVE")
    # The other older items are still named.
    assert "- item 2 (open 2d, likely stale)" in text and "- item 3 (open 3d)" in text


def test_eod_context_lists_rewritten_open_items() -> None:
    since = datetime.now(UTC) - timedelta(hours=24)
    today = _today()
    today["proposals"].append(_proposal(
        4, hours_ago=70, review_verdict="changed", review_note="two offers now expiring Friday",
        last_reviewed_at=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
    ))
    text = _render_eod_context(period_label="p", today_data=today, activity=[], since=since)
    assert "REWRITTEN BY THE EXECUTIVE TODAY" in text
    assert "- item 4 — two offers now expiring Friday" in text


def test_activity_label_claims_a_delta_only_when_bounded() -> None:
    """Only the standalone briefs pass `since`, and only they bound activity to
    it. The /today header gets the unbounded history rail, so calling that
    block "since last brief" made the header report old rows as overnight news.
    """
    activity = [{"at": "2026-09-20", "kind": "alert_raised", "summary": "old news"}]

    bounded = render_briefing_context(
        period_label="p", today_data=_today(), activity=activity,
        since=datetime.now(UTC) - timedelta(hours=24),
    )
    assert "OE ACTIVITY SINCE LAST BRIEF" in bounded

    unbounded = render_briefing_context(
        period_label="p", today_data=_today(), activity=activity, since=None,
    )
    assert "OE ACTIVITY SINCE LAST BRIEF" not in unbounded
    assert "RECENT OE ACTIVITY" in unbounded
    assert "NOT a delta" in unbounded


def test_older_items_carry_what_they_need_and_their_link(monkeypatch) -> None:
    from openexecutive.alerts import card_refs

    monkeypatch.setattr(card_refs, "_base", lambda: "https://app.example.com")
    since = datetime.now(UTC) - timedelta(hours=24)
    today = {"departments": [], "people": [], "proposals": [
        _proposal(4, hours_ago=80, suggested_action="Reply with your answer."),
    ]}
    for text in (
        render_briefing_context(period_label="p", today_data=today, activity=[], since=since),
        _render_eod_context(period_label="p", today_data=today, activity=[], since=since),
    ):
        assert (
            "- item 4 — needs: Reply with your answer. (open 3d) — link: "
            "https://app.example.com/?card=4"
        ) in text


def test_older_items_past_the_limit_end_with_one_more_line() -> None:
    from openexecutive.briefing.brief_state import CARRIED_LIMIT, carried_lines

    items = [_proposal(i, hours_ago=50) for i in range(CARRIED_LIMIT + 2)]
    lines = carried_lines(items, datetime.now(UTC))
    assert len(lines) == CARRIED_LIMIT + 1
    assert lines[-1] == "- (and 2 more on Home)"


def test_eod_context_names_what_the_research_found_and_the_work_done(monkeypatch) -> None:
    from openexecutive.alerts import card_refs

    monkeypatch.setattr(card_refs, "_base", lambda: "https://app.example.com")
    text = _render_eod_context(
        period_label="p", today_data={"departments": [], "people": [], "proposals": []},
        activity=[{
            "kind": "workflow_done", "summary": "Researched your market",
            "detail": "Looked at 7 updates. Sent 1 message.", "artifact_id": "run:r1",
            "at": "2026-11-07T19:30:00+00:00",
        }],
        found=["- Harbor Supply is closing — dealers need a new wholesaler"],
    )
    assert "WHAT THE RESEARCH FOUND" in text
    assert "- Harbor Supply is closing — dealers need a new wholesaler" in text
    assert (
        "Researched your market — Looked at 7 updates. Sent 1 message. — link: "
        "https://app.example.com/artifacts/run%3Ar1"
    ) in text


def test_older_open_items_alone_are_no_news_for_the_digest() -> None:
    from openexecutive.workflows.end_of_day_digest import _has_news

    since = datetime.now(UTC) - timedelta(hours=24)
    old_only = {"proposals": [_proposal(2, hours_ago=60), _proposal(3, hours_ago=90)]}
    quiet = dict(
        since=since, activity=[], handled=[], conversation=[], live_lines=[],
        owner_notes="", found=[],
    )
    assert _has_news(today_data=old_only, **quiet) is False
    assert _has_news(today_data=_today(), **quiet) is True  # item 1 is new
    assert _has_news(
        today_data=old_only, **{**quiet, "activity": [{"kind": "workflow_done"}]},
    ) is True
    assert _has_news(today_data=old_only, **{**quiet, "found": ["- a finding"]}) is True
