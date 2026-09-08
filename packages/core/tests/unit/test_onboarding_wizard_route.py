"""HTTP-level tests for the company-onboarding wizard's final step.

Regression for issue #84's second half: ``/onboard/answer`` used to commit the
final answer (``completed=True``) *before* building the profile. When the
build raised, the client got a 500 and every retry hit "Onboarding already
completed" — the only way out was to restart onboarding from scratch.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.models import ONBOARD_ANSWER_MAX_CHARS
from openexecutive.api.routes import onboarding as route
from openexecutive.onboarding import profile_builder
from openexecutive.onboarding.wizard import TOTAL_STEPS


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _no_research(session_id: str) -> None:
        return None

    monkeypatch.setattr(route, "_fire_post_onboarding_research", _no_research)
    monkeypatch.setattr(route, "_wizard_sessions", {})
    monkeypatch.setattr(route, "_onboarding_research_fired", set())
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
