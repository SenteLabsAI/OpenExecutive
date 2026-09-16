"""Cross-client cockpit — practice-wide awareness over parked client slots.

The slot architecture's deliberate ceiling is that only the active client is
*live*. The cockpit gives the fractional operator simultaneous awareness
anyway, without touching that ceiling: every parked slot's ``state.db`` is a
complete SQLite snapshot sitting on disk, so we open it **read-only, in
place** and count what matters — overdue follow-ups, awaiting replies,
unread alerts. The active client is read from
the live DB instead (its slot copy is only as fresh as the last save-back).

Honesty contract: parked data is as fresh as each slot's ``saved_at``; every
card carries that stamp. One broken slot must never take down the board —
cards degrade individually (``error`` flag) and the rest render.

Engagement metadata (role, status, renewal_date, …) comes from meta.json,
which is practice-level state outside the swap — visible for every client
regardless of which one is active.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openexecutive.clients.slots import (
    _episodic_db_path,
    _slot_dir,
    get_active_client,
    list_client_slots,
)

logger = logging.getLogger(__name__)


class ClientCockpitCard(BaseModel):
    """One client's practice-level rollup for the cockpit board."""

    slug: str
    display_name: str
    is_active: bool = False
    # Engagement metadata (meta.json — always current, never swapped).
    role: str | None = None
    status: str | None = None
    renewal_date: str | None = None
    days_to_renewal: int | None = None
    primary_contact: str | None = None
    # State counts (live DB for the active client; read-only slot state.db
    # for parked ones; None when no state exists yet or the read failed).
    pending_actions: int | None = None
    overdue_actions: int | None = None
    awaiting_replies: int | None = None
    unread_alerts: int | None = None
    # Freshness + degradation.
    saved_at: str | None = None  # parked cards: when this data was captured
    has_state: bool = False
    error: bool = False


def practice_overview(settings: Any) -> list[ClientCockpitCard]:
    """One card per client slot, active first, then by display name.

    Read-only end to end: no lock is taken (parked DBs open with
    ``mode=ro``; the live DB is only SELECTed), so the cockpit can never
    block or be blocked by a switch in progress — at worst a card reads the
    instant before a save-back and is one checkpoint stale.
    """
    cards: list[ClientCockpitCard] = []
    active = get_active_client(settings)

    for summary in list_client_slots(settings):
        slug = summary["slug"]
        is_active = slug == active
        card = ClientCockpitCard(
            slug=slug,
            display_name=summary.get("display_name") or slug,
            is_active=is_active,
            saved_at=None if is_active else summary.get("saved_at"),
            has_state=bool(summary.get("has_state")) or is_active,
            role=summary.get("role"),
            status=summary.get("status"),
            renewal_date=summary.get("renewal_date"),
            primary_contact=summary.get("primary_contact"),
        )
        card.days_to_renewal = _days_until(card.renewal_date)

        try:
            db_path = (
                _episodic_db_path()
                if is_active
                else _slot_dir(settings, slug) / "state.db"
            )
            if db_path.exists():
                _fill_state_counts(card, db_path, read_only=not is_active)
        except Exception:
            logger.exception("cockpit: card for %r failed", slug)
            card.error = True
        cards.append(card)

    cards.sort(key=lambda c: (not c.is_active, c.display_name.lower()))
    return cards


def _days_until(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    try:
        target = datetime.fromisoformat(iso_date[:10]).date()
    except ValueError:
        return None
    return (target - datetime.now(UTC).date()).days


def _fill_state_counts(
    card: ClientCockpitCard, db_path: Path, *, read_only: bool
) -> None:
    """Populate the card's counters from one SQLite snapshot.

    Every query is existence-guarded — a slot saved before a schema-adding
    deploy simply reports None for that counter instead of erroring.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        now_iso = datetime.now(UTC).isoformat()

        if "scheduled_actions" in tables:
            card.pending_actions = _one(
                conn,
                "SELECT COUNT(*) FROM scheduled_actions WHERE status='pending'",
            )
            card.overdue_actions = _one(
                conn,
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE status='pending' AND run_at < ?",
                (now_iso,),
            )
            card.awaiting_replies = _one(
                conn,
                "SELECT COUNT(*) FROM scheduled_actions "
                "WHERE status='pending' AND awaiting_response_since IS NOT NULL",
            )
        if "alerts" in tables:
            card.unread_alerts = _one(
                conn, "SELECT COUNT(*) FROM alerts WHERE status='unread'"
            )
    finally:
        conn.close()


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def format_practice_for_today(settings: Any) -> list[ClientCockpitCard]:
    """The /today "Across your clients" panel: parked clients only, and only
    in multi-client mode (2+ slots). Single-client installs get an empty
    list and render nothing. Swallows its own errors — a cockpit hiccup can
    never break the morning brief.
    """
    try:
        cards = practice_overview(settings)
        if len(cards) < 2:
            return []
        return [c for c in cards if not c.is_active]
    except Exception:
        logger.exception("cockpit: today panel failed")
        return []
