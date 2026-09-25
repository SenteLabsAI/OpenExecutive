"""HTTP-level tests for the company-onboarding wizard's final step.

Regression for issue #84's second half: ``/onboard/answer`` used to commit the
final answer (``completed=True``) *before* building the profile. When the
build raised, the client got a 500 and every retry hit "Onboarding already
completed" — the only way out was to restart onboarding from scratch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.models import ONBOARD_ANSWER_MAX_CHARS
from openexecutive.api.routes import onboarding as route
from openexecutive.onboarding import profile_builder
from openexecutive.onboarding.wizard import TOTAL_STEPS
from openexecutive.people import store as people_store


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    async def _no_research(session_id: str) -> None:
        return None

    # The final answer checks the caller against the roster and fails closed
    # when it can't be read — as the shared ./episodic_memory.db briefly can't
    # while another test process builds it. An empty roster of its own (no
    # owner yet) lets the final answer through.
    roster = tmp_path / "episodic.db"
    monkeypatch.setattr(people_store, "DB_PATH", roster)
    people_store.initialize_db(roster)

    monkeypatch.setattr(route, "_fire_post_onboarding_research", _no_research)
    monkeypatch.setattr(route, "_wizard_sessions", {})
    monkeypatch.setattr(route, "_onboarding_research_fired", set())
    # /onboard/start reads the workspace mode; pin team so a stray local DB
    # cannot change the step count. The solo tests below switch it.
    _set_mode(monkeypatch, "team")
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _answer_all_but_last(client: TestClient) -> str:
    session_id = client.get("/onboard/start").json()["session_id"]
    for _ in range(TOTAL_STEPS - 1):
        resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": "x"})
        assert resp.status_code == 200
        assert resp.json()["completed"] is False
    return session_id


def test_builder_failure_on_final_answer_is_retryable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _boom(state):  # type: ignore[no-untyped-def]
        calls.append("boom")
        raise ValueError("could not convert string to float: ''")

    monkeypatch.setattr(profile_builder, "build_and_save_profile", _boom)
    session_id = _answer_all_but_last(client)

    resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": "final"})
    assert resp.status_code == 422
    assert "rephrase" in resp.json()["detail"].lower()

    # The session was rolled back to the last step, not stuck at completed.
    status = client.get(f"/onboard/status/{session_id}").json()
    assert status["completed"] is False
    assert status["current_step"] == TOTAL_STEPS - 1

    # A retry with a working builder completes normally...
    def _ok(state):  # type: ignore[no-untyped-def]
        calls.append("ok")

    monkeypatch.setattr(profile_builder, "build_and_save_profile", _ok)
    resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": "final"})
    assert resp.status_code == 200
    assert resp.json()["completed"] is True
    assert calls == ["boom", "ok"]

    # ...and only then does the session refuse further answers.
    resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": "again"})
    assert resp.status_code == 400


def test_issue_84_answer_completes_through_the_real_builder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: the reporter's exact business-model answer finishes onboarding."""
    real_build = profile_builder.build_and_save_profile

    def _build_to_tmp(state):  # type: ignore[no-untyped-def]
        return real_build(state, profile_path=tmp_path / "profile.yaml")

    monkeypatch.setattr(profile_builder, "build_and_save_profile", _build_to_tmp)
    monkeypatch.setattr(profile_builder, "_save_wizard_people", lambda answers: None)

    session_id = client.get("/onboard/start").json()["session_id"]
    answers = {"business_model": "IT, marketing and video agency"}
    for step in range(TOTAL_STEPS):
        from openexecutive.onboarding.wizard import WIZARD_STEPS

        answer = answers.get(WIZARD_STEPS[step]["field"], "x")
        resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": answer})
        assert resp.status_code == 200, resp.json()

    assert resp.json()["completed"] is True
    assert (tmp_path / "profile.yaml").exists()


def test_oversized_answer_is_rejected_before_parsing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def _never(state):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    monkeypatch.setattr(profile_builder, "build_and_save_profile", _never)
    session_id = client.get("/onboard/start").json()["session_id"]

    too_long = "SECRET-BURN-" + "x" * ONBOARD_ANSWER_MAX_CHARS
    resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": too_long})
    assert resp.status_code == 422
    assert route._wizard_sessions[session_id].current_step == 0
    # The rejection must not echo the answer back (FastAPI's default
    # validation error would include the full `input`).
    assert "SECRET-BURN" not in resp.text
    assert len(resp.content) < 500

    just_fits = "x" * ONBOARD_ANSWER_MAX_CHARS
    resp = client.post("/onboard/answer", json={"session_id": session_id, "answer": just_fits})
    assert resp.status_code == 200
    assert called is False


# ── solo mode: the wizard skips the team steps ──────────────────────────────


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from openexecutive.memory import workspace_settings as ws

    monkeypatch.setattr(ws, "get_workspace", lambda *a, **k: ws.WorkspaceSettings(mode=mode))


def test_team_mode_asks_every_step(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "team")
    status = client.get("/onboard/start").json()
    assert status["total_steps"] == TOTAL_STEPS
    assert status["current_step"] == 0
    assert status["optional"] is False


def test_solo_mode_skips_team_size_members_and_fractional_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.onboarding.wizard import SOLO_SKIPPED_FIELDS, WIZARD_STEPS

    _set_mode(monkeypatch, "solo")
    built: list[Any] = []
    monkeypatch.setattr(profile_builder, "build_and_save_profile", built.append)

    skipped = {s["question"] for s in WIZARD_STEPS if s["field"] in SOLO_SKIPPED_FIELDS}
    assert len(skipped) == 3
    solo_total = TOTAL_STEPS - 3

    status = client.get("/onboard/start").json()
    session_id = status["session_id"]
    assert status["total_steps"] == solo_total
    asked: list[str] = []
    positions: list[int] = []
    optional: list[bool] = []
    while not status["completed"]:
        asked.append(status["current_question"])
        positions.append(status["current_step"])
        optional.append(status["optional"])
        # The team-size step was required; nothing solo asks is skipped here.
        status = client.post(
            "/onboard/answer", json={"session_id": session_id, "answer": "x"}
        ).json()
        assert status["total_steps"] == solo_total

    assert not skipped & set(asked)
    assert positions == list(range(solo_total))
    # The optional steps (culture onward) are flagged so the UI offers Skip.
    assert optional == [not s["required"] for s in WIZARD_STEPS if s["field"] not in SOLO_SKIPPED_FIELDS]
    assert status["progress_percent"] == 100
    assert len(built) == 1
    state = built[0]
    assert state.solo is True
    assert not SOLO_SKIPPED_FIELDS & set(state.answers)


def test_solo_wizard_saves_only_the_principal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through the real builder: the roster steps never run, so the
    only person a solo wizard can create is the principal."""
    from openexecutive.onboarding.wizard import SOLO_SKIPPED_FIELDS, WIZARD_STEPS
    from openexecutive.people import store as people_store

    _set_mode(monkeypatch, "solo")
    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    real_build = profile_builder.build_and_save_profile
    monkeypatch.setattr(
        profile_builder,
        "build_and_save_profile",
        lambda state: real_build(state, profile_path=tmp_path / "profile.yaml"),
    )

    answers = {"name": "Dana Studio", "principal_identity": "Dana Reyes, Founder"}
    session_id = client.get("/onboard/start").json()["session_id"]
    for step in WIZARD_STEPS:
        if step["field"] in SOLO_SKIPPED_FIELDS:
            continue
        resp = client.post(
            "/onboard/answer",
            json={"session_id": session_id, "answer": answers.get(step["field"], "x")},
        )
        assert resp.status_code == 200, resp.json()
    assert resp.json()["completed"] is True

    people = people_store.list_people(db_path=db)
    assert [(p.full_name, p.is_principal) for p in people] == [("Dana Reyes", True)]


# ── who may save the form ────────────────────────────────────────────────────
# Saving adds people with their emails (the web sign-in allow-list) and a
# principal, so once there is an owner only they may — as on the People page.


@pytest.fixture()
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    people_store.upsert_person(full_name="Dana Reyes", email="dana@example.com", is_principal=True)
    people_store.upsert_person(full_name="Bob Lin", email="bob@example.com")
    return db


def _finish(client: TestClient, session_id: str, caller: str) -> Any:
    return client.post(
        "/onboard/answer",
        json={"session_id": session_id, "answer": "final"},
        headers={"x-caller-email": caller},
    )


def test_a_teammate_cannot_save_the_form(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, roster: Path
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(profile_builder, "build_and_save_profile", built.append)
    session_id = _answer_all_but_last(client)

    resp = _finish(client, session_id, "bob@example.com")
    assert resp.status_code == 403
    assert built == []
    # Rolled back rather than stuck, so the owner can still finish it.
    assert client.get(f"/onboard/status/{session_id}").json()["completed"] is False
    assert _finish(client, session_id, "dana@example.com").status_code == 200
    assert len(built) == 1


def test_before_there_is_an_owner_anyone_can_save_the_form(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "no-roster-yet.db")
    built: list[Any] = []
    monkeypatch.setattr(profile_builder, "build_and_save_profile", built.append)
    session_id = _answer_all_but_last(client)
    assert _finish(client, session_id, "sam@example.com").status_code == 200
    assert len(built) == 1
