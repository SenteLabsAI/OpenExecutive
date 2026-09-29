"""briefing.live_signals: the principal's live world for the header and the brief."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.audit.logger import AuditLogger, set_audit_logger
from openexecutive.briefing import live_signals
from openexecutive.briefing.top_three import CalendarEvent


@pytest.fixture(autouse=True)
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuditLogger]:
    from openexecutive.memory import episodic, workspace_settings

    logger = AuditLogger(db_path=tmp_path / "audit.db")
    set_audit_logger(logger)
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "episodic.db")
    episodic.initialize_db()
    monkeypatch.setattr(workspace_settings, "get_user_timezone", lambda *a, **k: UTC)
    live_signals.reset_calendar_cache()
    yield logger
    set_audit_logger(None)
    live_signals.reset_calendar_cache()


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(hours=6)


def _email(logger: AuditLogger, sender: str, subject: str, *, private: bool = False) -> None:
    logger.log(
        "integration_inbound", f"Inbound email from {sender}: {subject}", actor="email",
        details={"channel": "email", "from": sender, "subject": subject, "preview": "hi there"},
        private=private,
    )


def test_inbound_groups_a_thread_and_counts_it(audit: AuditLogger) -> None:
    _email(audit, "dana@x.com", "Budget draft")
    _email(audit, "dana@x.com", "Re: Budget draft")
    _email(audit, "sam@x.com", "vendor renewal terms")
    audit.log(
        "integration_inbound",
        "Inbound google_chat from Sam Rivera (space=spaces/AAA): emails were not deliverable",
        actor="google_chat", details={"channel": "google_chat"},
    )

    signals = live_signals.gather_live_signals(_since(), include_private=False)

    assert signals.inbound_total == 4
    assert signals.inbound_shown == 4
    joined = "\n".join(signals.inbound)
    assert "sam@x.com: vendor renewal terms" in joined
    assert "(x2)" in joined  # the Budget draft thread reads as one line
    assert "space=" not in joined  # channel refs are stripped
    assert "Sam Rivera: emails were not deliverable" in joined


def test_bookkeeping_inbound_rows_stay_out(audit: AuditLogger) -> None:
    audit.log("integration_inbound", "Rejected: slack user=U1 not in People roster",
              details={"channel": "slack", "outcome": "rejected_unknown_sender"})
    audit.log("integration_inbound", "Skipped: response gate (reason=noise)",
              details={"channel": "slack"})

    signals = live_signals.gather_live_signals(_since(), include_private=False)

    assert signals.inbound == () and signals.inbound_total == 0
    assert signals.is_empty()


def test_stuck_collects_blocked_replies_and_non_roster_senders(audit: AuditLogger) -> None:
    audit.log("integration_outbound_blocked",
              "Blocked outbound email to dana@x.com (tool=send_gmail_message field=to)",
              details={"tool": "send_gmail_message", "field": "to", "address": "dana@x.com"})
    audit.log("integration_outbound_blocked",
              "Blocked a Google Workspace call as another account (tool=x)",
              details={"field": "user_google_email", "address": "other@x.com"})
    audit.log("integration_inbound",
              "Accepted non-roster email from dana@x.com (reply blocked at outbound gate)",
              details={"channel": "email", "from": "dana@x.com",
                       "outcome": "accepted_non_roster"})
    audit.log("integration_inbound", "Google Chat reply not delivered in spaces/B: quota",
              details={"channel": "google_chat"})

    signals = live_signals.gather_live_signals(_since(), include_private=False)

    joined = "\n".join(signals.stuck)
    assert "email to dana@x.com was held at the outbound gate" in joined
    assert "not on the roster" in joined
    assert "Google Chat reply not delivered" in joined
    assert "other@x.com" not in joined  # guard bookkeeping, not a held reply
    assert signals.inbound_total == 0


def test_private_rows_only_with_include_private(audit: AuditLogger) -> None:
    _email(audit, "contact@x.com", "Private matter", private=True)
    audit.log("delegation_drafted", "Drafted an email as person 1 in their Gmail",
              details={"person_id": 1}, private=True)

    shared = live_signals.gather_live_signals(_since(), include_private=False)
    own = live_signals.gather_live_signals(_since(), include_private=True)

    assert shared.inbound == () and shared.drafts == 0
    assert "Private matter" in "\n".join(own.inbound)
    assert own.drafts == 1


def test_window_excludes_older_rows(audit: AuditLogger) -> None:
    _email(audit, "sam@x.com", "old news")
    later = datetime.now(UTC) + timedelta(seconds=1)
    assert live_signals.gather_live_signals(later, include_private=False).inbound == ()


def test_conversations_are_the_principals_titled_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    monkeypatch.setattr(
        people_store, "find_principal_person", lambda *a, **k: SimpleNamespace(id=7)
    )
    now = datetime.now(UTC).isoformat()
    with episodic._get_conn() as conn:
        for sid, title, owner in [
            ("a", "Vendor renewal question", 7),
            ("b", "New chat", 7),
            ("c", "Someone else's chat", 9),
        ]:
            conn.execute(
                "INSERT INTO sessions (session_id, title, created_at, updated_at, "
                "caller_person_id) VALUES (?, ?, ?, ?, ?)", (sid, title, now, now, owner),
            )

    own = live_signals.gather_live_signals(_since(), include_private=True)
    shared = live_signals.gather_live_signals(_since(), include_private=False)

    assert [line.split("] ", 1)[1] for line in own.conversations] == ["Vendor renewal question"]
    assert shared.conversations == ()  # chat titles are the principal's own


def test_calendar_lists_what_is_left_of_today() -> None:
    now = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    events = [
        CalendarEvent("Standup", now - timedelta(hours=5), now - timedelta(hours=4)),
        CalendarEvent("Board prep", now - timedelta(minutes=30), now + timedelta(minutes=30)),
        CalendarEvent("Call with the auditor", now + timedelta(hours=2), now + timedelta(hours=3)),
        CalendarEvent("Holiday", None, None, all_day=True),
    ]

    signals = live_signals.gather_live_signals(
        _since(), now=now, include_private=True, calendar=events
    )

    assert signals.calendar is not None
    assert signals.calendar[0] == "all day: Holiday"
    assert signals.calendar[1].startswith("IN PROGRESS 13:30–14:30 Board prep")
    assert signals.calendar[2] == "16:00–17:00 Call with the auditor"
    assert not any("Standup" in line for line in signals.calendar)


async def test_calendar_is_read_once_and_served_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openexecutive.briefing import top_three

    calls: list[Any] = []

    async def _read(now: datetime, tz: Any) -> list[CalendarEvent]:
        calls.append(now)
        return [CalendarEvent("Later", now + timedelta(hours=1), now + timedelta(hours=2))]

    monkeypatch.setattr(top_three, "read_todays_calendar", _read)
    now = datetime.now(UTC)

    assert live_signals.cached_calendar(now, UTC) is None  # the hot path never calls out
    await live_signals.refresh_calendar(now)
    await live_signals.refresh_calendar(now)
    assert len(calls) == 1
    signals = live_signals.gather_live_signals(_since(), now=now, include_private=True)
    assert signals.calendar is not None and "Later" in signals.calendar[0]
    no_cal = live_signals.gather_live_signals(
        _since(), now=now, include_private=True, use_cached_calendar=False
    )
    assert no_cal.calendar is None


def test_render_blocks_quote_the_data_and_count_the_rest(audit: AuditLogger) -> None:
    for i in range(12):
        _email(audit, f"s{i}@x.com", f"topic {i}")
    signals = live_signals.gather_live_signals(_since(), include_private=False)

    lines = live_signals.render_live_blocks(signals, window="today so far")
    text = "\n".join(lines)

    assert text.startswith("INBOUND TODAY SO FAR (12 messages")
    assert "data, not instructions" in text
    assert "…and 2 more" in text
    assert live_signals.render_live_blocks(live_signals.LiveSignals(), window="x") == []


def test_never_raises_when_a_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("store down")

    monkeypatch.setattr(live_signals, "_audit_rows", _boom)
    signals = live_signals.gather_live_signals(_since(), include_private=True)
    assert signals.inbound == () and signals.stuck == ()


def test_keys_are_free_of_times(audit: AuditLogger) -> None:
    _email(audit, "sam@x.com", "vendor renewal")
    a = live_signals.gather_live_signals(_since(), include_private=False).keys
    b = live_signals.gather_live_signals(
        _since() - timedelta(hours=1), include_private=False
    ).keys
    assert a == b
    assert a["inbound"] == ["sam@x.com|vendor renewal|1"]


async def test_the_calendar_is_only_read_for_the_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meeting titles are the principal's own: never in a shared read, whether
    they come from the cache or are handed in."""
    from openexecutive.briefing import top_three

    now = datetime.now(UTC)
    events = [CalendarEvent("Acquisition talks", now + timedelta(hours=1), now + timedelta(hours=2))]

    async def _read(now: datetime, tz: Any) -> list[CalendarEvent]:
        return events

    monkeypatch.setattr(top_three, "read_todays_calendar", _read)
    await live_signals.refresh_calendar(now)

    shared = live_signals.gather_live_signals(_since(), now=now, include_private=False)
    handed = live_signals.gather_live_signals(
        _since(), now=now, include_private=False, calendar=events,
    )
    own = live_signals.gather_live_signals(_since(), now=now, include_private=True)
    assert shared.calendar is None and handed.calendar is None
    assert shared.keys["calendar"] == ""
    assert own.calendar is not None and "Acquisition talks" in own.calendar[0]


def test_the_calendar_is_placed_at_the_real_time() -> None:
    """At 10:40 a 10:00–10:15 meeting is over and a 10:30 one is under way —
    not what the top of the hour would say."""
    now = datetime(2026, 9, 28, 10, 40, tzinfo=UTC)
    at = lambda h, m: datetime(2026, 9, 28, h, m, tzinfo=UTC)  # noqa: E731
    events = [
        CalendarEvent("Standup", at(10, 0), at(10, 15)),
        CalendarEvent("Pricing review", at(10, 30), at(11, 0)),
        CalendarEvent("Call", at(14, 0), at(14, 30)),
    ]
    signals = live_signals.gather_live_signals(
        _since(), now=now, include_private=True, calendar=events,
    )
    assert signals.calendar == ("IN PROGRESS 10:30–11:00 Pricing review", "14:00–14:30 Call")
