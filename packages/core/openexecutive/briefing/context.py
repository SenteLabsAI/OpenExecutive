"""Compact open-alert digest for the chat user turn.

The ``/today`` page shows a synthesized "What's going on" narrative whose items
(e.g. "Gulf Coast Port Cyberattack") are drawn from the open alerts queue — the
same rows that render as proposal cards below it. But ``/chat`` never saw that
data, so when the principal clicked a briefing item (or just typed its name) the
Executive had no record of it and couldn't discuss it.

This renders the current open alerts into a compact ``<briefing>`` block that the
chat route injects into the **user turn** (never a cached system block, so prompt
caching is unaffected). It mirrors how ``/today`` builds proposals
(`api/routes/today.py`): company-wide, live ``unread`` rows (inside TTL,
not snoozed — see ``alerts.lifecycle.list_live_alerts``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Cap each alert body so a wordy proposal can't blow the context budget; the
# headline + suggested_action carry the gist and the user can open the card for
# the full text.
_BODY_SNIPPET_CHARS = 200

# Cap how many open alerts the digest carries. The board rarely holds this many
# unread items at once; the ceiling just bounds the per-turn token cost.
_MAX_ALERTS = 30

# How far the trusted set reaches: the whole board `/today` renders as cards.
# Every one of them is Discuss-able, so trusting only the rows the digest prints
# would refuse an ack on a card the principal is looking at. Imported rather
# than restated so the two surfaces cannot drift apart.

# Recently-closed items the digest names so the Executive can recognise work
# the principal already settled. Without these, a card acked from the briefing
# page a few minutes ago is simply absent from chat: asked to "record that as
# fixed", the Executive cannot tell an already-handled item from one it has
# never heard of, and sends the principal back to the page to click Dismiss on
# a card that is already gone.
#
# Bounded by both a count and an age. `alerts` has no closed-at column, so the
# window is on `created_at` — an old alert closed today will not appear. That
# is the conservative direction: the block can omit a handled item, never
# invent one. The window matches the ACTION alert TTL (`alerts.lifecycle`
# `_ttl_settings` → 14 days): an action item may sit unread that long, so a
# shorter window would miss most of the lifetime during which the principal can
# clear one. `_MAX_HANDLED` bounds each status's query and then the merged list,
# so the block is at most that many lines however the statuses split. The merge
# is ordered by `created_at`, not by when the row was closed — there is no
# closed-at column — so on a busy day the cap can drop a just-dismissed older
# alert in favour of newer ones.
_MAX_HANDLED = 8
_HANDLED_WINDOW = timedelta(days=14)
_HANDLED_STATUSES = frozenset({"ack", "dismissed", "resolved"})


def _one_line(value: str | None) -> str:
    """Collapse a field to a single line of single-spaced text.

    EVERY interpolated field must go through this. Alert headlines, suggested
    actions, tags and review notes all originate in inbound email and chat, so
    they are attacker-controlled; a newline in any of them lets the sender
    forge an extra line in this block. That matters because a line starting
    `[N]` is the source `ack_alert` is told to trust, so a forged line is a
    forged instruction to clear somebody else's alert. Only `body` was being
    stripped.
    """
    return " ".join((value or "").split())


def format_open_alerts_for_prompt(
    db_path: Path | None = None,
    limit: int = _MAX_ALERTS,
    trusted_ids: list[int] | None = None,
) -> str:
    """Render current open (unread) alerts as a compact digest, or ``""`` when none.

    One line per alert::

        [<id>] (<category>) <headline> — <body snippet> | suggested: ... | tags: ...

    Company-wide and scoped to ``status="unread"``, mirroring ``/today``'s
    proposal build. ``category`` (``action``/``monitoring``) comes from
    :func:`openexecutive.briefing.ranking.score_and_categorize` so the Executive
    can tell an item awaiting a decision from a passive monitoring signal.

    ``trusted_ids``, when given, is filled with every LIVE alert id — including
    ones past the render cap, which is a token budget and not a trust boundary.
    This is what `ack_alert` accepts: `/today` shows up to ``BOARD_LIMIT`` cards and all of them are
    Discuss-able, so trusting only the printed subset refuses an ack on a card
    the principal is looking at.

    What the trust boundary does and does not do: it confines the model to ids
    the SERVER derived from the live board, so an id invented by the model, or
    quoted out of an alert body for a row that is closed, snoozed, expired or
    nonexistent, is refused. It does NOT make the model immune to being talked
    into acking the wrong LIVE card — one alert's attacker-controlled body can
    still argue for clearing another id that is genuinely on the board. Scoping
    trust to the single item under discussion would need the Discuss primer id
    carried server-side; it is not, so do not read this control as more than it
    is.

    Pure synchronous SQLite read — wrap in ``asyncio.to_thread`` at the call
    site. Never raises: any failure logs and returns ``""`` so a chat turn is
    never blocked by an alerts-store hiccup.
    """
    from openexecutive.alerts.lifecycle import BOARD_LIMIT, list_live_alerts
    from openexecutive.briefing.ranking import score_and_categorize

    now = datetime.now(UTC)
    try:
        # One read, two consumers. We fetch the whole live board (up to
        # `BOARD_LIMIT`, the cap `/today` renders as cards) because that is
        # what `ack_alert` must accept; we then render only the first `limit`
        # of it. Fetching past `limit` also tells a full board from a truncated
        # one — without that the header claimed the list was everything when it
        # was the most recent `limit` of many more (#136, second symptom).
        live = list_live_alerts(
            limit=max(BOARD_LIMIT, limit + 1), db_path=db_path
        )
    except Exception:
        logger.exception("briefing_context.list_alerts_failed")
        return ""

    if trusted_ids is not None:
        # Clamped to BOARD_LIMIT, never to `limit`. `limit` only decides how
        # much gets printed; letting it size the trusted set would mean a
        # caller passing limit=500 could trust ids past any board `/today`
        # renders — widening the ack surface through what is meant to be a
        # token budget.
        trusted_ids.extend(
            a.id for a in live[:BOARD_LIMIT] if a.id is not None
        )

    # The board caps BOTH halves. `limit` can only narrow what is printed, never
    # widen it past the cards `/today` actually renders: the header tells the
    # model "the principal sees these as cards", which would be false for any
    # row beyond the board.
    render_cap = min(limit, BOARD_LIMIT)
    truncated = len(live) > render_cap
    alerts = live[:render_cap]

    lines: list[str] = []
    for alert in alerts:
        _score, category, _reason = score_and_categorize(alert)
        body = _one_line(alert.body)
        if len(body) > _BODY_SNIPPET_CHARS:
            body = body[:_BODY_SNIPPET_CHARS].rstrip() + "…"
        line = f"[{alert.id}] ({category}) {_one_line(alert.headline)}"
        if body:
            line += f" — {body}"
        if alert.suggested_action:
            line += f" | suggested: {_one_line(alert.suggested_action)}"
        if alert.topic_tags:
            tags = ", ".join(_one_line(t) for t in alert.topic_tags)
            line += f" | tags: {tags}"
        if alert.review_verdict:
            review = f" | review: {_one_line(alert.review_verdict)}"
            if alert.review_note:
                review += f" — {_one_line(alert.review_note)}"
            if alert.recommended_move and alert.recommended_move != "none":
                review += f" | next move: {_one_line(alert.recommended_move)}"
            line += review
        if alert.occurrence_count > 1:
            line += f" | seen x{alert.occurrence_count}"
        lines.append(line)

    if not lines:
        # An empty board is exactly when the handled block matters most: the
        # principal just cleared everything and is still talking about it.
        try:
            return _handled_block(db_path, now)
        except Exception:
            logger.exception("briefing_context.handled_block_failed")
            return ""

    header = (
        "Open items currently on the briefing board — the principal sees these "
        "as cards and as the 'What's going on' summary on /today. Each line is "
        "[alert_id] (category) headline — details. When the user asks about one "
        "of these by name, this is what they mean."
    )
    if truncated:
        header += (
            f" NOTE: this is only the {len(lines)} most recent open items, not "
            "the complete board — there are more. Do not describe this list as "
            "everything that is open; say it is the most recent slice and point "
            "the principal at the briefing page for the rest."
        )
    out = header + "\n" + "\n".join(lines)
    # The tail is strictly additive: a failure building it must never discard
    # the open board we already rendered (this function promises never to
    # raise, and `_handled_block` reaches the store).
    try:
        handled = _handled_block(db_path, now)
    except Exception:
        logger.exception("briefing_context.handled_block_failed")
        handled = ""
    return out + "\n\n" + handled if handled else out


def _handled_block(db_path: Path | None, now: datetime) -> str:
    """Recently closed items, so the Executive knows what is already settled.

    These ids are deliberately NOT added to the session's trusted set: the
    rows are closed, so there is nothing to ack, and widening the ack surface
    is the opposite of what this block is for.
    """
    from openexecutive.alerts.lifecycle import parse_aware
    from openexecutive.alerts.store import list_alerts

    # One query per closed status, with the filter pushed into SQL. Pulling a
    # mixed page and dropping the open rows afterwards would let a board with
    # many open alerts starve the closed ones out of the page entirely — the
    # same starvation `list_alerts` documents for `exclude_source`.
    rows = []
    try:
        for status in sorted(_HANDLED_STATUSES):
            rows.extend(
                list_alerts(status=status, limit=_MAX_HANDLED, db_path=db_path)
            )
    except Exception:
        logger.exception("briefing_context.handled_lookup_failed")
        return ""

    # Each per-status page is newest-first; the merge is not, so re-sort before
    # capping or the cap would favour whichever status sorts first by name.
    fresh = [
        alert for alert in rows
        if (created := parse_aware(alert.created_at)) is not None
        and now - created <= _HANDLED_WINDOW
    ]
    fresh.sort(key=lambda a: a.created_at, reverse=True)

    lines = [
        f"[{alert.id}] ({_one_line(alert.status)}) {_one_line(alert.headline)}"
        for alert in fresh[:_MAX_HANDLED]
    ]
    if not lines:
        return ""
    return (
        "Already handled — these came off the board recently (the principal "
        "approved, dismissed or you resolved them). They are NOT open. If the "
        "user refers to one, say it is already cleared; never send them to the "
        "briefing page to dismiss it again, and never ack it — these ids are "
        "not acceptable to `ack_alert` and it will refuse them.\n"
        + "\n".join(lines)
    )


def render_and_trust(session: object, *, db_path: Path | None = None) -> str:
    """Render the digest and record the live board on ``session``.

    What is recorded is the whole live board (up to ``BOARD_LIMIT``), not only
    the ids this block printed — `/today` renders more cards than the digest
    lists and every one of them is Discuss-able, so trusting only the printed
    subset refuses an ack on a card the principal is looking at.

    The single place both entry points go through — the web chat route and the
    channel adapters — so the block the model is shown and the set `ack_alert`
    will accept can never drift apart. `ack_alert` refuses anything outside
    that set, so a caller that renders the block without recording its ids
    leaves the tool unusable, and one that records without rendering hands the
    model an ack surface it was never shown.

    Never raises: a digest failure must not take down a chat turn. On failure
    the trusted set is emptied rather than left stale, so a turn that could not
    be shown the board cannot ack anything from it either.
    """
    trusted: list[int] = []
    try:
        block = format_open_alerts_for_prompt(db_path=db_path, trusted_ids=trusted)
    except Exception:
        logger.exception("briefing_context.render_and_trust_failed")
        trusted = []
        block = ""
    if session is not None:
        try:
            session.trusted_alert_ids = set(trusted)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("briefing_context.trust_record_failed")
    return block


__all__ = ["format_open_alerts_for_prompt", "render_and_trust"]
