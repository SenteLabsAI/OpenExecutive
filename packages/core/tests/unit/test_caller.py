"""api/caller.py: the one place the API reads the signed-in caller."""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from openexecutive.api import caller as api_caller
from openexecutive.api.caller import Caller
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

PACKAGE = Path(api_caller.__file__).resolve().parents[1]


def _request(headers: dict[str, str] | None = None, verified: Any = None) -> Any:
    state = SimpleNamespace() if verified is None else SimpleNamespace(caller=verified)
    return SimpleNamespace(headers=headers or {}, state=state)


@pytest.fixture
def principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    yield people_store.upsert_person(
        full_name="Olivia Owner", is_principal=True, email="olivia@co.example"
    )
    people_registry.invalidate()


def test_nothing_else_reads_the_caller_headers() -> None:
    """Every read of an ``x-caller-*`` header goes through api/caller.py, so
    how far a caller is trusted is decided in one place."""
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == Path(api_caller.__file__).resolve():
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            header = (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.strip().lower().startswith("x-caller-")
            )
            # FastAPI turns a parameter named x_caller_email into the header.
            param = isinstance(node, ast.arg) and node.arg.lower().startswith("x_caller_")
            if header or param:
                offenders.append(f"{path.relative_to(PACKAGE)}:{getattr(node, 'lineno', '?')}")
    assert offenders == []


def test_open_mode_reads_the_header_as_sent() -> None:
    who = api_caller.caller(_request({"x-caller-email": "  Alice@Example.COM "}))
    assert who == Caller("open", "alice@example.com")
    assert not who.defaults_to_principal
    nobody = api_caller.caller(_request())
    assert nobody == Caller("open", "") and nobody.defaults_to_principal
    # A request object with no state at all (a test double) reads the same.
    assert api_caller.caller(SimpleNamespace(headers={})) == nobody


@pytest.mark.parametrize(
    ("who", "email", "signed_in", "actor", "key", "principal"),
    [
        (Caller("open"), "", False, "api", "local", True),
        (Caller("open", "a@x.example"), "a@x.example", True, "a@x.example", "email:a@x.example", False),
        (Caller("user", "a@x.example"), "a@x.example", True, "a@x.example", "email:a@x.example", False),
        (Caller("operator"), "", False, "api", "local", True),
        (Caller("service"), "", False, "api", "service", False),
    ],
)
def test_each_kind_of_caller(
    who: Caller, email: str, signed_in: bool, actor: str, key: str, principal: bool
) -> None:
    request = _request(verified=who) if who.kind != "open" else _request(
        {"x-caller-email": who.email} if who.email else {}
    )
    assert api_caller.caller(request) == who
    assert api_caller.caller_email(request) == email
    assert api_caller.signed_in(request) is signed_in
    assert api_caller.actor(request) == actor
    assert api_caller.identity_key(request) == key
    assert who.defaults_to_principal is principal


def test_a_verified_caller_wins_over_the_header() -> None:
    # Only the gate sets request.state.caller; once it has, a header the
    # request also carries says nothing.
    request = _request({"x-caller-email": "olivia@co.example"}, verified=Caller("service"))
    assert api_caller.caller(request) == Caller("service")
    # Anything else in that slot is not a verified caller.
    assert api_caller.caller(_request({}, verified="olivia@co.example")) == Caller("open")


def test_the_actor_is_cut_to_length() -> None:
    long = "a" * 300 + "@x.example"
    assert api_caller.actor(_request({"x-caller-email": long})) == "a" * api_caller.MAX_ACTOR_LEN


def test_a_service_call_is_never_the_principal(
    principal: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import delegation as delegation_route
    from openexecutive.api.routes.chat import _resolve_caller_person_id
    from openexecutive.api.routes.workflows import _caller_is_the_principal

    monkeypatch.setattr(delegation_route, "local_login", lambda: True)
    service = _request(verified=Caller("service"))
    assert _resolve_caller_person_id(service) is None
    assert _caller_is_the_principal(service) is False
    with pytest.raises(HTTPException) as refused:
        delegation_route._caller(service)
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "sign_in_required"

    operator = _request(verified=Caller("operator"))
    assert _resolve_caller_person_id(operator) == principal
    assert _caller_is_the_principal(operator) is True
    assert delegation_route._caller(operator).id == principal

    user = _request(verified=Caller("user", "olivia@co.example"))
    assert _resolve_caller_person_id(user) == principal
    assert _caller_is_the_principal(user) is True


def test_act_as_me_needs_local_login_for_a_caller_who_names_no_one(
    principal: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import delegation as delegation_route

    monkeypatch.setattr(delegation_route, "local_login", lambda: False)
    for request in (_request(), _request(verified=Caller("operator"))):
        with pytest.raises(HTTPException) as refused:
            delegation_route._caller(request)
        assert refused.value.detail["code"] == "sign_in_required"


def test_a_service_never_claims_an_install_with_no_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    path = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", path)
    monkeypatch.setattr(people_store, "DB_PATH", path)
    episodic.initialize_db(path)
    people_store.initialize_db(path)
    people_registry.invalidate()
    # No owner yet: open to whoever is setting the install up...
    assert _caller_is_principal_or_unclaimed(_request()) is True
    assert _caller_is_principal_or_unclaimed(_request(verified=Caller("operator"))) is True
    assert _caller_is_principal_or_unclaimed(_request(verified=Caller("user", "a@x.example"))) is True
    # ...but never to a caller that names no one.
    assert _caller_is_principal_or_unclaimed(_request(verified=Caller("service"))) is False

