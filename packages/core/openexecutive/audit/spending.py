"""This month's AI spending, the owner's monthly limit, and the budget pause.

Spend is the usage log's estimate (``AuditLogger.estimated_spend``, the same
figure as ``usage_summary``'s: charges a provider reported, the rest at list
price — ``audit.pricing``) from the first of the month in the user's time
zone. The limit is ``workspace_settings.monthly_budget_usd``, set by the
owner (``PUT /workspace``); no limit is the default.

Once this month's spend reaches the limit, ``enforce_budget`` holds
background work with the existing pause switch (``scheduler.pause``), marked
``paused_by = BUDGET_PAUSED_BY``: briefs, research, alert reviews, nudges,
the Gmail reader and workflow timers wait; chat and DMs keep working, as for
any pause. It lifts that pause — and only that one, compare-and-set — once
spend is under the limit again: on the first of the next month, or as soon
as the owner raises or removes the limit. A pause someone started by hand is
never lifted here, and a hand pause already in place is left as it is.

``run_budget_watch`` applies it once a minute, so spend can pass the limit
by what one minute of work costs; ``PUT /workspace`` applies it at once when
the limit changes. Spend and limit are per company (both swap with a client
slot); the pause switch is install-wide, so with client companies the active
company's limit is the one that applies.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from openexecutive.audit.pricing import format_usd

if TYPE_CHECKING:
    from openexecutive.audit.logger import AuditLogger
    from openexecutive.scheduler.pause import PauseState

logger = logging.getLogger(__name__)

# ``paused_by`` on a pause this module started. Not an email address or
# "api", the only values a person's pause can carry.
BUDGET_PAUSED_BY = "monthly AI limit"
# From this share of the limit on, the spend reads as close to it.
NEAR_LIMIT_SHARE = 0.8
# A forecast from the first hours of a month would be noise.
_FORECAST_AFTER = timedelta(hours=72)
WATCH_INTERVAL_SECONDS = 60.0

SpendingState = Literal["no_limit", "ok", "near", "reached"]


@dataclass(frozen=True)
class Spending:
    month: str  # "2026-09", in the user's time zone
    zone: str
    since: str  # ISO start of that month, in UTC
    spent_usd: float
    forecast_usd: float | None  # month-end spend at this month's pace so far
    limit_usd: float | None
    unpriced_calls: int  # calls whose cost the estimate could not include
    state: SpendingState
    paused_for_budget: bool


def month_start(now: datetime, zone: ZoneInfo) -> datetime:
    """Midnight on the first of ``now``'s month, in ``zone``. ``fold=0``: where
    that midnight happens twice (a clock set back at 1am), the month starts
    at the first one."""
    local = now.astimezone(zone)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, fold=0)


def next_month_start(start: datetime) -> datetime:
    """Midnight on the first of the month after ``start``, same zone."""
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def _now() -> datetime:
    return datetime.now(UTC)


def is_budget_pause(state: PauseState) -> bool:
    """Whether ``state`` is a pause the monthly limit started."""
    return state.paused and state.paused_by == BUDGET_PAUSED_BY


def budget_pause_active() -> bool:
    """Whether background work is paused for the monthly limit. Never raises."""
    from openexecutive.scheduler import pause as pause_store

    try:
        state = pause_store.get_pause_state()
    except Exception:
        logger.exception("spending: could not read the pause state")
        return False
    return is_budget_pause(state)


def _state(spent: float, limit: float | None) -> SpendingState:
    if limit is None:
        return "no_limit"
    if spent >= limit:
        return "reached"
    return "near" if spent >= limit * NEAR_LIMIT_SHARE else "ok"


def current_spending(
    now: datetime | None = None, audit: AuditLogger | None = None
) -> Spending:
    """This month's spend against the limit, from ``audit`` (the default
    audit logger when omitted). Reads the usage log once."""
    from openexecutive.audit.logger import get_audit_logger
    from openexecutive.memory.workspace_settings import get_user_timezone, get_workspace

    now = now or _now()
    zone = get_user_timezone()
    start = month_start(now, zone)
    since = start.astimezone(UTC).isoformat()
    estimate, unpriced = (audit or get_audit_logger()).estimated_spend(since=since)
    spent = round(estimate, 2)
    elapsed = now - start
    forecast = None
    if elapsed >= _FORECAST_AFTER:
        whole_month = next_month_start(start) - start
        forecast = round(spent * (whole_month / elapsed), 2)
    limit = get_workspace().monthly_budget_usd
    return Spending(
        month=start.strftime("%Y-%m"),
        zone=zone.key,
        since=since,
        spent_usd=spent,
        forecast_usd=forecast,
        limit_usd=limit,
        unpriced_calls=unpriced,
        state=_state(spent, limit),
        paused_for_budget=budget_pause_active(),
    )


def _audit(summary: str, details: dict[str, object]) -> None:
    from openexecutive.audit import log_event

    event = "executive_paused" if details["action"] == "paused" else "executive_resumed"
    log_event(event, summary, actor=BUDGET_PAUSED_BY, details=details)


def enforce_budget(now: datetime | None = None) -> Literal["paused", "lifted"] | None:
    """Pause background work once this month's spend reaches the limit, and
    lift that pause once it is under it again (or the limit is gone). Returns
    what it did. Raises on a DB error (the watch loop logs it)."""
    from openexecutive.memory.workspace_settings import get_workspace
    from openexecutive.scheduler import pause as pause_store

    limit = get_workspace().monthly_budget_usd
    state = pause_store.get_pause_state()
    budget_paused = is_budget_pause(state)
    if limit is None:
        if budget_paused and pause_store.resume_if_paused_by(BUDGET_PAUSED_BY):
            _audit(
                "Background work resumed — the monthly AI limit was removed",
                {"action": "lifted", "limit_usd": None},
            )
            return "lifted"
        return None

    spending = current_spending(now)
    over = spending.spent_usd >= limit
    # pause_if_running is compare-and-set: a person who pauses after the
    # read above keeps their pause, and two checks at once pause (and audit)
    # only once.
    if (
        over
        and not state.paused
        and pause_store.pause_if_running(
            BUDGET_PAUSED_BY,
            f"This month's AI spending ({format_usd(spending.spent_usd)}) reached the "
            f"{format_usd(limit)} monthly limit",
        )
    ):
        _audit(
            f"Background work paused — this month's AI spending reached the "
            f"{format_usd(limit)} limit",
            {"action": "paused", "limit_usd": limit, "spent_usd": spending.spent_usd,
             "month": spending.month},
        )
        return "paused"
    if budget_paused and not over and pause_store.resume_if_paused_by(BUDGET_PAUSED_BY):
        _audit(
            "Background work resumed — AI spending is under the monthly limit",
            {"action": "lifted", "limit_usd": limit, "spent_usd": spending.spent_usd,
             "month": spending.month},
        )
        return "lifted"
    return None


def spending_blocks_resume() -> bool:
    """Whether resuming now would only be undone by the next check: the pause
    is the monthly limit's and this month's spend is still at or over it.
    Fails closed (True) when that cannot be read."""
    from openexecutive.memory.workspace_settings import get_workspace
    from openexecutive.scheduler import pause as pause_store

    try:
        if not is_budget_pause(pause_store.get_pause_state()):
            return False
        limit = get_workspace().monthly_budget_usd
        return limit is not None and current_spending().spent_usd >= limit
    except Exception:
        logger.exception("spending: could not tell whether the monthly limit still holds")
        return True


async def run_budget_watch(interval_seconds: float = WATCH_INTERVAL_SECONDS) -> None:
    """Apply the monthly limit every ``interval_seconds``, for the life of the
    process. Never raises out of the loop."""
    while True:
        try:
            await asyncio.to_thread(enforce_budget)
        except Exception:
            logger.exception("spending: applying the monthly AI limit failed")
        await asyncio.sleep(interval_seconds)


__all__ = [
    "BUDGET_PAUSED_BY",
    "NEAR_LIMIT_SHARE",
    "Spending",
    "budget_pause_active",
    "current_spending",
    "enforce_budget",
    "is_budget_pause",
    "month_start",
    "next_month_start",
    "run_budget_watch",
    "spending_blocks_resume",
]
