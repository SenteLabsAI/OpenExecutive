"""Signed callers (api/caller.py): with CALLER_ASSERTION_PUBLIC_KEYS set, who
is calling comes from an assertion the UI signs, never from x-caller-email
alone."""
from __future__ import annotations

import base64
import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from openexecutive.api import caller as api_caller
from openexecutive.api.caller import AssertionRefused, Caller, JtiLedger
from openexecutive.api.routes import executive as executive_route
from openexecutive.memory import episodic
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

_REPO = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "mint_caller_assertion", _REPO / "scripts" / "mint-caller-assertion.py"
)
assert _spec is not None and _spec.loader is not None
mint_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mint_script)

VECTORS = json.loads((Path(__file__).parent / "caller_assertion_vectors.json").read_text())
SIGNING_KEY = VECTORS["test_signing_key"]
KEYS = api_caller.parse_public_keys(VECTORS["public_keys"])
OWNER = "olivia@co.example"


def _mint(kind: str = "user", email: str = OWNER, method: str = "GET", target: str = "/x", **kw: Any) -> str:
    return str(mint_script.mint(SIGNING_KEY, kind=kind, email=email, method=method, target=target, **kw))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _signed(claims: dict[str, Any], version: str = "v1") -> str:
    """A token over arbitrary claims, signed with the vectors' key."""
    _kid, key = mint_script.load_private_key(SIGNING_KEY)
    signing_input = f"{version}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(key.sign(signing_input.encode()))}"


def _signed_raw(payload: bytes) -> str:
    """A token over raw payload bytes, signed with the vectors' key."""
    _kid, key = mint_script.load_private_key(SIGNING_KEY)
    signing_input = f"v1.{_b64(payload)}"
    return f"{signing_input}.{_b64(key.sign(signing_input.encode()))}"


def _claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "aud": "openexecutive-api", "exp": now + 60, "iat": now, "jti": "j" * 24,
        "kid": "rfc8032-test1", "kind": "user", "m": "GET", "p": "/x", "sub": OWNER,
    }
    return {**base, **over}


def _verify(token: str, *, method: str = "GET", target: str = "/x", now: int | None = None,
            ledger: JtiLedger | None = None) -> Caller:
    return api_caller.verify_assertion(
        token, keys=KEYS, method=method, target=target,
        now=int(time.time()) if now is None else now,
        ledger=JtiLedger() if ledger is None else ledger,
    )


# ── the assertion itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["kind"] + c["target"])
def test_the_shared_vectors_sign_and_verify(case: dict[str, Any]) -> None:
    """The script signs exactly what the web app signs (the UI's test checks
    the same tokens), and the API reads them back."""
    token = _mint(case["kind"], case["email"], case["method"], case["target"],
                  now_ms=case["now_ms"], jti=case["jti"])
    assert token == case["token"]
    who = _verify(token, method=case["method"].upper(), target=case["target"],
                  now=case["now_ms"] // 1000)
    expected = Caller("user", case["email"].lower()) if case["kind"] == "user" else Caller("operator")
    assert who == expected


def test_the_constants_match_the_script() -> None:
    assert mint_script.AUDIENCE == api_caller.AUDIENCE
    assert mint_script.VERSION == api_caller.ASSERTION_VERSION
    assert mint_script.LIFETIME_S <= api_caller.MAX_LIFETIME_S


def test_it_holds_for_one_request_once() -> None:
    ledger = JtiLedger()
    token = _mint(method="POST", target="/executive/pause")
    assert _verify(token, method="POST", target="/executive/pause", ledger=ledger) == Caller("user", OWNER)
    with pytest.raises(AssertionRefused, match="replayed"):
        _verify(token, method="POST", target="/executive/pause", ledger=ledger)


@pytest.mark.parametrize(
    ("method", "target", "reason"),
    [
        ("POST", "/x", "method"),
        ("GET", "/y", "path"),
        ("GET", "/x?limit=500", "path"),
        ("GET", "/X", "path"),
    ],
)
def test_it_names_one_method_and_path(method: str, target: str, reason: str) -> None:
    with pytest.raises(AssertionRefused, match=reason):
        _verify(_mint(), method=method, target=target)


# (claims relative to now, the reason it is refused)
_BAD_CLAIMS: dict[str, tuple[dict[str, Any], str]] = {
    "audience": ({"aud": "another-api"}, "audience"),
    "expired": ({"exp": -1, "iat": -61}, "expired"),
    "from_the_future": ({"iat": 300, "exp": 360}, "not_yet_valid"),
    "too_long_lived": ({"exp": 3600}, "lifetime"),
    "no_lifetime": ({"exp": 0}, "lifetime"),
    "bool_time": ({"iat": True}, "malformed"),
    "string_time": ({"iat": "1790000000"}, "malformed"),
    "short_jti": ({"jti": "short"}, "malformed"),
    "no_jti": ({"jti": None}, "malformed"),
    "unknown_kind": ({"kind": "admin"}, "subject"),
    "service_kind": ({"kind": "service", "sub": ""}, "subject"),
    "operator_naming_someone": ({"kind": "operator", "sub": OWNER}, "subject"),
    "user_naming_no_one": ({"kind": "user", "sub": ""}, "subject"),
    "user_not_lowercased": ({"kind": "user", "sub": "Olivia@Co.Example"}, "subject"),
    "user_not_an_email": ({"kind": "user", "sub": "not an email"}, "subject"),
    "user_sub_not_a_string": ({"kind": "user", "sub": None}, "malformed"),
    # Signed with a key the API has, but claiming another.
    "other_key_id": ({"kid": "another-key"}, "key_id"),
    "no_key_id": ({"kid": None}, "key_id"),
}


@pytest.mark.parametrize("name", sorted(_BAD_CLAIMS))
def test_claims_that_do_not_hold_are_refused(name: str) -> None:
    over, reason = _BAD_CLAIMS[name]
    now = int(time.time())
    # Times in the table are offsets from now.
    shifted = {k: now + v if k in ("iat", "exp") and type(v) is int else v for k, v in over.items()}
    with pytest.raises(AssertionRefused, match=reason):
        _verify(_signed(_claims(**shifted)), now=now)


def test_a_refused_assertion_spends_nothing() -> None:
    ledger = JtiLedger()
    token = _mint()
    with pytest.raises(AssertionRefused, match="path"):
        _verify(token, target="/y", ledger=ledger)
    assert _verify(token, ledger=ledger) == Caller("user", OWNER)


_MALFORMED: dict[str, Any] = {
    "empty": lambda: "",
    "two_parts": lambda: "v1.abc",
    "other_version": lambda: "v2." + _mint().split(".", 1)[1],
    "four_parts": lambda: _mint() + ".extra",
    "not_base64url": lambda: "v1.!!!.abc",
    # Signed, so they get as far as the parser.
    "not_json": lambda: _signed_raw(b"not json"),
    "not_an_object": lambda: _signed_raw(b"[1, 2]"),
    "too_long": lambda: "v1." + "A" * 5000 + ".x",
    "not_ascii": lambda: "v1.\u00e9.x",
}


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_malformed_assertions_are_refused(name: str) -> None:
    with pytest.raises(AssertionRefused, match="malformed"):
        _verify(_MALFORMED[name]())


def test_a_changed_payload_or_another_key_fails_the_signature() -> None:
    version, payload, signature = _mint().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["sub"] = "mallory@co.example"
    forged = f"{version}.{_b64(json.dumps(claims).encode())}.{signature}"
    with pytest.raises(AssertionRefused, match="signature"):
        _verify(forged)

    # Same key id, someone else's key.
    stranger = Ed25519PrivateKey.generate()
    signing_input = f"v1.{_b64(json.dumps(_claims()).encode())}"
    with pytest.raises(AssertionRefused, match="signature"):
        _verify(f"{signing_input}.{_b64(stranger.sign(signing_input.encode()))}")


def test_nothing_unsigned_reaches_the_json_parser() -> None:
    """A payload nested deep enough to blow the parser's stack is refused as
    unsigned before it is parsed; signed (only the UI could), it is still a
    clean refusal, not a 500."""
    nested = b"[" * 5000 + b"]" * 5000
    body = _b64(nested)[:3000]
    with pytest.raises(AssertionRefused, match="signature"):
        _verify(f"v1.{body}.{_b64(b'x' * 64)}")
    with pytest.raises(AssertionRefused, match="malformed"):
        _verify(_signed_raw(b"[" * 2000 + b"]" * 2000))


def test_several_keys_while_rotating() -> None:
    fresh_private, fresh_public = _new_pair("k2")
    keys = api_caller.parse_public_keys(f"{VECTORS['public_keys']}, {fresh_public}")
    assert set(keys) == {"rfc8032-test1", "k2"}
    for setting in (SIGNING_KEY, fresh_private):
        token = mint_script.mint(setting, kind="operator", email="", method="GET", target="/x")
        who = api_caller.verify_assertion(
            token, keys=keys, method="GET", target="/x", now=int(time.time()), ledger=JtiLedger()
        )
        assert who == Caller("operator")


def _new_pair(kid: str) -> tuple[str, str]:
    spec = importlib.util.spec_from_file_location("make_caller_keys", _REPO / "scripts" / "make-caller-keys.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    private, public = module.make_pair(kid)
    return str(private), str(public)


@pytest.mark.parametrize(
    "raw",
    [
        "no-separator",
        "k1:tooshort",
        "bad kid!:" + "A" * 43,
        "k1:" + "A" * 43 + ",k1:" + "B" * 43,
        ":" + "A" * 43,
        # Set, but no key: the gate would check nothing while signing_on()
        # said it was on.
        " , ",
    ],
)
def test_unusable_public_keys_raise(raw: str) -> None:
    with pytest.raises(api_caller.CallerKeysError):
        api_caller.parse_public_keys(raw)


def test_no_public_keys_is_no_keys() -> None:
    assert api_caller.parse_public_keys("") == {}
    assert api_caller.parse_public_keys("  ") == {}


def test_the_ledger_forgets_only_what_expired() -> None:
    ledger = JtiLedger()
    for i in range(1024):
        ledger.claim(f"old-{i:012d}", exp=100, now=50)
    ledger.claim("live-000000000001", exp=500, now=200)  # prunes the expired ones
    assert ledger.size() == 1
    with pytest.raises(AssertionRefused, match="replayed"):
        ledger.claim("live-000000000001", exp=500, now=200)

    full = JtiLedger(limit=2)
    full.claim("a" * 16, exp=100, now=50)
    full.claim("b" * 16, exp=100, now=50)
    # Full of ids that could still come back: refuse rather than forget one.
    with pytest.raises(AssertionRefused, match="ledger_full"):
        full.claim("c" * 16, exp=500, now=60)
    full.claim("c" * 16, exp=500, now=200)  # a and b have expired
    assert full.size() == 1


# ── the gate ─────────────────────────────────────────────────────────────────


@pytest.fixture
def principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    people_registry.invalidate()
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **kw: None)
    person = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    people_store.upsert_person(full_name="Tia Teammate", email="tia@co.example")
    yield person
    people_registry.invalidate()


@pytest.fixture
def client(principal: int) -> TestClient:
    app = FastAPI()
    app.middleware("http")(api_caller.caller_gate(KEYS))
    app.include_router(executive_route.router)

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str]:
        who = api_caller.caller(request)
        return {"kind": who.kind, "email": who.email}

    return TestClient(app)


def _as(kind: str = "user", email: str = OWNER, method: str = "GET", target: str = "/executive/status") -> dict[str, str]:
    return {"x-caller-assertion": _mint(kind, email, method, target)}


def test_the_gate_sets_who_is_calling(client: TestClient) -> None:
    assert client.get("/whoami", headers=_as(target="/whoami")).json() == {"kind": "user", "email": OWNER}
    assert client.get("/whoami", headers=_as("operator", "", target="/whoami")).json() == {
        "kind": "operator", "email": "",
    }
    assert client.get("/whoami").json() == {"kind": "service", "email": ""}


def test_only_a_signed_owner_may_resume(client: TestClient) -> None:
    assert client.get("/executive/status", headers=_as()).json()["can_resume"] is True
    assert client.get("/executive/status", headers=_as("operator", "")).json()["can_resume"] is True
    tia = _as(email="tia@co.example")
    assert client.get("/executive/status", headers=tia).json()["can_resume"] is False
    # Holding only the shared secret names no one, so it is not the owner.
    assert client.get("/executive/status").json()["can_resume"] is False


def test_the_email_header_alone_is_refused(client: TestClient) -> None:
    resp = client.get("/executive/status", headers={"x-caller-email": OWNER})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized", "code": "caller_assertion_required"}


def test_an_email_header_must_agree_with_the_assertion(client: TestClient) -> None:
    agree = client.get("/executive/status", headers={**_as(), "x-caller-email": "Olivia@Co.Example"})
    assert agree.status_code == 200
    resp = client.get(
        "/executive/status", headers={**_as(email="tia@co.example"), "x-caller-email": OWNER}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "caller_assertion_invalid"


def test_a_write_assertion_is_spent_on_first_use(client: TestClient) -> None:
    headers = _as(method="POST", target="/executive/pause")
    assert client.post("/executive/pause", headers=headers).status_code == 200
    again = client.post("/executive/pause", headers=headers)
    assert again.status_code == 401
    # Nor does it open any other route.
    other = client.post("/executive/resume", headers=_as(method="POST", target="/executive/pause"))
    assert other.status_code == 401


def test_the_query_is_part_of_what_was_signed(client: TestClient) -> None:
    signed = _as(target="/whoami?view=mine")
    assert client.get("/whoami?view=mine", headers=signed).status_code == 200
    assert client.get("/whoami?view=all", headers=_as(target="/whoami?view=mine")).status_code == 401


def test_duplicate_caller_headers_are_refused(client: TestClient) -> None:
    token = _mint(target="/whoami")
    resp = client.get("/whoami", headers=[("x-caller-assertion", token), ("x-caller-assertion", token)])
    assert resp.status_code == 401


def test_preflight_passes_untouched(client: TestClient) -> None:
    assert client.options("/whoami").status_code != 401


def test_the_app_installs_the_gate_and_will_not_boot_on_bad_keys(
    principal: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.main import create_app

    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
    app_client = TestClient(create_app())
    resp = app_client.get("/executive/status", headers={"x-caller-email": OWNER})
    assert resp.status_code == 401
    ok = app_client.get("/executive/status", headers=_as())
    assert ok.status_code == 200 and ok.json()["can_resume"] is True

    monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", "k1:not-a-key")
    with pytest.raises(RuntimeError, match="CALLER_ASSERTION_PUBLIC_KEYS"):
        create_app()


def test_without_keys_nothing_changes(principal: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.api.main import create_app

    for var in ("OE_LOCAL_LOGIN", "OE_PUBLIC_DEPLOYMENT", "BACKEND_SHARED_SECRET", "CALLER_ASSERTION_PUBLIC_KEYS"):
        monkeypatch.delenv(var, raising=False)
    app_client = TestClient(create_app())
    assert app_client.get("/executive/status", headers={"x-caller-email": OWNER}).json()["can_resume"] is True
    assert app_client.get("/executive/status").json()["can_resume"] is True


@pytest.mark.asyncio
async def test_mcp_asks_as_no_one_once_callers_are_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.mcp_server import server as mcp_server

    class _Profile:
        def is_empty(self) -> bool:
            return True

    class _Person:
        id = 42

    chat = AsyncMock(return_value="answer")
    with (
        patch("openexecutive.onboarding.profile_builder.load_or_create_profile", return_value=_Profile()),
        patch("openexecutive.people.store.find_principal_person", return_value=_Person()),
        patch("openexecutive.people.store.find_person_by_email", return_value=_Person()),
        patch("openexecutive.orchestrator.mcp_gateway.get_active_gateway", return_value=None),
        patch("openexecutive.orchestrator.executive.Executive.chat", new=chat),
    ):
        monkeypatch.delenv("CALLER_ASSERTION_PUBLIC_KEYS", raising=False)
        await mcp_server.ask_executive("hi", caller_email=OWNER)
        assert chat.await_args.kwargs["person_id"] == 42
        monkeypatch.setenv("CALLER_ASSERTION_PUBLIC_KEYS", VECTORS["public_keys"])
        await mcp_server.ask_executive("hi", caller_email=OWNER)
        assert chat.await_args.kwargs["person_id"] is None
        await mcp_server.ask_executive("hi")
        assert chat.await_args.kwargs["person_id"] is None
