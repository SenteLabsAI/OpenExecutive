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
    assert len(first.new) == 2 and first.earlier_today is False

    later = right_now.record_points(
        "principal", HEADER + "\n- **Board deck** — draft is in.", now.isoformat(),
    )
    assert later.new == ["**Board deck** — draft is in."] and later.earlier_today is True
