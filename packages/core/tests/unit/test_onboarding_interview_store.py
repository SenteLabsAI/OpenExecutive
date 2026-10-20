"""The stored copy of a setup interview: round trip, TTL, cap, deletion."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from openexecutive.api.routes.onboarding import InterviewSession
from openexecutive.onboarding import interview_store as store
from openexecutive.onboarding.interview import CompanyDraft, Turn


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "interviews.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    return path


def _session() -> InterviewSession:
    draft = CompanyDraft.model_validate(
        {
            "profile": {"name": "Northwind Tools", "industry": "Industrial supply"},
            "people": [{"full_name": "Dana Reyes", "role": "CEO", "is_principal": True}],
            "departments": [{"title": "Operations", "head_person_name": "Dana Reyes"}],
            "summary": "A supplier.",
        }
    )
    return InterviewSession(
        transcript=[Turn(role="user", text="We sell tools."), Turn(role="assistant", text="Who runs ops?")],
        questions_asked=1,
        draft=draft,
        last_hint="A name.",
    )


def test_round_trip_keeps_every_field() -> None:
    store.save("s1", _session())
    back = store.load("s1")
    assert back is not None
    assert [t.text for t in back.transcript] == ["We sell tools.", "Who runs ops?"]
    assert back.questions_asked == 1
    assert back.last_hint == "A name."
    assert back.saved is False
    assert back.draft is not None
    assert back.draft.profile.name == "Northwind Tools"
    assert back.draft.people[0].is_principal is True
    assert back.draft.departments[0].authority_level.value == "propose_only"


def test_missing_and_deleted_rows_load_as_none() -> None:
    assert store.load("nope") is None
    store.save("s1", _session())
    store.delete("s1")
    assert store.load("s1") is None


def test_rows_past_the_ttl_are_not_served(monkeypatch: pytest.MonkeyPatch) -> None:
    store.save("old", _session())
    later = time.time() + store.TTL_SECONDS + 1
    monkeypatch.setattr(store.time, "time", lambda: later)
    assert store.load("old") is None


def test_the_cap_keeps_the_newest_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "MAX_ROWS", 3)
    clock = [time.time()]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    for i in range(5):
        clock[0] += 1
        store.save(f"s{i}", InterviewSession())
    assert store.load("s0") is None
    assert store.load("s1") is None
    assert store.load("s4") is not None


def test_an_unwritable_database_is_only_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "DB_PATH", Path("/nonexistent/dir/x.db"))
    store.save("s1", _session())
    assert store.load("s1") is None
    store.delete("s1")
