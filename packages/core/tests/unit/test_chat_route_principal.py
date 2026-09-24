"""The web /chat route must resolve the caller to the principal Person.

Without this, Honcho's prefetch/sync_turn short-circuit with
``outcome=no_person`` and the web UI contributes nothing to peer memory.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    return db_path


class _FakeProfile(CompanyProfile):
    pass


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from openexecutive.onboarding import profile_builder

    def _load_profile() -> CompanyProfile:
        return _FakeProfile()

    monkeypatch.setattr(profile_builder, "load_or_create_profile", _load_profile)

    from openexecutive.knowledge import retriever

    def _retrieve(query: str, specialist_name: Any = None, store: Any = None) -> str:
        return ""

    monkeypatch.setattr(retriever, "retrieve", _retrieve)


def _install_capturing_executive(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    from openexecutive.orchestrator import executive as exec_mod

    class _CapturingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat"] = kwargs
            for piece in ("ok",):
                yield piece

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat_with_committee"] = kwargs
            for piece in ("ok",):
                yield piece

    monkeypatch.setattr(exec_mod, "Executive", _CapturingExecutive)


def test_chat_passes_principal_person_id(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = people_store.upsert_person(full_name="Alex Rivera", role="CEO", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text  # drain SSE

    assert "stream_chat" in captured, "stream_chat was not called"
    assert captured["stream_chat"]["person_id"] == pid


def test_chat_committee_passes_principal_person_id(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = people_store.upsert_person(full_name="Alex", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi", "committee_review": True})
    assert resp.status_code == 200
    _ = resp.text

    assert "stream_chat_with_committee" in captured
    assert captured["stream_chat_with_committee"]["person_id"] == pid


def test_chat_without_principal_passes_none(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No principal row → person_id is None, not a crash."""
    people_store.upsert_person(full_name="Staff", is_principal=False)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["person_id"] is None


# --------------------------------------------------------------------------- #
# x-caller-email header (per-user identity layer)
# --------------------------------------------------------------------------- #


def test_caller_email_header_resolves_to_matching_person(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x-caller-email overrides the principal fallback when it matches a Person."""
    people_store.upsert_person(full_name="Alex", role="CEO", is_principal=True)
    sabin_id = people_store.upsert_person(
        full_name="Sabin Lomac", role="Co-founder", email="sabin@example.com"
    )

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"x-caller-email": "sabin@example.com"},
    )
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["person_id"] == sabin_id


def test_caller_email_case_insensitive(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed-case header values still resolve."""
    people_store.upsert_person(full_name="Alex", is_principal=True)
    sabin_id = people_store.upsert_person(full_name="Sabin", email="sabin@example.com")

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"x-caller-email": "Sabin@Example.COM"},
    )
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["person_id"] == sabin_id


def test_caller_email_unknown_does_not_fall_back_to_principal(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Header present + no roster match → person_id=None.

    Strict precedence prevents cross-identity memory contamination: a
    logged-in user whose email isn't in the roster yet must NOT have
    their turns fused into the operator's peer card. Honcho silently
    skips the turn (outcome=no_person).
    """
    people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"x-caller-email": "stranger@example.com"},
    )
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["person_id"] is None


def test_no_caller_email_header_uses_principal(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backwards-compat: clients that don't send the header still get the principal."""
    principal_id = people_store.upsert_person(full_name="Alex", is_principal=True)

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["person_id"] == principal_id


# --------------------------------------------------------------------------- #
# Session row ownership attribution
# --------------------------------------------------------------------------- #


def _session_owner(session_id: str, db_path: Path) -> int | None:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT caller_person_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return row[0] if row else None


def test_session_row_attributes_caller_person_id_from_header(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session created via /chat is tagged with the resolved caller's Person id."""
    people_store.upsert_person(full_name="Alex", is_principal=True)
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "hi", "session_id": "owned-by-sabin"},
        headers={"x-caller-email": "sabin@example.com"},
    )
    assert resp.status_code == 200
    _ = resp.text

    assert _session_owner("owned-by-sabin", temp_db) == sabin_id


def test_session_row_owner_null_when_header_unknown(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown caller_email → session row gets NULL owner, not the principal.

    Mirrors the no-principal-leak guarantee from Honcho's prefetch: a
    signed-in user whose email isn't in the roster must not have their
    chats attributed to (and shown to) the principal.
    """
    people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )

    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)

    resp = client.post(
        "/chat",
        json={"message": "hi", "session_id": "ghost"},
        headers={"x-caller-email": "stranger@example.com"},
    )
    assert resp.status_code == 200
    _ = resp.text

    assert _session_owner("ghost", temp_db) is None
