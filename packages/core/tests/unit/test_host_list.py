"""The host list (packages/ui/src/lib/host-list.json): the calls a host
origin's page may make through the web app's proxy (docs/auth.md, "Host
origins"). The proxy enforces it; this checks it against the API itself, so a
route renamed or removed here fails in the same change, and so nothing that
approves, sends or changes who may do what is ever on it."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

HOST_LIST_PATH = Path(__file__).resolve().parents[3] / "ui" / "src" / "lib" / "host-list.json"
CALLS: list[list[str]] = json.loads(HOST_LIST_PATH.read_text(encoding="utf-8"))["calls"]

# What only Open Executive's own pages may do: approving, rejecting or cancelling a card,
# preparing a draft, Act as me's send, delegation, settings, People and the
# rest of the company's set-up. Watch list proposals are the one approve the
# host list holds: approving a suggested watch only starts watching.
_FORBIDDEN_VERBS = re.compile(r"/(approve|reject|cancel|prepare|decide|decision|send|bulk-approve)(/|$)")
_FORBIDDEN_PREFIXES = (
    "/delegation", "/settings", "/people", "/departments", "/goals", "/company-profile",
    "/knowledge", "/documents", "/workflows", "/audit", "/executive", "/agents", "/onboarding",
    "/review", "/artifacts", "/mcp", "/webhook", "/a2a",
)


def _api_routes() -> set[tuple[str, str]]:
    from openexecutive.api.main import create_app

    routes: set[tuple[str, str]] = set()
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        # FastAPI's {name:converter} is {name} on the host list.
        path = re.sub(r"\{([a-z_]+):[a-z_]+\}", r"{\1}", route.path)
        for method in route.methods:
            routes.add((method, path))
    return routes


def test_every_entry_names_a_real_route() -> None:
    routes = _api_routes()
    missing = [(m, p) for m, p in CALLS if (m, p) not in routes]
    assert missing == []


def test_entries_are_well_formed_and_listed_once() -> None:
    assert len({(m, p) for m, p in CALLS}) == len(CALLS)
    for method, path in CALLS:
        assert method in {"GET", "POST", "PATCH", "PUT", "DELETE"}
        assert re.fullmatch(r"(/([a-z0-9-]+|\{[a-z_]+\}))+", path), path


@pytest.mark.parametrize(("method", "path"), [tuple(c) for c in CALLS], ids=lambda v: str(v))
def test_nothing_that_acts_for_the_person_is_on_it(method: str, path: str) -> None:
    assert not path.startswith(_FORBIDDEN_PREFIXES), path
    # Who is signed in, never the roster's emails.
    if path.startswith("/auth/"):
        assert (method, path) == ("GET", "/auth/me"), path
    if path.startswith("/watchlist/{slug}/"):
        return
    assert not _FORBIDDEN_VERBS.search(path), path


def test_the_card_actions_are_not_on_it() -> None:
    calls = {(m, p) for m, p in CALLS}
    for path in (
        "/decisions/{instance_id}/approve", "/decisions/{instance_id}/reject",
        "/decisions/{instance_id}/cancel", "/alerts/{alert_id}/approve",
    ):
        assert ("POST", path) in _api_routes()
        assert ("POST", path) not in calls
