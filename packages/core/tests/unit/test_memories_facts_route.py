"""GET /memories/facts and POST /memories/facts/{id}/retire — the Pulse
page's Corrections tab. Anyone signed in sees the standing facts (every
conversation already does); only the principal sees the quote of their own
message, and only the principal may retire one."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import episodic as episodic_route
from openexecutive.memory import episodic, facts


@pytest.fixture(autouse=True)
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "facts.db")
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.log_event",
        lambda event_type, summary, **kw: rows.append({"event_type": event_type, **kw}),
    )
    yield rows


def _client(monkeypatch: pytest.MonkeyPatch, *, principal: bool) -> TestClient:
    monkeypatch.setattr(episodic_route, "_caller_is_principal", lambda request: principal)
    app = FastAPI()
    app.include_router(episodic_route.router)
    return TestClient(app)


def _seed() -> tuple[int, int, int]:
    old, _ = facts.record_fact(subject="Units", statement="Maple House has 52 units.", source_quote="52")
    new, _ = facts.record_fact(subject="Units", statement="Maple House has 48 units.",
                               source_quote="Maple House is 48 units, not 52", source_channel="web",
                               session_id="s-principal", turn_id="t-1", recorded_by_person_id=1)
    prof, _ = facts.record_fact(kind="profile", subject="Company profile — Headcount",
                                statement="Headcount set to 42", source_quote="we're 42 now")
    return old.id, new.id, prof.id


def test_the_principal_sees_everything_with_history(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new, prof = _seed()
    body = _client(monkeypatch, principal=True).get("/memories/facts").json()
    assert body["can_retire"] is True
    by_id = {f["id"]: f for f in body["facts"]}
    assert set(by_id) == {old, new, prof}
    assert by_id[new]["kind"] == "correction" and by_id[new]["status"] == "active"
    assert by_id[new]["source_quote"] == "Maple House is 48 units, not 52"
    assert by_id[new]["session_id"] == "s-principal" and by_id[new]["recorded_by_person_id"] == 1
    assert by_id[old]["status"] == "superseded" and by_id[old]["superseded_by"] == new
    active = _client(monkeypatch, principal=True).get("/memories/facts?include_inactive=false").json()
    assert {f["id"] for f in active["facts"]} == {new, prof}


def test_a_teammate_sees_the_facts_but_not_the_principals_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    facts.retire_fact(new, reason="annex sold, keep quiet")
    body = _client(monkeypatch, principal=False).get("/memories/facts").json()
    assert body["can_retire"] is False
    assert body["facts"] and all(f["source_quote"] == "" for f in body["facts"])
    assert all(
        f["session_id"] is None and f["turn_id"] is None and f["recorded_by_person_id"] is None
        for f in body["facts"]
    )
    # Only what is in force: no retired or replaced text, whatever is asked.
    assert all(f["status"] == "active" for f in body["facts"])
    assert "52 units" not in str(body) and "48 units" not in str(body)
    mine = _client(monkeypatch, principal=True).get("/memories/facts").json()
    assert any(f["retired_reason"] == "annex sold, keep quiet" for f in mine["facts"])


def test_only_the_principal_retires(monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]]) -> None:
    _, new, prof = _seed()
    assert _client(monkeypatch, principal=False).post(f"/memories/facts/{new}/retire").status_code == 403
    assert facts.render_facts_for_prompt() != ""

    client = _client(monkeypatch, principal=True)
    res = client.post(f"/memories/facts/{new}/retire", json={"reason": "annex sold"})
    assert res.status_code == 200 and res.json()["status"] == "retired"
    assert res.json()["retired_reason"] == "annex sold"
    assert facts.render_facts_for_prompt() == ""
    assert [r["event_type"] for r in db] == ["fact_retired"]
    assert "annex sold" not in str(db)
    # It names what was retired: the principal's alone, never on /audit for
    # a teammate who can no longer read the fact itself.
    assert db[0]["private"] is True
    assert client.post(f"/memories/facts/{new}/retire").status_code == 409
    # Profile rows are the audit trail of a profile edit, not something to retire.
    assert client.post(f"/memories/facts/{prof}/retire").status_code == 404
    assert client.post("/memories/facts/999/retire").status_code == 404


def test_a_teammate_never_gets_the_principals_session_or_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    body = _client(monkeypatch, principal=False).get("/memories/facts").json()
    row = next(f for f in body["facts"] if f["id"] == new)
    assert row["statement"] == "Maple House has 48 units." and row["status"] == "active"
    assert row["session_id"] is None and row["turn_id"] is None
    assert row["recorded_by_person_id"] is None and row["source_quote"] == ""


def test_a_failed_audit_row_is_logged_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _, new, _ = _seed()
    warned: list[tuple[Any, ...]] = []

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr("openexecutive.audit.log_event", broken)
    # The logger itself, not caplog: another test's logging setup can stop
    # propagation to caplog's handler in a full run.
    monkeypatch.setattr(episodic_route.logger, "warning", lambda *a, **k: warned.append(a))
    res = _client(monkeypatch, principal=True).post(f"/memories/facts/{new}/retire")
    assert res.status_code == 200 and res.json()["status"] == "retired"
    assert warned and warned[0][0] % warned[0][1:] == "fact_retired audit row failed"
    # The fact's text never reaches the log line.
    assert "Maple House" not in str(warned)


# --------------------------------------------------------------------------- #
# Teammates' facts: attribution, their own retires, proposals, the switch
# --------------------------------------------------------------------------- #

SAM, KIM = 7, 8


@pytest.fixture(autouse=True)
def _trusted_teammates(db: list[dict[str, Any]]) -> None:
    # Approval is on by default; these tests store active teammate facts.
    for person in (SAM, KIM):
        facts.set_needs_approval(person, False)


def _as_teammate(monkeypatch: pytest.MonkeyPatch, person_id: int) -> TestClient:
    monkeypatch.setattr(episodic_route, "_caller_id", lambda request: person_id)
    return _client(monkeypatch, principal=False)


def _teammate_fact(person_id: int, name: str, *, subject: str = "Cedar Court",
                   proposed: bool = False) -> int:
    row, _ = facts.record_fact(
        subject=subject, statement=f"{subject} per {name}.", source_quote=f"{name} said it",
        recorded_by_person_id=person_id, recorded_by_role="teammate", recorded_by_name=name,
        session_id=f"s-{name}", proposed=proposed,
    )
    return row.id


def test_a_teammate_sees_attribution_their_own_proposals_and_what_they_may_retire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, principals, _ = _seed()
    sams = _teammate_fact(SAM, "Sam Lee")
    kims = _teammate_fact(KIM, "Kim Park", subject="Maple House")
    sams_proposal = _teammate_fact(SAM, "Sam Lee", subject="Oak Row", proposed=True)
    kims_proposal = _teammate_fact(KIM, "Kim Park", subject="Elm Yard", proposed=True)
    body = _as_teammate(monkeypatch, SAM).get("/memories/facts").json()
    by_id = {f["id"]: f for f in body["facts"]}
    assert sams_proposal in by_id and kims_proposal not in by_id
    assert by_id[kims]["recorded_by_name"] == "Kim Park" and by_id[kims]["recorded_by_role"] == "teammate"
    # Their own quote is theirs to see; nobody's ids or anyone else's quote.
    assert by_id[sams]["source_quote"] == "Sam Lee said it" and by_id[kims]["source_quote"] == ""
    assert by_id[principals]["source_quote"] == ""
    assert all(f["session_id"] is None and f["recorded_by_person_id"] is None for f in body["facts"])
    assert body["retirable_ids"] == [sams] and body["can_review"] is False


def test_a_teammate_retires_only_their_own(monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]]) -> None:
    _, principals, _ = _seed()
    sams = _teammate_fact(SAM, "Sam Lee")
    kims = _teammate_fact(KIM, "Kim Park", subject="Maple House")
    client = _as_teammate(monkeypatch, SAM)
    assert client.post(f"/memories/facts/{principals}/retire").status_code == 403
    assert client.post(f"/memories/facts/{kims}/retire").status_code == 403
    assert client.post(f"/memories/facts/{sams}/retire").json()["status"] == "retired"
    [row] = [r for r in db if r["event_type"] == "fact_retired"]
    assert row["private"] is True and row["details"]["by"] == "teammate"


def test_the_principal_approves_and_declines_proposals(
    monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]],
) -> None:
    mine, _ = facts.record_fact(subject="Cedar Court", statement="Cedar Court has 36 units.", source_quote="q")
    first = _teammate_fact(SAM, "Sam Lee", proposed=True)
    second = _teammate_fact(SAM, "Sam Lee", subject="Oak Row", proposed=True)
    assert _as_teammate(monkeypatch, SAM).post(f"/memories/facts/{first}/approve").status_code == 403
    client = _client(monkeypatch, principal=True)
    assert client.get("/memories/facts").json()["can_review"] is True
    approved = client.post(f"/memories/facts/{first}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    assert facts.get_fact(mine.id).status == "superseded"  # type: ignore[union-attr]
    assert client.post(f"/memories/facts/{first}/approve").status_code == 409
    declined = client.post(f"/memories/facts/{second}/decline", json={"reason": "not signed yet"})
    assert declined.json()["status"] == "declined"
    reviews = [r for r in db if r["event_type"] == "fact_reviewed"]
    assert [r["details"]["decision"] for r in reviews] == ["approved", "declined"]
    assert all(r["private"] is True for r in reviews)


def test_the_principal_sets_who_needs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]],
) -> None:
    from openexecutive.people import registry as people_registry
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "facts.db")
    people_store.initialize_db()
    people_registry.invalidate()
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="o@n.test")
    sam = people_store.upsert_person(full_name="Sam Lee", email="sam@n.test")
    carl = people_store.upsert_person(full_name="Carl Contact", email="c@else.test", kind="contact")
    people_registry.invalidate()
    assert _as_teammate(monkeypatch, sam).get("/memories/facts/approval").status_code == 403
    assert _as_teammate(monkeypatch, sam).put(
        f"/memories/facts/approval/{sam}", json={"needs_approval": False},
    ).status_code == 403
    client = _client(monkeypatch, principal=True)
    # On by default: a teammate's facts wait for the principal until trusted.
    assert client.get("/memories/facts/approval").json() == [
        {"person_id": sam, "full_name": "Sam Lee", "needs_approval": True},
    ]
    assert facts.needs_approval(sam) is True
    res = client.put(f"/memories/facts/approval/{sam}", json={"needs_approval": False})
    assert res.json()["needs_approval"] is False and facts.needs_approval(sam) is False
    assert client.put(f"/memories/facts/approval/{carl}", json={"needs_approval": False}).status_code == 404
    [row] = [r for r in db if r["event_type"] == "fact_approval_changed"]
    assert row["details"] == {"person_id": sam, "needs_approval": False} and row["private"] is True
    people_registry.invalidate()


def test_an_approved_fact_is_the_principals_to_retire(
    monkeypatch: pytest.MonkeyPatch, db: list[dict[str, Any]],
) -> None:
    sams = _teammate_fact(SAM, "Sam Lee", proposed=True)
    facts.approve_fact(sams)
    client = _as_teammate(monkeypatch, SAM)
    assert client.get("/memories/facts").json()["retirable_ids"] == []
    assert client.post(f"/memories/facts/{sams}/retire").status_code == 403
    own = _teammate_fact(SAM, "Sam Lee", subject="Oak Row")
    assert client.post(f"/memories/facts/{own}/retire").status_code == 200
    [row] = [r for r in db if r["event_type"] == "fact_retired"]
    assert row["details"]["caller_person_id"] == SAM


def test_history_does_not_crowd_out_a_teammates_active_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _teammate_fact(SAM, "Sam Lee", subject="Oak Row")
    for n in range(5):
        facts.record_fact(subject="Units", statement=f"Maple House has {50 + n} units.", source_quote="q")
    body = _as_teammate(monkeypatch, SAM).get("/memories/facts?limit=2").json()
    ids = [f["id"] for f in body["facts"]]
    assert len(ids) == 2 and all(facts.get_fact(i).status == "active" for i in ids)  # type: ignore[union-attr]
    assert live in ids


def test_others_proposals_cannot_crowd_out_a_teammates_view(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _teammate_fact(SAM, "Sam Lee", subject="Oak Row")
    for n in range(5):
        _teammate_fact(KIM, "Kim Park", subject=f"Kim {n}", proposed=True)
    mine = _teammate_fact(SAM, "Sam Lee", subject="Elm Yard", proposed=True)
    body = _as_teammate(monkeypatch, SAM).get("/memories/facts?limit=2").json()
    assert {f["id"] for f in body["facts"]} == {live, mine}
