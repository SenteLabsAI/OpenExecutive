"""HTTP-level tests for /company-profile — PUT creates, PATCH still requires an existing profile."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import company_profile as profile_route


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml"))
    app = FastAPI()
    app.include_router(profile_route.router)
    return TestClient(app)


def test_get_404_when_empty(client: TestClient) -> None:
    assert client.get("/company-profile").status_code == 404


def test_patch_404_when_empty(client: TestClient) -> None:
    resp = client.patch("/company-profile", json={"name": "Acme"})
    assert resp.status_code == 404


def test_put_creates_profile_on_fresh_install(client: TestClient, tmp_path: Path) -> None:
    body = {
        "name": "Sente Labs",
        "industry": "AI R&D lab",
        "stage": "Bootstrapped LLC",
        "headcount": 5,
        "target_customer": {"profile": "Mid-market ops leaders", "pain_points": ["AI stalls in the org"]},
        "financials": {"key_metrics": {"billable_rate_usd_hr": 250}},
    }
    resp = client.put("/company-profile", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Sente Labs"
    assert data["headcount"] == 5
    assert data["target_customer"]["pain_points"] == ["AI stalls in the org"]
    assert data["financials"]["key_metrics"] == {"billable_rate_usd_hr": 250}
    assert (tmp_path / "company" / "profile.yaml").exists()

    # Now visible to GET and patchable.
    assert client.get("/company-profile").json()["industry"] == "AI R&D lab"
    patched = client.patch("/company-profile", json={"stage": "Seed"})
    assert patched.status_code == 200
    assert patched.json()["stage"] == "Seed"
    assert patched.json()["headcount"] == 5


def test_put_replaces_rather_than_merges(client: TestClient) -> None:
    client.put("/company-profile", json={"name": "Acme", "mission": "Old mission", "headcount": 40})
    resp = client.put("/company-profile", json={"name": "Acme", "industry": "Robotics"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["industry"] == "Robotics"
    assert data["mission"] == ""
    assert data["headcount"] is None


def test_put_requires_name(client: TestClient) -> None:
    assert client.put("/company-profile", json={"industry": "x"}).status_code == 422
    assert client.put("/company-profile", json={"name": ""}).status_code == 422
