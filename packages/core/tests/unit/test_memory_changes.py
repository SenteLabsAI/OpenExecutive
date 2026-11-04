"""GET /memories/changes: each person's own memory changes since a time
(memory.changes), for the chat's "Memory updated" chip and the Memory page's
"New since your last visit"."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import memory_changes as route
from openexecutive.memory import changes, episodic, facts, history, lessons
from openexecutive.memory import worked_out as wo
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

OWNER = {"x-caller-email": "olivia@co.example"}
TEAMMATE = {"x-caller-email": "ben@co.example"}


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT"):
        monkeypatch.delenv(var, raising=False)
    people_registry.invalidate()
    yield path
    people_registry.invalidate()


@pytest.fixture
def ids() -> dict[str, int]:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@co.example")
    teammate = people_store.upsert_person(full_name="Ben Teammate", email="ben@co.example")
    people_registry.invalidate()
    return {"principal": principal, "teammate": teammate}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _since(minutes: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _remember(statement: str, *, session: str, person: int | None, role: str = "principal",
              decision: bool = False, proposed: bool = False) -> int:
    fact, _ = facts.record_fact(
        subject=statement[:30], statement=statement, source_quote=statement,
        session_id=session, recorded_by_person_id=person, recorded_by_role=role,  # type: ignore[arg-type]
        decision=decision, proposed=proposed,
    )
    return fact.id


def test_each_kind_shows_up_newest_first_for_its_own_person(client: TestClient, ids: dict[str, int]) -> None:
    owner = ids["principal"]
    since = _since()
    fact = _remember("Pine View II closes on the 14th", session="s1", person=None)
    figure = wo.add_figures(owner, [wo.Figure("Reserves held", "$330,241", "DCA statement")],
                            session_id="s1", private=False)[0]
    lesson, _ = lessons.add_lesson(owner, "Summarize what an email says instead of only saying it arrived.",
                                   quote="summarize it instead", session_id="s2", turn_row_id=None)
    assert lesson is not None
    note = history.add_notes(
        owner, [history.NewNote(kind="promised", summary="Told Kyle the scans come Friday", quote="scans come Friday")],
        source="chat_message", channel="web", conversation_ref="s1", counterpart="", subject="",
        trust="high", occurred_at=datetime.now(UTC),
    )[0]
    _remember("Ben's own fact", session="s3", person=ids["teammate"], role="teammate")

    got = client.get("/memories/changes", params={"since": since}, headers=OWNER).json()["changes"]
    assert {(c["kind"], c["id"]) for c in got} == {
        ("fact", fact), ("worked_out", figure), ("lesson", lesson["id"]), ("note", note),
    }
    assert all(c["can_undo"] for c in got)
    assert [c["created_at"] for c in got] == sorted((c["created_at"] for c in got), reverse=True)
    figure_row = next(c for c in got if c["kind"] == "worked_out")
    assert figure_row["title"] == "Reserves held: $330,241" and figure_row["source"] == "DCA statement"

    # One conversation: the lesson from s2 is left out.
    in_s1 = client.get("/memories/changes", params={"since": since, "session_id": "s1"}, headers=OWNER).json()
    assert {c["kind"] for c in in_s1["changes"]} == {"fact", "worked_out", "note"}

    # The teammate sees only their own fact.
    theirs = client.get("/memories/changes", params={"since": since}, headers=TEAMMATE).json()["changes"]
    assert [c["title"] for c in theirs] == ["Ben's own fact"]


def test_nothing_before_since_and_nothing_retired(client: TestClient, ids: dict[str, int]) -> None:
    fact = _remember("Old fact", session="s1", person=None)
    later = _since(-1)  # a minute from now
    assert client.get("/memories/changes", params={"since": later}, headers=OWNER).json() == {"changes": []}
    facts.retire_fact(fact, reason="wrong")
    assert client.get("/memories/changes", params={"since": _since()}, headers=OWNER).json() == {"changes": []}


def test_a_decision_and_a_waiting_fact(client: TestClient, ids: dict[str, int]) -> None:
    since = _since()
    _remember("We hire a second analyst in Q1", session="s1", person=None, decision=True)
    _remember("Ben's guess at headcount", session="s1", person=ids["teammate"], role="teammate", proposed=True)
    mine = client.get("/memories/changes", params={"since": since}, headers=OWNER).json()["changes"]
    assert [(c["kind"], c["can_undo"]) for c in mine] == [("decision", True)]
    theirs = client.get("/memories/changes", params={"since": since}, headers=TEAMMATE).json()["changes"]
    assert [(c["status"], c["can_undo"]) for c in theirs] == [("proposed", False)]


def test_refusals(client: TestClient, ids: dict[str, int]) -> None:
    assert client.get("/memories/changes", params={"since": _since()}).status_code == 403
    assert client.get("/memories/changes", params={"since": "yesterday"}, headers=OWNER).status_code == 422


def test_since_is_read_as_utc() -> None:
    assert changes.normalize_since("2026-11-03T21:00:00Z") == "2026-11-03T21:00:00+00:00"
    assert changes.normalize_since("2026-11-03T16:00:00-05:00") == "2026-11-03T21:00:00+00:00"
    assert changes.normalize_since("2026-11-03T21:00:00") == "2026-11-03T21:00:00+00:00"
    assert changes.normalize_since("soon") is None
