"""Right now (briefing.right_now): splitting the header and the times kept."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openexecutive.briefing import narrative_cache, right_now

HEADER = (
    "**Northwind signed, so the renewal is the one thing to move today.**\n\n"
    "- **Northwind contract** — it came back signed this afternoon.\n"
    "- **Lakeside renewal** — they want a price before their 4 PM review.\n\n"
    "**Move today:** Call Lakeside before 4 PM; the hiring plan can wait."
)


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative_cache, "DB_PATH", tmp_path / "rn.db")


def test_split_reads_opener_points_and_move() -> None:
    parts = right_now.split_narrative(HEADER)
    assert parts.lead == "Northwind signed, so the renewal is the one thing to move today."
    assert parts.points == [
        "**Northwind contract** — it came back signed this afternoon.",
        "**Lakeside renewal** — they want a price before their 4 PM review.",
    ]
    assert parts.move == "Call Lakeside before 4 PM; the hiring plan can wait."


def test_split_quiet_line_is_the_opener_alone() -> None:
    parts = right_now.split_narrative("All quiet. Nothing needs you right now.")
    assert parts.lead == "All quiet. Nothing needs you right now."
    assert parts.points == [] and parts.move is None


def test_split_keeps_a_stray_line_as_a_point() -> None:
    parts = right_now.split_narrative("**Opener.**\n\nA paragraph the model added.\n- **One** — two.")
    assert parts.points == ["**One** — two.", "A paragraph the model added."]


def test_point_key_follows_the_bold_subject_across_rewording() -> None:
    a = right_now.point_key("**Lakeside renewal** — they want a price by 4.")
    b = right_now.point_key("**Lakeside Renewal:** still waiting on your price.")
    assert a == b == "lakeside renewal"
    assert right_now.point_key("**Résumé review** — due.") == "résumé review"
    assert right_now.point_key("**東京支社** — 新しい") == "東京支社"


def test_a_point_keeps_its_first_time_across_rewrites() -> None:
    now = datetime.now(UTC)
    first = (now - timedelta(minutes=30)).isoformat()
    later = now.isoformat()
    right_now.record_points("principal", HEADER, first)
    reworded = HEADER.replace("this afternoon", "an hour ago") + "\n- **Board deck** — draft is in."
    right_now.record_points("principal", reworded, later)

    times = right_now.point_times("principal", narrative_cache.local_today(now))
    assert times["northwind contract"] == first
    assert times["lakeside renewal"] == first
    assert times["board deck"] == later
    # Another reader's scope keeps its own times.
    assert right_now.point_times("person:7", narrative_cache.local_today(now)) == {}


def test_seen_is_per_scope() -> None:
    assert right_now.seen_at("principal") is None
    at = right_now.mark_seen("principal")
    assert right_now.seen_at("principal") == at
    assert right_now.seen_at("company") is None


def test_record_points_says_what_is_new_today() -> None:
    now = datetime.now(UTC)
    first = right_now.record_points("principal", HEADER, (now - timedelta(minutes=30)).isoformat())
    assert len(first.new) == 2

    later = right_now.record_points(
        "principal", HEADER + "\n- **Board deck** — draft is in.", now.isoformat(),
    )
    assert later.new == ["**Board deck** — draft is in."]


def test_point_sources_are_the_latest_headers_alone() -> None:
    now = datetime.now(UTC)
    day = narrative_cache.local_today(now)
    mail = {"kind": "mail", "message_id": "m1", "thread_id": "t1"}
    right_now.record_sources("principal", now.isoformat(), {"northwind contract": mail})
    assert right_now.point_sources("principal", day) == {"northwind contract": mail}
    # The next header replaces them: a point it no longer has opens nothing.
    card = {"kind": "alert", "alert_id": 4}
    right_now.record_sources("principal", now.isoformat(), {"lakeside renewal": card})
    assert right_now.point_sources("principal", day) == {"lakeside renewal": card}
    assert right_now.point_sources("person:7", day) == {}


def test_a_dismissed_point_stays_gone_for_the_day_however_it_is_worded() -> None:
    written = "2026-10-30T18:00:00+00:00"
    right_now.record_sources("principal", written, {"signed contracts": {"kind": "mail", "message_id": "m1"}})

    gone = right_now.dismiss("principal", "**Signed contracts**: Dana sent them.", at="2026-10-30T19:00:00+00:00")

    assert gone is not None and gone.key == "signed contracts"
    assert gone.source == {"kind": "mail", "message_id": "m1"}
    day = right_now._day_of("2026-10-30T19:00:00+00:00")
    assert day is not None
    today = right_now.dismissed("principal", day)
    assert [d.key for d in today] == ["signed contracts"]
    # Same subject, or a new subject about the same email: both gone.
    assert right_now.is_dismissed("**Signed contracts** — reworded.", None, today)
    assert right_now.is_dismissed("**Signed papers**: in.", {"kind": "mail", "message_id": "m1"}, today)
    assert not right_now.is_dismissed("**Lakeside renewal**: price.", {"kind": "mail", "message_id": "m2"}, today)
    # Another scope, or the next day, starts clean.
    assert right_now.dismissed("person:4", day) == []
    assert right_now.dismissed("principal", "2026-10-31") == []
    assert "Signed contracts: Dana sent them." in right_now.dismissed_block(today)
    assert right_now.dismissed_block([]) == ""


def test_dismissing_without_a_subject_or_source_still_works() -> None:
    gone = right_now.dismiss("principal", "**Q3 board deck**: draft unsent.")
    assert gone is not None and gone.source is None
    assert right_now.dismiss("principal", "   ") is None


def test_a_full_day_still_takes_a_point_dismissed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(right_now, "_DISMISSED_MAX", 2)
    at = "2026-10-30T19:00:00+00:00"
    assert right_now.dismiss("principal", "**One**: a.", at=at) is not None
    assert right_now.dismiss("principal", "**Two**: b.", at=at) is not None
    assert right_now.dismiss("principal", "**Three**: c.", at=at) is None
    # A double tap on one already kept is not refused.
    assert right_now.dismiss("principal", "**Two**: b.", at=at) is not None
