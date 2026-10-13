"""Held proactive DMs (people/held_messages.py): released when quiet hours
end, carried by the principal's brief, sent on their own to anyone else when
the briefs go out — each exactly once."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from openexecutive.memory import episodic
from openexecutive.people import held_messages as held
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.notifications import (
    NotificationSettings,
    QuietHours,
    save_notification_settings,
)
from openexecutive.scheduler import runner
from openexecutive.scheduler.runner import PrincipalDelivery

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


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


@pytest.fixture
def ids() -> dict[str, int]:
    return {
        "principal": people_store.upsert_person(
            full_name="Olivia Owner", is_principal=True, email="olivia@co.example"
        ),
        "teammate": people_store.upsert_person(full_name="Ben", email="ben@co.example"),
    }


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    async def fake_deliver(person: Any, text: str, *, label: str = "") -> PrincipalDelivery:
        out.append((person.id, text))
        return PrincipalDelivery(True, "slack_dm → U1", "delivered", "slack_dm")

    monkeypatch.setattr(runner, "deliver_to_person", fake_deliver)
    return out


def test_quiet_hours_messages_go_once_when_due(ids: dict[str, int], sent: list[tuple[int, str]]) -> None:
    pid = ids["teammate"]
    held.hold(pid, "First thing", source="reflection", release_at=NOW + timedelta(hours=1), now=NOW)
    held.hold(pid, "Second thing", source="alert_review", release_at=NOW + timedelta(hours=1), now=NOW)
    assert held.held_count(pid) == 2
    assert asyncio.run(held.release_due(NOW)) == 0 and sent == []
    later = NOW + timedelta(hours=2)
    assert asyncio.run(held.release_due(later)) == 1
    [(to, text)] = sent
    assert to == pid and text.startswith(f"**{held.HEADING}**")
    assert "First thing" in text and "Second thing" in text
    assert held.held_count(pid) == 0
    assert asyncio.run(held.release_due(later)) == 0 and len(sent) == 1


def test_a_failed_delivery_is_retried(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    pid = ids["teammate"]
    held.hold(pid, "Try me", source="reflection", release_at=NOW, now=NOW)

    async def failing(person: Any, text: str, *, label: str = "") -> PrincipalDelivery:
        return PrincipalDelivery(False, "no channel", "no_channel")

    monkeypatch.setattr(runner, "deliver_to_person", failing)
    assert asyncio.run(held.release_due(NOW)) == 0
    assert held.held_count(pid) == 1


def test_still_quiet_postpones(ids: dict[str, int], sent: list[tuple[int, str]]) -> None:
    pid = ids["teammate"]
    # They moved their quiet hours so it is quiet now (12:00–14:00 UTC).
    save_notification_settings(pid, NotificationSettings(
        quiet_hours=QuietHours(enabled=True, start="11:00", end="14:00"), timezone="UTC",
    ))
    held.hold(pid, "Wait", source="reflection", release_at=NOW, now=NOW)
    assert asyncio.run(held.release_due(NOW)) == 0 and sent == []
    assert asyncio.run(held.release_due(NOW.replace(hour=14))) == 1


def test_old_messages_are_dropped(ids: dict[str, int], sent: list[tuple[int, str]]) -> None:
    pid = ids["teammate"]
    held.hold(pid, "Stale", source="reflection", release_at=NOW, now=NOW - timedelta(days=8))
    assert asyncio.run(held.release_due(NOW)) == 0 and sent == []
    assert held.held_count(pid) == 0


def test_digest_waits_for_the_briefs_then_goes_to_everyone_but_the_principal(
    ids: dict[str, int], sent: list[tuple[int, str]]
) -> None:
    held.hold(ids["principal"], "For Olivia", source="reflection", release_at=None, now=NOW)
    held.hold(ids["teammate"], "For Ben", source="reflection", release_at=None, now=NOW)
    assert asyncio.run(held.release_due(NOW)) == 0
    assert held.schedule_digest_release(NOW, except_person_id=ids["principal"]) == 1
    assert asyncio.run(held.release_due(NOW)) == 1
    assert sent[0][0] == ids["teammate"] and "For Ben" in sent[0][1]
    # The principal's waits for their brief.
    assert [m.text for m in held.for_brief(ids["principal"])] == ["For Olivia"]


def test_a_digest_with_no_brief_to_carry_it_goes_on_its_own(
    ids: dict[str, int], sent: list[tuple[int, str]]
) -> None:
    held.hold(ids["principal"], "Waiting", source="reflection", release_at=None, now=NOW)
    later = NOW + held.DIGEST_FALLBACK_AFTER + timedelta(minutes=1)
    assert asyncio.run(held.release_due(later)) == 1
    assert sent[0][0] == ids["principal"]


def test_one_person_cannot_pile_up_without_end(ids: dict[str, int]) -> None:
    pid = ids["teammate"]
    for i in range(held.MAX_HELD_PER_PERSON + 5):
        held.hold(pid, f"m{i}", source="reflection", release_at=None, now=NOW)
    waiting = held.for_brief(pid)
    assert len(waiting) == held.MAX_HELD_PER_PERSON
    assert waiting[0].text == "m5"


def test_the_principals_brief_carries_their_held_messages(ids: dict[str, int]) -> None:
    held.hold(ids["principal"], "Ben replied about Q3", source="reflection", release_at=None, now=NOW)
    text, held_ids = runner._with_held_messages("principal_brief_morning", "# Morning brief\n\nAll good.")
    assert text.startswith("# Morning brief") and "Ben replied about Q3" in text
    assert held.HEADING in text and len(held_ids) == 1
    # The weekly review does not carry them.
    assert runner._with_held_messages("principal_weekly_review", "x") == ("x", [])
    asyncio.run(runner._mark_held_sent(held_ids))
    assert held.for_brief(ids["principal"]) == []
    assert runner._with_held_messages("principal_brief_eod", "x") == ("x", [])


def test_render_block_names_when_each_would_have_gone() -> None:
    msg = held.HeldMessage(1, 1, "reflection", "Hello", "2026-10-14T21:05:00+00:00", None)
    block = held.render_block([msg], zone=ZoneInfo("UTC"))
    assert block == f"**{held.HEADING}**\n\n- Wed 9:05 pm: Hello"


def test_a_claimed_row_is_not_sent_twice(ids: dict[str, int], sent: list[tuple[int, str]]) -> None:
    pid = ids["principal"]
    later = datetime.now(UTC)
    old = later - held.DIGEST_FALLBACK_AFTER - timedelta(minutes=1)
    held.hold(pid, "Once", source="reflection", release_at=None, now=old)
    # The brief claims it; a fallback release in the meantime skips it.
    text, held_ids = runner._with_held_messages("principal_brief_morning", "brief")
    assert "Once" in text and len(held_ids) == 1
    assert asyncio.run(held.release_due(later)) == 0 and sent == []
    # The brief failed: the claim is given back and the fallback sends it.
    runner._unclaim_held(held_ids)
    assert asyncio.run(held.release_due(later)) == 1 and len(sent) == 1


def test_someone_on_leave_waits_without_retrying(
    ids: dict[str, int], sent: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    pid = ids["teammate"]
    people_store.update_person(pid, on_leave_until=NOW.date() + timedelta(days=2))
    people_registry.invalidate()
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace(outbound_respect_quiet_hours=True)
    )
    held.hold(pid, "Back soon", source="reflection", release_at=NOW, now=NOW)
    assert asyncio.run(held.release_due(NOW)) == 0 and sent == []
    [row] = held._due(NOW + timedelta(days=3), None)[pid]
    assert row.release_at == datetime(2026, 10, 17, 0, 0, tzinfo=UTC).isoformat()


def test_a_failed_delivery_backs_off(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    pid = ids["teammate"]
    held.hold(pid, "Try me", source="reflection", release_at=NOW, now=NOW)

    async def failing(person: Any, text: str, *, label: str = "") -> PrincipalDelivery:
        return PrincipalDelivery(False, "send failed", "send_failed")

    monkeypatch.setattr(runner, "deliver_to_person", failing)
    asyncio.run(held.release_due(NOW))
    assert held._due(NOW + timedelta(minutes=1), None) == {}
    assert pid in held._due(NOW + timedelta(minutes=2), None)
    asyncio.run(held.release_due(NOW + timedelta(minutes=2)))
    assert held._due(NOW + timedelta(minutes=5), None) == {}
    [row] = held._due(NOW + timedelta(minutes=6), None)[pid]
    assert row.attempts == 2


def test_drops_are_audited_without_their_text(ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("openexecutive.audit.log_event", lambda et, summary, **kw: events.append((et, kw)))
    pid = ids["teammate"]
    for i in range(held.MAX_HELD_PER_PERSON + 1):
        held.hold(pid, f"secret {i}", source="reflection", release_at=None, now=NOW)
    held.hold(pid, "old secret", source="reflection", release_at=NOW, now=NOW - timedelta(days=8))
    asyncio.run(held.release_due(NOW))
    drops = [kw for et, kw in events if et == "held_messages_dropped"]
    assert [d["details"]["reason"] for d in drops][:2] == [
        f"more than {held.MAX_HELD_PER_PERSON} waiting",
        f"more than {held.MAX_HELD_PER_PERSON} waiting",
    ]
    assert any("undelivered" in d["details"]["reason"] for d in drops)
    assert all(d["private_to_person"] == pid and "secret" not in str(d) for d in drops)


def test_only_the_stale_rows_are_dropped(ids: dict[str, int], sent: list[tuple[int, str]]) -> None:
    pid = ids["teammate"]
    held.hold(pid, "Stale", source="reflection", release_at=NOW, now=NOW - timedelta(days=8))
    held.hold(pid, "Fresh", source="alert_review", release_at=NOW, now=NOW - timedelta(minutes=2))
    assert asyncio.run(held.release_due(NOW)) == 1
    [(_, text)] = sent
    assert "Fresh" in text and "Stale" not in text
    assert held.held_count(pid) == 0


def test_a_failing_backoff_does_not_stop_the_pass(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    held.hold(ids["principal"], "For Olivia", source="reflection", release_at=NOW, now=NOW)
    held.hold(ids["teammate"], "For Ben", source="reflection", release_at=NOW, now=NOW)
    got: list[int] = []

    async def half(person: Any, text: str, *, label: str = "") -> PrincipalDelivery:
        if person.id == ids["principal"]:
            return PrincipalDelivery(False, "send failed", "send_failed")
        got.append(person.id)
        return PrincipalDelivery(True, "slack_dm → U1", "delivered", "slack_dm")

    def locked(*a: Any, **k: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner, "deliver_to_person", half)
    monkeypatch.setattr(held, "_backoff", locked)
    assert asyncio.run(held.release_due(NOW)) == 1
    assert got == [ids["teammate"]]


def test_mark_sent_is_retried_after_a_delivery(
    ids: dict[str, int], sent: list[tuple[int, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = ids["teammate"]
    held.hold(pid, "Once", source="reflection", release_at=NOW, now=NOW)
    real = held.mark_sent
    calls: list[int] = []

    def flaky(*a: Any, **k: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(*a, **k)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(held, "mark_sent", flaky)
    monkeypatch.setattr(held.asyncio, "sleep", no_sleep)
    assert asyncio.run(held.release_due(NOW)) == 1
    assert len(calls) == 2 and held.held_count(pid) == 0
    # Past the claim TTL nothing is sent again.
    assert asyncio.run(held.release_due(NOW + held.CLAIM_TTL + timedelta(minutes=1))) == 0
    assert len(sent) == 1


def test_a_brief_retries_marking_its_held_messages_sent(
    ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = ids["principal"]
    held.hold(pid, "In the brief", source="reflection", release_at=None, now=NOW)
    [msg] = held.for_brief(pid)
    real = held.mark_sent
    calls: list[int] = []

    def flaky(*a: Any, **k: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(*a, **k)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(held, "mark_sent", flaky)
    monkeypatch.setattr(held.asyncio, "sleep", no_sleep)
    asyncio.run(runner._mark_held_sent([msg.id]))
    assert len(calls) == 2 and held.held_count(pid) == 0
