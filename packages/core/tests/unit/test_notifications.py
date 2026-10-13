"""Each person's notification settings (people/notifications.py): quiet hours,
digest or instant, urgent things through — and how nudge routing
(people/channel.py) and the outbound guard (outbound_guard.check_hold,
schedule_tools._hold_outbound) apply them."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from openexecutive.attunement.outcomes import (
    SOURCE_ALERT_REVIEW,
    SOURCE_FOLLOWUP,
    SOURCE_NUDGE_STALLED,
    SOURCE_REFLECTION,
    tag_proactive,
)
from openexecutive.memory import episodic
from openexecutive.orchestrator import outbound_guard, schedule_tools
from openexecutive.people import channel
from openexecutive.people import held_messages as held
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.notifications import (
    NotificationSettings,
    QuietHours,
    get_notification_settings,
    hold_for,
    in_quiet_hours,
    quiet_hours_end,
    save_notification_settings,
    urgent_send,
)

# 2026-10-14 is a Wednesday; 2026-10-17 a Saturday.
WED_2300 = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)
WED_1200 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SAT_1200 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _quiet(**kw: object) -> NotificationSettings:
    return NotificationSettings(
        quiet_hours=QuietHours(enabled=True, **kw),  # type: ignore[arg-type]
        timezone="UTC",
    )


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    yield path
    people_registry.invalidate()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def test_defaults_hold_nothing_back() -> None:
    s = get_notification_settings(42)
    assert s.quiet_hours.enabled is False
    assert s.delivery == "instant" and s.urgent_through is True
    assert hold_for(42, source=SOURCE_REFLECTION, now=WED_2300) is None


def test_settings_round_trip() -> None:
    saved = save_notification_settings(7, NotificationSettings(
        quiet_hours=QuietHours(enabled=True, start="21:30", end="06:45", days="weekdays"),
        delivery="digest", urgent_through=False, timezone="Europe/London",
    ))
    got = get_notification_settings(7)
    assert got.quiet_hours.start == "21:30" and got.quiet_hours.days == "weekdays"
    assert got.delivery == "digest" and got.urgent_through is False
    assert got.timezone == "Europe/London" and got.updated_at == saved.updated_at != ""
    # Another person keeps the defaults.
    assert get_notification_settings(8).delivery == "instant"


@pytest.mark.parametrize(
    "kw",
    [
        {"start": "22:00", "end": "22:00"},
        {"start": "24:00", "end": "07:00"},
        {"start": "7:00", "end": "08:00"},
    ],
)
def test_bad_quiet_hours_are_refused(kw: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        QuietHours(enabled=True, **kw)


def test_unknown_zone_is_refused() -> None:
    with pytest.raises(ValidationError):
        NotificationSettings(timezone="Mars/Olympus")


# --------------------------------------------------------------------------- #
# Quiet hours
# --------------------------------------------------------------------------- #

def test_quiet_hours_cross_midnight() -> None:
    s = _quiet(start="22:00", end="07:00")
    assert in_quiet_hours(s, WED_2300)
    assert in_quiet_hours(s, datetime(2026, 10, 15, 6, 59, tzinfo=UTC))
    assert not in_quiet_hours(s, datetime(2026, 10, 15, 7, 0, tzinfo=UTC))
    assert not in_quiet_hours(s, WED_1200)
    assert quiet_hours_end(s, WED_2300) == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


def test_same_day_quiet_hours() -> None:
    s = _quiet(start="12:00", end="14:00")
    assert in_quiet_hours(s, WED_1200)
    assert not in_quiet_hours(s, WED_2300)
    assert quiet_hours_end(s, WED_1200) == datetime(2026, 10, 14, 14, 0, tzinfo=UTC)


def test_weekdays_only_keeps_the_weekend_quiet() -> None:
    s = _quiet(start="22:00", end="07:00", days="weekdays")
    assert in_quiet_hours(s, SAT_1200)
    assert not in_quiet_hours(s, WED_1200)
    # Friday night runs through the weekend to Monday morning's end.
    fri = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
    assert quiet_hours_end(s, fri) == datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
    # Every day keeps Saturday noon open.
    assert not in_quiet_hours(_quiet(start="22:00", end="07:00"), SAT_1200)


def test_quiet_hours_are_read_in_the_persons_zone() -> None:
    s = NotificationSettings(
        quiet_hours=QuietHours(enabled=True, start="22:00", end="07:00"),
        timezone="America/New_York",
    )
    # 23:00 UTC is 19:00 in New York (EDT): not quiet there.
    assert not in_quiet_hours(s, WED_2300)
    # 03:00 UTC is 23:00 in New York: quiet, until 07:00 there (11:00 UTC).
    late = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    assert in_quiet_hours(s, late)
    assert quiet_hours_end(s, late) == datetime(2026, 10, 15, 11, 0, tzinfo=UTC)


def test_off_quiet_hours_have_no_end() -> None:
    assert quiet_hours_end(NotificationSettings(), WED_2300) is None


# --------------------------------------------------------------------------- #
# What is held
# --------------------------------------------------------------------------- #

def test_quiet_hours_hold_until_they_end() -> None:
    save_notification_settings(1, _quiet(start="22:00", end="07:00"))
    hold = hold_for(1, source=SOURCE_REFLECTION, now=WED_2300)
    assert hold is not None and hold.release_at == datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
    assert hold_for(1, source=SOURCE_REFLECTION, now=WED_1200) is None


def test_digest_holds_for_the_brief_except_follow_ups() -> None:
    save_notification_settings(1, NotificationSettings(delivery="digest", timezone="UTC"))
    hold = hold_for(1, source=SOURCE_ALERT_REVIEW, now=WED_1200)
    assert hold is not None and hold.release_at is None
    # A follow-up was asked for at a time: never folded into a brief.
    assert hold_for(1, source=SOURCE_FOLLOWUP, now=WED_1200) is None


def test_urgent_things_get_through_unless_turned_off() -> None:
    save_notification_settings(1, NotificationSettings(
        quiet_hours=QuietHours(enabled=True), delivery="digest", timezone="UTC",
    ))
    # An approval nudge is urgent by its source; a critical alert by the caller.
    assert hold_for(1, source=SOURCE_NUDGE_STALLED, now=WED_2300) is None
    assert hold_for(1, source=SOURCE_ALERT_REVIEW, now=WED_2300, urgent=True) is None
    save_notification_settings(1, NotificationSettings(
        quiet_hours=QuietHours(enabled=True), delivery="instant", urgent_through=False,
        timezone="UTC",
    ))
    hold = hold_for(1, source=SOURCE_NUDGE_STALLED, now=WED_2300)
    assert hold is not None and hold.release_at is not None


# --------------------------------------------------------------------------- #
# Nudge routing (people.channel)
# --------------------------------------------------------------------------- #

def test_channel_defers_nudges_past_quiet_hours_only_when_asked() -> None:
    pid = people_store.upsert_person(full_name="Ben", email="ben@co.example", slack_user_id="U1")
    save_notification_settings(pid, _quiet(start="22:00", end="07:00"))
    person = people_store.get_person(pid)
    assert person is not None
    # The outbound guard's own check (no quiet_hours) is unchanged.
    assert channel.is_within_availability(person, now=WED_2300)
    assert channel.next_window_for(person, after=WED_2300) is None
    # Nudge routing asks for them.
    assert not channel.is_within_availability(person, now=WED_2300, quiet_hours=True)
    assert channel.prefer_channel_for(pid, now=WED_2300, quiet_hours=True) is None
    assert channel.next_available_window(pid, after=WED_2300, quiet_hours=True) == datetime(
        2026, 10, 15, 7, 0, tzinfo=UTC
    )
    # Outside quiet hours nothing defers.
    assert channel.next_window_for(person, after=WED_1200, quiet_hours=True) is None
    # An approval nudge gets through.
    assert channel.prefer_channel_for(pid, now=WED_2300, quiet_hours=True, urgent=True) is not None


def test_channel_combines_windows_and_quiet_hours() -> None:
    from openexecutive.people.models import AvailabilityWindow

    pid = people_store.upsert_person(full_name="Cleo", email="cleo@co.example", slack_user_id="U2")
    people_store.set_availability(pid, [AvailabilityWindow(
        weekdays=[0, 1, 2, 3, 4], start_local="06:00", end_local="20:00", timezone="UTC",
    )])
    save_notification_settings(pid, _quiet(start="19:00", end="08:00"))
    person = people_store.get_person(pid)
    assert person is not None
    # 06:00 opens the window, but quiet hours run to 08:00.
    nxt = channel.next_window_for(person, after=WED_2300, quiet_hours=True)
    assert nxt == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The outbound guard
# --------------------------------------------------------------------------- #

def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        slack_bot_token="tok", discord_bot_token=None, telegram_bot_token=None,
        calendar_booking_enabled=False, mcp_enabled=False,
        outbound_max_per_recipient_per_window=5, outbound_rate_window_minutes=60,
        outbound_dedup_window_minutes=360, outbound_respect_quiet_hours=True,
    )


def test_check_hold_only_holds_the_executives_own_outreach() -> None:
    pid = people_store.upsert_person(full_name="Ben", email="ben@co.example", slack_user_id="U1")
    save_notification_settings(pid, NotificationSettings(delivery="digest", timezone="UTC"))
    # Nobody tagged it: someone asked for it in chat.
    assert outbound_guard.check_hold("slack_dm", "U1", now=WED_1200) is None
    with tag_proactive(SOURCE_REFLECTION):
        got = outbound_guard.check_hold("slack_dm", "U1", now=WED_1200)
        assert got is not None and got[0] == pid and got[1] == SOURCE_REFLECTION
        # Someone not on the People list is never held.
        assert outbound_guard.check_hold("slack_dm", "U-unknown", now=WED_1200) is None
        # A critical alert gets through.
        with urgent_send():
            assert outbound_guard.check_hold("slack_dm", "U1", now=WED_1200) is None


def test_message_person_holds_once_and_does_not_send() -> None:
    pid = people_store.upsert_person(
        full_name="Ben", email="ben@co.example", slack_user_id="U1", telegram_chat_id="555",
    )
    save_notification_settings(pid, NotificationSettings(delivery="digest", timezone="UTC"))
    tg_send = AsyncMock()
    settings = _settings()
    settings.telegram_bot_token = "tok"
    with (
        patch("openexecutive.config.get_settings", return_value=settings),
        patch("openexecutive.integrations.telegram_bot.send_message", tg_send),
        tag_proactive(SOURCE_REFLECTION),
    ):
        raw = asyncio.run(schedule_tools.handle_message_person({"person_id": pid, "text": "Q3 is slipping"}))
    result = json.loads(raw)
    assert result["status"] == "held"
    tg_send.assert_not_awaited()
    waiting = held.for_brief(pid)
    assert [m.text for m in waiting] == ["Q3 is slipping"] and waiting[0].source == SOURCE_REFLECTION


def test_a_message_that_cannot_be_stored_is_sent() -> None:
    pid = people_store.upsert_person(full_name="Ben", email="ben@co.example", slack_user_id="U1")
    save_notification_settings(pid, NotificationSettings(delivery="digest", timezone="UTC"))
    with (
        patch("openexecutive.config.get_settings", return_value=_settings()),
        patch("openexecutive.people.held_messages.hold", side_effect=OSError("disk full")),
        tag_proactive(SOURCE_REFLECTION),
    ):
        assert schedule_tools._hold_outbound(
            tool="send_slack_dm", channel="slack_dm", channel_ref="U1", text="hi",
        ) is None
