"""Onboarding bootstrap and ownership authorization tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import onboarding as route
from openexecutive.people import store as people_store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    people_store.initialize_db()
    route._wizard_sessions.clear()
    route._wizard_session_owners.clear()
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def test_bootstrap_requires_verified_owner_and_principal_identity(client: TestClient) -> None:
    assert client.get("/onboard/start").status_code == 403

    started = client.get(
        "/onboard/start", headers={"X-Caller-Email": "owner@example.com"}
    )
    assert started.status_code == 200
    body = started.json()
    assert body["current_step_required"] is True

    # A different signed-in user cannot read or take over a bootstrap session.
    assert client.get(f"/onboard/status/{body['session_id']}").status_code == 403
    assert client.get(
        f"/onboard/status/{body['session_id']}",
        headers={"X-Caller-Email": "member@example.com"},
    ).status_code == 403
    assert client.post(
        "/onboard/answer",
        json={"session_id": body["session_id"], "answer": "Acme"},
        headers={"X-Caller-Email": "member@example.com"},
    ).status_code == 403

    # The required principal-identity step cannot be skipped; it remains on
    # that step rather than completing an unbootstrappable installation.
    state = route._wizard_sessions[body["session_id"]]
    state.current_step = 9
    response = client.post(
        "/onboard/answer",
        json={"session_id": body["session_id"], "answer": "skip"},
        headers={"X-Caller-Email": "owner@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["current_step"] == 9
    assert response.json()["current_step_required"] is True


def test_completed_wizard_invalidates_same_owner_stale_sessions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_research(_session_id: str) -> None:
        return None

    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.build_and_save_profile",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(route, "_fire_post_onboarding_research", no_research)
    headers = {"X-Caller-Email": "owner@example.com"}
    first = client.get("/onboard/start", headers=headers).json()
    stale = client.get("/onboard/start", headers=headers).json()
    # Complete the remaining optional final step from a state containing the
    # required principal identity; this exercises the endpoint's save path.
    state = route._wizard_sessions[first["session_id"]]
    state.current_step = 11
    state.answers["principal_identity"] = "Owner, CEO"
    assert client.post(
        "/onboard/answer",
        json={"session_id": first["session_id"], "answer": "skip"},
        headers=headers,
    ).status_code == 200

    assert client.post(
        "/onboard/answer",
        json={"session_id": stale["session_id"], "answer": "Acme"},
        headers=headers,
    ).status_code == 404


def test_configured_bootstrap_owner_excludes_other_fallback_users(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINCIPAL_EMAIL", "owner@example.com")

    assert client.get(
        "/onboard/start", headers={"X-Caller-Email": "member@example.com"}
    ).status_code == 403
    assert client.get(
        "/onboard/start", headers={"X-Caller-Email": "owner@example.com"}
    ).status_code == 200


def test_existing_principal_is_the_only_onboarding_user(client: TestClient) -> None:
    people_store.upsert_person(
        full_name="Owner", is_principal=True, email="owner@example.com"
    )

    assert client.get(
        "/onboard/start", headers={"X-Caller-Email": "member@example.com"}
    ).status_code == 403
    assert client.get(
        "/onboard/start", headers={"X-Caller-Email": "owner@example.com"}
    ).status_code == 200
