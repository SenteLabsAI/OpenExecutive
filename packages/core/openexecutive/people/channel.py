"""Channel routing helpers for People.

Determines WHEN and WHERE to deliver a message to a Person by checking
their availability windows and preferred channel configuration.

Used by the authority gate (Phase 4) to decide whether to dispatch
a proposal now or defer it to the Person's next available window.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openexecutive.people.models import AvailabilityWindow, Person
from openexecutive.people.notifications import (
    NotificationSettings,
    in_quiet_hours,
    quiet_applies,
    quiet_hours_end,
    zone_for,
)

logger = logging.getLogger(__name__)

# How far ahead to scan for the next available window.
_SCAN_DAYS = 14
# Granularity of the scan (minutes between probe points).
_SCAN_STEP_MINUTES = 15


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """Parse "HH:MM" into (hour, minute). Raises ValueError on bad input."""
    parts = hhmm.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {hhmm!r}")
    return int(parts[0]), int(parts[1])


def _to_local(now: datetime, tz_name: str) -> datetime:
    """Convert a UTC-aware datetime to the given IANA timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("channel: unknown timezone %r — falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    return now.astimezone(tz)


def _in_window(window: AvailabilityWindow, now: datetime) -> bool:
    """Return True if `now` (UTC-aware) falls within the availability window.

    Handles cross-midnight windows (end_local < start_local). For the "morning
    half" of a cross-midnight window (now_minutes < end_minutes), the relevant
    day is *yesterday* — so we check `(weekday - 1) % 7 in window.weekdays`
    rather than `weekday in window.weekdays`. This correctly handles Friday
    22:00–Saturday 06:00 when probing at Saturday 02:00.
    """
    local = _to_local(now, window.timezone)
    # ISO weekday: Monday=0 … Sunday=6 (matching our model's convention).
    weekday = local.weekday()

    try:
        sh, sm = _parse_hhmm(window.start_local)
        eh, em = _parse_hhmm(window.end_local)
    except ValueError:
        return False

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    now_minutes = local.hour * 60 + local.minute

    if end_minutes > start_minutes:
        # Normal same-day window: e.g. 09:00-17:00
        return weekday in window.weekdays and start_minutes <= now_minutes < end_minutes

    # Cross-midnight window: e.g. 22:00-06:00
    # Evening half: local time >= start, on a listed weekday
    in_evening = weekday in window.weekdays and now_minutes >= start_minutes
    # Morning half: local time < end, and the *previous* calendar day is listed
    in_morning = (weekday - 1) % 7 in window.weekdays and now_minutes < end_minutes
    return in_evening or in_morning


def _channel_ref_for(person: Person, channel: str) -> str | None:
    """Return the channel reference string for a Person on a given channel."""
    from openexecutive.people.reach import channel_id

    return channel_id(person, channel)


def _resolve_channel(person: Person) -> tuple[str, str | None]:
    """Return (channel, channel_ref_or_None) for the person: the first channel
    in ``people.reach.reach_order`` (their pick, else where they last talked,
    then Slack, Discord, Telegram, then email). A picked channel with no id
    stays the answer, with no ref.
    """
    from openexecutive.people.reach import reach_order

    pref = person.preferred_channel
    if pref != "any":
        return pref, _channel_ref_for(person, pref)
    for ch in reach_order(person):
        ref = _channel_ref_for(person, ch)
        if ref:
            return ch, ref
    return "email", None


def _quiet_settings(person: Person, *, quiet_hours: bool, urgent: bool) -> NotificationSettings | None:
    """The person's notification settings when their quiet hours apply to this
    lookup (``quiet_hours`` asked for, quiet hours on, and not an urgent
    message they let through), else None. Fails open."""
    if not quiet_hours or person.id is None:
        return None
    return quiet_applies(person.id, urgent=urgent)


def is_within_availability(
    person: Person, *, now: datetime, quiet_hours: bool = False, urgent: bool = False
) -> bool:
    """Return True if `person` is on the clock right now (leave + windows only).

    With ``quiet_hours`` it is also False during the person's own quiet hours
    (Settings → Notifications, ``people.notifications``), unless ``urgent``
    and they let urgent things through. Only the Executive's own outreach asks
    for that (nudge routing); the outbound guard's availability check does not,
    so a message someone asked for is never held by them here.

    False when the person is on leave, or has availability windows configured and
    `now` falls outside all of them (no windows means "always on"). Unlike
    `is_reachable_now` this does NOT require a usable channel_ref and ignores the
    archived flag — it answers purely "is this a reasonable time to reach them?"
    so callers that already hold a validated channel (e.g. the outbound-DM guard,
    which is about to send on a known-good channel_ref) aren't tripped by an
    unrelated preferred-channel having no ref.
    """
    # On-leave check: compare date only, not time.
    if person.on_leave_until is not None and now.astimezone(UTC).date() <= person.on_leave_until:
        return False
    # Window check: no windows means "always on"; otherwise must be inside one.
    if person.availability and not any(_in_window(win, now) for win in person.availability):
        return False
    quiet = _quiet_settings(person, quiet_hours=quiet_hours, urgent=urgent)
    return quiet is None or not in_quiet_hours(quiet, now)


def is_reachable_now(
    person: Person, *, now: datetime, quiet_hours: bool = False, urgent: bool = False
) -> bool:
    """Return True if `person` can be reached right now.

    False when the person is archived, on leave, has no usable channel_ref for
    their preferred channel, or is outside all configured availability windows
    (no windows means "always on"). Operates on a `Person` object directly so
    callers that already hold the roster (e.g. the /today builder) avoid a
    registry round-trip per person.
    """
    if person.archived:
        return False
    if not is_within_availability(person, now=now, quiet_hours=quiet_hours, urgent=urgent):
        return False
    _, ref = _resolve_channel(person)
    return ref is not None


def next_window_for(
    person: Person, *, after: datetime, quiet_hours: bool = False, urgent: bool = False
) -> datetime | None:
    """Next window-open moment for `person`, or None (Person-object variant).

    None when no windows are configured (always-reachable, no deferral needed)
    or no window falls within the 14-day scan horizon. With ``quiet_hours``
    (see ``is_within_availability``) the moment must also be outside the
    person's quiet hours; with quiet hours but no windows that is when the
    quiet hours end.
    """
    quiet = _quiet_settings(person, quiet_hours=quiet_hours, urgent=urgent)
    if not person.availability:
        # Always on but for quiet hours: the end of them, if they are on now.
        if quiet is None or not in_quiet_hours(quiet, after):
            return None
        return quiet_hours_end(quiet, after)

    step = timedelta(minutes=_SCAN_STEP_MINUTES)
    horizon = after + timedelta(days=_SCAN_DAYS)
    probe = after + step  # start one step ahead so we don't return "now"
    zone = zone_for(quiet) if quiet is not None else None

    while probe <= horizon:
        if any(_in_window(win, probe) for win in person.availability) and (
            quiet is None or not in_quiet_hours(quiet, probe, zone=zone)
        ):
            return probe.astimezone(UTC).replace(second=0, microsecond=0)
        probe += step

    if quiet is not None:
        # Quiet hours cover every window in the horizon: the window wins,
        # rather than the message never going.
        return next_window_for(person, after=after)
    return None


def prefer_channel_for(
    person_id: int,
    *,
    now: datetime,
    quiet_hours: bool = False,
    urgent: bool = False,
) -> tuple[str, str] | None:
    """Return (channel, channel_ref) if the Person is reachable right now.

    Returns None when:
    - Person not found.
    - Person is archived.
    - Person is currently on leave.
    - No usable channel_ref for their preferred channel.
    - Current time is outside all configured availability windows.
      (If no windows are configured at all, the Person is always considered
      reachable — no windows means "always on".)
    - ``quiet_hours`` is set and it is the person's quiet hours (see
      ``is_within_availability``).
    """
    from openexecutive.people.registry import get_person

    person = get_person(person_id)
    if person is None or not is_reachable_now(
        person, now=now, quiet_hours=quiet_hours, urgent=urgent
    ):
        return None
    # Reachability guarantees a usable ref; resolve it for the caller.
    channel, ref = _resolve_channel(person)
    if not ref:
        return None
    return channel, ref


def next_available_window(
    person_id: int,
    *,
    after: datetime,
    quiet_hours: bool = False,
    urgent: bool = False,
) -> datetime | None:
    """Scan forward up to 14 days to find the next window-open moment.

    Returns the UTC datetime of the next window start, or None if:
    - Person not found.
    - No windows are configured (person is always-reachable — return None
      to signal "call now, no deferral needed").
    - No window found within the 14-day scan horizon.
    """
    from openexecutive.people.registry import get_person

    person = get_person(person_id)
    if person is None:
        return None
    return next_window_for(person, after=after, quiet_hours=quiet_hours, urgent=urgent)
