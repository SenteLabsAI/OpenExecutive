"""Unit tests for the proactive-DM anti-spam guard.

Covers the three controls enforced at the send-tool chokepoint by
``orchestrator.outbound_guard.check_outbound_allowed``:

1. content dedup (near-identical resend within the dedup window),
2. per-recipient rate cap (rolling window), and
3. quiet-hours / availability (on-leave + availability windows),

plus the fail-open contract and the ``recent_sends_for_channel_ref`` query the
guard reads its delivery history from.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from openexecutive.memory.episodic import (
    initialize_db,
    insert_scheduled_action,
    list_scheduled_actions,
    recent_sends_for_channel_ref,
)
from openexecutive.orchestrator import outbound_guard, schedule_tools
from openexecutive.people.models import AvailabilityWindow, Person


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    return db_path


class _Settings:
    """Stub mirroring the four outbound_* settings the guard reads."""

    def __init__(
        self,
        *,
        max_per: int = 5,
        rate_min: int = 60,
        dedup_min: int = 360,
        respect_quiet_hours: bool = False,
    ) -> None:
        self.outbound_max_per_recipient_per_window = max_per
        self.outbound_rate_window_minutes = rate_min
        self.outbound_dedup_window_minutes = dedup_min
        self.outbound_respect_quiet_hours = respect_quiet_hours


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: _Settings) -> None:
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: settings)


def _record_done(channel: str, channel_ref: str, text: str) -> None:
    """Insert a done send row exactly as _record_send_to_activity would."""
    insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(),
        channel=channel,
        channel_ref=channel_ref,
        intent_text=text[:160],
        status="done",
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_normalize_collapses_whitespace_and_casefolds() -> None:
    assert outbound_guard._normalize("  Hello   World\n") == "hello world"
    assert outbound_guard._normalize("HELLO world") == outbound_guard._normalize("hello   WORLD")


def test_parse_iso_assumes_utc_for_naive() -> None:
    aware = outbound_guard._parse_iso("2026-06-03T12:00:00")
    assert aware is not None and aware.tzinfo is not None
    assert outbound_guard._parse_iso("not-a-date") is None


# --------------------------------------------------------------------------- #
# recent_sends_for_channel_ref
# --------------------------------------------------------------------------- #
def test_recent_sends_returns_only_matching_done_rows_newest_first() -> None:
    _record_done("slack_dm", "U1", "first")
    _record_done("slack_dm", "U1", "second")
    _record_done("slack_dm", "U2", "other recipient")  # different ref
    # A pending row to the same ref must not appear (only status='done').
    insert_scheduled_action(
        run_at=datetime.now(UTC).isoformat(),
        channel="slack_dm",
        channel_ref="U1",
        intent_text="pending one",
        status="pending",
    )
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    rows = recent_sends_for_channel_ref("slack_dm", "U1", since)
    texts = [t for _, t in rows]
    assert texts == ["second", "first"]  # newest first


def test_recent_sends_empty_when_db_absent(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert recent_sends_for_channel_ref("slack_dm", "U1", "2000-01-01T00:00:00", missing) == []


# --------------------------------------------------------------------------- #
# Content dedup
# --------------------------------------------------------------------------- #
def test_dedup_suppresses_near_identical_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings())
    _record_done("slack_dm", "U1", "Board prep — please review the deck")
    # Same content, different case/whitespace => suppressed.
    reason = outbound_guard.check_outbound_allowed(
        "slack_dm", "U1", "board prep —   please review the   deck"
    )
    assert reason is not None and "near-identical" in reason


def test_dedup_allows_distinct_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings())
    _record_done("slack_dm", "U1", "Board prep — review the deck")
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", "Totally different ask") is None


def test_dedup_compares_only_first_160_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    # _record_send_to_activity stores intent_text[:160]; the guard normalizes the
    # candidate to the same 160-char prefix, so two messages that agree on the
    # first 160 chars but diverge after are duplicates, while a divergence inside
    # the first 160 is not.
    _use_settings(monkeypatch, _Settings())
    prefix = "a" * 160
    _record_done("slack_dm", "U1", prefix + " ORIGINAL TAIL")
    # Same 160-char prefix, different tail -> suppressed.
    same = outbound_guard.check_outbound_allowed("slack_dm", "U1", prefix + " DIFFERENT TAIL")
    assert same is not None and "near-identical" in same
    # Difference within the first 160 chars -> allowed.
    diverged = "a" * 159 + "b" + " tail"
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", diverged) is None


def test_dedup_matches_despite_whitespace_in_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the stored value is raw text[:160]; if the candidate is
    # normalized-then-clipped while the stored side is clipped-then-normalized,
    # leading/trailing whitespace in the 160-char window desyncs the lengths and
    # a true duplicate slips through. A message whose 160-prefix ends on a space
    # exercises that path.
    _use_settings(monkeypatch, _Settings())
    text = "   Board update: " + "x " * 80  # 160-char slice ends mid "x " run
    _record_done("slack_dm", "U1", text)
    reason = outbound_guard.check_outbound_allowed("slack_dm", "U1", text)
    assert reason is not None and "near-identical" in reason


def test_dedup_ignores_rows_outside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # dedup window 360 min; evaluate 10h in the future so the just-written row
    # (created_at ~ real now) is older than the window and must be ignored.
    _use_settings(monkeypatch, _Settings(dedup_min=360, rate_min=60, max_per=5))
    _record_done("slack_dm", "U1", "stale duplicate")
    future = datetime.now(UTC) + timedelta(hours=10)
    assert (
        outbound_guard.check_outbound_allowed("slack_dm", "U1", "stale duplicate", now=future)
        is None
    )


# --------------------------------------------------------------------------- #
# Per-recipient rate cap
# --------------------------------------------------------------------------- #
def test_rate_cap_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(max_per=5, dedup_min=360, rate_min=60))
    # 4 prior sends (distinct text so dedup doesn't fire) -> 5th allowed.
    for i in range(4):
        _record_done("telegram", "123", f"update number {i}")
    assert outbound_guard.check_outbound_allowed("telegram", "123", "a fresh update") is None
    # A 5th prior send -> now at the cap -> next one suppressed.
    _record_done("telegram", "123", "update number 4")
    reason = outbound_guard.check_outbound_allowed("telegram", "123", "one too many")
    assert reason is not None and "rate cap" in reason


# --------------------------------------------------------------------------- #
# Quiet hours / availability
# --------------------------------------------------------------------------- #
def _patch_person(monkeypatch: pytest.MonkeyPatch, person: Person) -> None:
    # find_person_by_channel_ref calls the finder with (ref, db_path), so the
    # stub must accept the optional db_path positional too.
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_slack_id",
        lambda _ref, _db=None: person,
    )


def test_quiet_hours_allows_inside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=True))
    person = Person(
        full_name="CFO",
        slack_user_id="U1",
        availability=[
            AvailabilityWindow(
                weekdays=[0, 1, 2, 3, 4, 5, 6], start_local="09:00", end_local="17:00", timezone="UTC"
            )
        ],
    )
    _patch_person(monkeypatch, person)
    noon = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)  # inside 09:00–17:00
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", "hi", now=noon) is None


def test_quiet_hours_suppresses_outside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=True))
    person = Person(
        full_name="CFO",
        slack_user_id="U1",
        availability=[
            AvailabilityWindow(
                weekdays=[0, 1, 2, 3, 4, 5, 6], start_local="09:00", end_local="17:00", timezone="UTC"
            )
        ],
    )
    _patch_person(monkeypatch, person)
    night = datetime(2026, 6, 3, 22, 0, tzinfo=UTC)  # outside the window
    reason = outbound_guard.check_outbound_allowed("slack_dm", "U1", "hi", now=night)
    assert reason is not None and "availability" in reason


def test_on_leave_person_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=True))
    person = Person(
        full_name="CFO", slack_user_id="U1", on_leave_until=date(2030, 1, 1)
    )
    _patch_person(monkeypatch, person)
    reason = outbound_guard.check_outbound_allowed(
        "slack_dm", "U1", "hi", now=datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    )
    assert reason is not None and "on leave" in reason


def test_on_leave_with_windows_reports_leave_not_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: next_window_for ignores on_leave_until, so a person who is BOTH
    # on leave and has availability windows must still get the leave-aware reason
    # (not a misleading "next window tomorrow" that would loop on suppression).
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=True))
    person = Person(
        full_name="CFO",
        slack_user_id="U1",
        on_leave_until=date(2030, 1, 1),
        availability=[
            AvailabilityWindow(
                weekdays=[0, 1, 2, 3, 4, 5, 6], start_local="09:00", end_local="17:00", timezone="UTC"
            )
        ],
    )
    _patch_person(monkeypatch, person)
    # now is inside the window hours but the person is on leave until 2030.
    reason = outbound_guard.check_outbound_allowed(
        "slack_dm", "U1", "hi", now=datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    )
    assert reason is not None and "on leave" in reason
    assert "next open window" not in reason


def test_quiet_hours_disabled_skips_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=False))
    person = Person(full_name="CFO", slack_user_id="U1", on_leave_until=date(2030, 1, 1))
    _patch_person(monkeypatch, person)
    # respect_quiet_hours False -> on-leave is not consulted.
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", "hi") is None


# --------------------------------------------------------------------------- #
# Fail-open
# --------------------------------------------------------------------------- #
def test_fail_open_when_recent_sends_lookup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=False))

    def _boom(*_a: object, **_k: object) -> list:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "openexecutive.memory.episodic.recent_sends_for_channel_ref", _boom
    )
    # Guard must allow the send rather than crash the handler.
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", "hi") is None


def test_fail_open_when_person_lookup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_settings(monkeypatch, _Settings(respect_quiet_hours=True))

    def _boom(*_a: object, **_k: object) -> Person:
        raise RuntimeError("people store down")

    monkeypatch.setattr("openexecutive.people.store.find_person_by_slack_id", _boom)
    assert outbound_guard.check_outbound_allowed("slack_dm", "U1", "hi") is None


# --------------------------------------------------------------------------- #
# Handler wiring: a suppressed send must short-circuit before the network call
# and must not write a 'done' activity row.
# --------------------------------------------------------------------------- #
def test_slack_handler_suppresses_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    class _S:
        slack_bot_token = "xoxb-test"

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _S())
    monkeypatch.setattr(
        # On the roster, so the suppression under test is the anti-spam guard
        # rather than the roster gate that now precedes it.
        "openexecutive.people.store.find_person_by_slack_id", lambda _ref: object()
    )
    monkeypatch.setattr(
        "openexecutive.orchestrator.outbound_guard.check_outbound_allowed",
        lambda *_a, **_k: "duplicate suppressed",
    )
    post = AsyncMock(return_value={"ok": True, "ts": "1.0"})

    class _FakeClient:
        def __init__(self, token: str) -> None:
            self.token = token

        chat_postMessage = post

    with patch("slack_sdk.web.async_client.AsyncWebClient", _FakeClient):
        result = asyncio.run(
            schedule_tools.handle_send_slack_dm({"user_id": "U123", "text": "huddle?"})
        )

    payload = json.loads(result)
    assert payload["status"] == "suppressed"
    assert payload["reason"] == "duplicate suppressed"
    post.assert_not_awaited()  # the real send never happened
    # No done activity row written for a suppressed attempt.
    assert list_scheduled_actions(status="done", limit=100) == []


def test_telegram_handler_suppresses_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    class _S:
        telegram_bot_token = "tg-test"

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _S())
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_telegram_chat_id", lambda _ref: object()
    )
    monkeypatch.setattr(
        "openexecutive.orchestrator.outbound_guard.check_outbound_allowed",
        lambda *_a, **_k: "rate cap reached",
    )
    send = AsyncMock(return_value="42")
    with patch("openexecutive.integrations.telegram_bot.send_message", new=send):
        result = asyncio.run(
            schedule_tools.handle_send_telegram_message({"chat_id": 55, "text": "ping"})
        )
    assert json.loads(result)["status"] == "suppressed"
    send.assert_not_awaited()
    assert list_scheduled_actions(status="done", limit=100) == []


def test_discord_handler_suppresses_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    class _S:
        discord_bot_token = "discord-test"

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _S())
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_discord_id", lambda _ref: object()
    )
    monkeypatch.setattr(
        "openexecutive.orchestrator.outbound_guard.check_outbound_allowed",
        lambda *_a, **_k: "recipient is on leave",
    )
    send = AsyncMock(return_value=None)
    with patch("openexecutive.integrations.discord_bot.send_dm", new=send):
        result = asyncio.run(
            schedule_tools.handle_send_discord_dm(
                {"discord_user_id": "1234567890", "text": "ping"}
            )
        )
    assert json.loads(result)["status"] == "suppressed"
    send.assert_not_awaited()
    assert list_scheduled_actions(status="done", limit=100) == []
