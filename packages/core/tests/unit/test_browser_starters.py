"""The "Try one" pills on chat's card once the browser is connected."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.browser import relay as browser_relay
from openexecutive.browser import starters as browser_starters
from openexecutive.memory import episodic
from openexecutive.memory.company_profile import CompanyProfile, CompetitiveLandscape
from openexecutive.people import store as people_store


def _profile(**kw: object) -> CompanyProfile:
    rivals = kw.pop("rivals", [])
    return CompanyProfile(competitive_landscape=CompetitiveLandscape(primary_competitors=rivals), **kw)  # type: ignore[arg-type]


def test_a_full_profile_names_the_site_a_rival_and_the_company() -> None:
    got = browser_starters.starters(
        _profile(name="Harbor Lane", website="https://www.harborlane.com/", rivals=["Northwind"])
    )
    assert got == [
        "Open harborlane.com and tell me what you'd fix on the home page",
        "Compare Northwind's pricing page with ours",
        "Find this week's news about Harbor Lane and sum it up",
    ]


def test_without_a_website_the_rival_pill_doesnt_say_ours() -> None:
    got = browser_starters.starters(_profile(name="Harbor Lane", rivals=["Contoso Labs"]))
    assert got[0] == "Find Contoso Labs' pricing page and sum up their plans"
    assert got[1] == "Find this week's news about Harbor Lane and sum it up"
    assert got[2] == browser_starters.GENERAL[0]


def test_an_empty_profile_gets_general_pills_that_need_no_company() -> None:
    assert browser_starters.starters(CompanyProfile()) == list(browser_starters.GENERAL)


@pytest.mark.parametrize("website", ["javascript:alert(1)", "not a site", "ftp://files.example.com", "http://"])
def test_a_website_that_isnt_one_is_left_out(website: str) -> None:
    got = browser_starters.starters(_profile(website=website))
    assert not any(p.startswith("Open ") and "home page" in p for p in got)


def test_a_bare_host_is_read_as_a_website() -> None:
    assert browser_starters.starters(_profile(website="harborlane.com"))[0].startswith("Open harborlane.com ")


def test_overlong_names_and_the_company_itself_as_a_rival_are_left_out() -> None:
    got = browser_starters.starters(_profile(name="Harbor Lane", rivals=["Harbor Lane", "x" * 60, "Northwind"]))
    assert got[0] == "Find Northwind's pricing page and sum up their plans"
    long_name = browser_starters.starters(_profile(name="y" * 60))
    assert long_name == list(browser_starters.GENERAL)


def test_never_more_than_three() -> None:
    got = browser_starters.starters(_profile(name="A", website="a.com", rivals=["B", "C", "D"]))
    assert len(got) == browser_starters.MAX


# ---- The route ----


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from openexecutive.api.routes import browser as route

    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db()
    who: dict[str, int | None] = {"id": None}
    monkeypatch.setattr("openexecutive.api.routes.chat._resolve_caller_person_id", lambda _r: who["id"])
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump({"company": {"name": "Harbor Lane", "website": "https://harborlane.com"}}), encoding="utf-8"
    )

    class _Settings:
        company_profile_path = profile_path

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _Settings())
    browser_relay._reset_for_tests()
    app = FastAPI()
    app.include_router(route.router)
    c = TestClient(app)
    c.who = who  # type: ignore[attr-defined]
    return c


def test_the_route_is_for_people_on_the_team_and_reads_the_profile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/browser/starters").status_code == 403
    client.who["id"] = people_store.upsert_person(  # type: ignore[attr-defined]
        full_name="Dana Lee", email="dana@example.com", is_principal=True
    )
    got = client.get("/browser/starters").json()["starters"]
    assert got[0] == "Open harborlane.com and tell me what you'd fix on the home page"
    assert got[1] == "Find this week's news about Harbor Lane and sum it up"
    monkeypatch.setenv(browser_relay.ENV_OFF, "1")
    assert client.get("/browser/starters").json() == {"starters": []}


def test_hidden_characters_are_dropped_from_names() -> None:
    got = browser_starters.starters(_profile(name="Harbor\u202e Lane\u200b"))
    assert got[0] == "Find this week's news about Harbor Lane and sum it up"


# ---- When it isn't connected ----


def test_not_connected_names_the_company_and_leaves_the_steps_to_the_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openexecutive.browser import tools as browser_tools

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump({"company": {"name": "Harbor  Lane {x}"}}), encoding="utf-8")

    class _Settings:
        company_profile_path = profile_path

    monkeypatch.setattr("openexecutive.config.get_settings", lambda: _Settings())
    said = browser_tools.not_connected()
    assert "isn't connected to Harbor Lane {x} yet" in said
    assert "Don't list the steps" in said and "card" in said
    profile_path.unlink()
    assert "isn't connected to this company yet" in browser_tools.not_connected()


def test_the_browser_is_offered_for_what_a_search_cant_read() -> None:
    from openexecutive.browser import tools as browser_tools

    assert "instead of asking them to download" in browser_tools.GROUP_PURPOSE
