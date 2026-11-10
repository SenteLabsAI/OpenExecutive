"""People: a reply time and channel suggested from how a person answers."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import reply_habits
from openexecutive.people import store as people_store

NOW = datetime(2030, 3, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


def _person(**fields: object) -> int:
    return people_store.upsert_person(full_name="Chris Donovan", email="chris@example.com", **fields)  # type: ignore[arg-type]


def _reply(db: Path, person_id: int, channel: str, hours: float, *, days_ago: float = 1) -> None:
    sent = NOW - timedelta(days=days_ago)
    with episodic._get_conn(db) as conn:
        conn.execute(
            "INSERT INTO outbound_context (created_at, channel, channel_ref, recipient_person_id, "
            "outbound_text, consumed_at, status) VALUES (?, ?, 'ref', ?, 'hi', ?, 'consumed')",
            (sent.isoformat(), channel, person_id, (sent + timedelta(hours=hours)).isoformat()),
        )


def _get(person_id: int) -> reply_habits.Person:
    person = people_store.get_person(person_id)
    assert person is not None
    return person


def test_nothing_is_suggested_from_too_few_replies(db: Path) -> None:
    pid = _person(preferred_channel="email", response_sla_hours=24)
    for _ in range(reply_habits.MIN_REPLIES - 1):
        _reply(db, pid, "slack_dm", 2)
    assert reply_habits.suggestions(_get(pid), now=NOW) == []


def test_a_faster_reply_time_and_channel_are_suggested(db: Path) -> None:
    pid = _person(preferred_channel="email", response_sla_hours=24)
    for i in range(9):
        _reply(db, pid, "slack_dm", 4 + i * 0.25, days_ago=i + 1)
    for i in range(5):
        _reply(db, pid, "email", 20, days_ago=i + 1)
    by_kind = {s.kind: s for s in reply_habits.suggestions(_get(pid), now=NOW)}
    assert by_kind["reply_time"].value == "6"
    assert by_kind["reply_time"].headline == "Usually replies in about 6 hours"
    assert by_kind["reply_time"].evidence == "You have 24h set. From their last 14 replies to me."
    assert by_kind["channel"].value == "slack"
    assert by_kind["channel"].headline == "Answers fastest on Slack"
    assert "You have Email set." in by_kind["channel"].evidence


def test_a_close_reply_time_or_the_same_channel_is_not_suggested(db: Path) -> None:
    pid = _person(preferred_channel="slack", response_sla_hours=8)
    for i in range(6):
        _reply(db, pid, "slack_dm", 5, days_ago=i + 1)
    assert reply_habits.suggestions(_get(pid), now=NOW) == []


def test_old_replies_are_not_counted(db: Path) -> None:
    pid = _person(preferred_channel="email", response_sla_hours=24)
    for _ in range(6):
        _reply(db, pid, "slack_dm", 1, days_ago=reply_habits.WINDOW_DAYS + 5)
    assert reply_habits.suggestions(_get(pid), now=NOW) == []


def test_keep_mine_hides_it_until_the_evidence_moves(db: Path) -> None:
    pid = _person(preferred_channel="slack", response_sla_hours=24)
    for i in range(6):
        _reply(db, pid, "slack_dm", 5, days_ago=i + 1)
    assert [s.kind for s in reply_habits.suggestions(_get(pid))] == ["reply_time"]
    assert reply_habits.keep_mine(_get(pid), "reply_time") is True
    assert reply_habits.suggestions(_get(pid)) == []
    assert reply_habits.keep_mine(_get(pid), "reply_time") is False  # nothing left to keep
    # Much faster now: the evidence moved, so it is offered again.
    for i in range(20):
        _reply(db, pid, "slack_dm", 1, days_ago=0.5 - i * 0.01)
    assert [s.value for s in reply_habits.suggestions(_get(pid))] == ["1"]


def test_only_the_owner_sees_or_keeps_them(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.main import create_app

    owner = people_store.upsert_person(full_name="Jordan Avery", email="jordan@example.com", is_principal=True)
    assert owner
    pid = _person(preferred_channel="email", response_sla_hours=24)
    people_store.upsert_person(full_name="Sam Lee", email="sam@example.com")
    people_registry.invalidate()
    for i in range(6):
        _reply(db, pid, "slack_dm", 2, days_ago=i + 1)
    client = TestClient(create_app())
    got = client.get(f"/people/{pid}/reply-habits")
    assert got.status_code == 200
    assert {s["kind"] for s in got.json()["suggestions"]} == {"reply_time", "channel"}
    refused = client.get(f"/people/{pid}/reply-habits", headers={"x-caller-email": "sam@example.com"})
    assert refused.status_code == 403
    assert client.post(
        f"/people/{pid}/reply-habits/keep", json={"kind": "channel"}, headers={"x-caller-email": "sam@example.com"},
    ).status_code == 403
    assert client.post(f"/people/{pid}/reply-habits/keep", json={"kind": "channel"}).status_code == 204
    assert [s["kind"] for s in client.get(f"/people/{pid}/reply-habits").json()["suggestions"]] == ["reply_time"]
    assert client.post(f"/people/{pid}/reply-habits/keep", json={"kind": "nope"}).status_code == 422


def test_keep_with_nothing_to_keep_is_409_and_an_archived_person_is_404(db: Path) -> None:
    from openexecutive.api.main import create_app

    people_store.upsert_person(full_name="Jordan Avery", email="jordan@example.com", is_principal=True)
    pid = _person(preferred_channel="email", response_sla_hours=24)
    people_registry.invalidate()
    client = TestClient(create_app())
    assert client.get(f"/people/{pid}/reply-habits").json() == {"suggestions": []}
    assert client.post(f"/people/{pid}/reply-habits/keep", json={"kind": "reply_time"}).status_code == 409
    people_store.archive_person(pid)
    people_registry.invalidate()
    assert client.get(f"/people/{pid}/reply-habits").status_code == 404
    assert client.get("/people/9999/reply-habits").status_code == 404


def test_where_they_last_talked_is_named_as_the_setting(db: Path) -> None:
    pid = _person(preferred_channel="any", response_sla_hours=4)
    for i in range(6):
        _reply(db, pid, "slack_dm", 2, days_ago=i + 1)
    (channel,) = [s for s in reply_habits.suggestions(_get(pid), now=NOW) if s.kind == "channel"]
    assert channel.evidence.startswith("You have Where they last talked set.")
