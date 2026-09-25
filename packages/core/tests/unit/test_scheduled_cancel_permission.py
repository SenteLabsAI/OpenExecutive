"""Who may cancel a pending scheduled action (DELETE /scheduled/{id}).

The Pulse page's Cancel button reaches the API through the UI proxy, which
cannot send SCHEDULED_ADMIN_TOKEN, so on a deployed server (non-loopback) the
button used to fail. A signed-in user coming through the proxy — proven by the
shared secret plus the caller email the proxy stamps — may now cancel, the same
"stopping is safe" rule the pause switch uses. A caller email on its own proves
nothing, and every successful cancel is audited with who did it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.scheduled import router as scheduled_router
from openexecutive.memory.episodic import (
    get_scheduled_action,
    initialize_db,
    insert_scheduled_action,
)

_REMOTE = ("203.0.113.5", 50000)
_LOOPBACK = ("127.0.0.1", 50000)
_SECRET = "shared-s3cret"


@pytest.fixture(autouse=True)
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "episodic.db"
    initialize_db(db_path)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", db_path)
    # Empty rather than deleted: Settings also reads the repo .env (a
    # developer's may set the token) and the process env wins over it.
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "")
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)


@pytest.fixture(autouse=True)
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit rows instead of writing them to ./episodic_memory.db."""
    calls: list[dict[str, Any]] = []

    def _fake_log_event(event_type: str, summary: str, **kwargs: Any) -> None:
        calls.append({"event_type": event_type, "summary": summary, **kwargs})

    monkeypatch.setattr("openexecutive.audit.log_event", _fake_log_event)
    return calls


def _client(host: tuple[str, int]) -> TestClient:
    app = FastAPI()
    app.include_router(scheduled_router)
    return TestClient(app, client=host)


def _pending_action() -> int:
    return insert_scheduled_action(
        run_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        channel="slack_dm",
        channel_ref="U123",
        intent_text="Chase Dana about the vendor quote",
    )


def _status(action_id: int) -> str:
    row = get_scheduled_action(action_id)
    assert row is not None
    return row.status


def test_signed_in_user_can_cancel_on_a_deployed_server(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    action_id = _pending_action()

    resp = _client(_REMOTE).delete(
        f"/scheduled/{action_id}",
        headers={"x-api-key": _SECRET, "x-caller-email": "Owner@Example.com"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert _status(action_id) == "cancelled"
    assert len(audit_calls) == 1
    assert audit_calls[0]["event_type"] == "scheduled_action_cancelled"
    assert audit_calls[0]["actor"] == "owner@example.com"
    assert audit_calls[0]["details"]["action_id"] == action_id


def test_caller_email_with_a_wrong_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    action_id = _pending_action()

    resp = _client(_REMOTE).delete(
        f"/scheduled/{action_id}",
        headers={"x-api-key": "guess", "x-caller-email": "owner@example.com"},
    )

    assert resp.status_code == 503
    assert _status(action_id) == "pending"
    assert audit_calls == []


def test_caller_email_is_not_trusted_without_a_configured_secret(
    audit_calls: list[dict[str, Any]],
) -> None:
    # With no shared secret anyone who can reach the API could write the header.
    action_id = _pending_action()

    resp = _client(_REMOTE).delete(
        f"/scheduled/{action_id}", headers={"x-caller-email": "owner@example.com"}
    )

    assert resp.status_code == 503
    assert _status(action_id) == "pending"
    assert audit_calls == []


def test_secret_without_a_signed_in_email_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    action_id = _pending_action()

    resp = _client(_REMOTE).delete(
        f"/scheduled/{action_id}", headers={"x-api-key": _SECRET}
    )

    assert resp.status_code == 503
    assert _status(action_id) == "pending"


def test_signed_in_user_can_cancel_even_when_an_admin_token_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "admin-123")
    action_id = _pending_action()

    resp = _client(_REMOTE).delete(
        f"/scheduled/{action_id}",
        headers={"x-api-key": _SECRET, "x-caller-email": "owner@example.com"},
    )

    assert resp.status_code == 200
    assert _status(action_id) == "cancelled"


def test_admin_token_still_cancels_and_is_audited(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("SCHEDULED_ADMIN_TOKEN", "admin-123")
    action_id = _pending_action()
    client = _client(_REMOTE)

    assert client.delete(f"/scheduled/{action_id}").status_code == 401
    assert (
        client.delete(
            f"/scheduled/{action_id}", headers={"X-Admin-Token": "nope"}
        ).status_code
        == 401
    )
    resp = client.delete(
        f"/scheduled/{action_id}", headers={"X-Admin-Token": "admin-123"}
    )

    assert resp.status_code == 200
    assert _status(action_id) == "cancelled"
    assert [c["actor"] for c in audit_calls] == ["admin-token"]


def test_loopback_without_an_admin_token_still_cancels(
    audit_calls: list[dict[str, Any]],
) -> None:
    action_id = _pending_action()

    resp = _client(_LOOPBACK).delete(f"/scheduled/{action_id}")

    assert resp.status_code == 200
    assert _status(action_id) == "cancelled"
    assert [c["actor"] for c in audit_calls] == ["local"]


def test_already_finished_action_is_not_audited_as_cancelled(
    monkeypatch: pytest.MonkeyPatch, audit_calls: list[dict[str, Any]]
) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", _SECRET)
    action_id = _pending_action()
    headers = {"x-api-key": _SECRET, "x-caller-email": "owner@example.com"}
    client = _client(_REMOTE)

    assert client.delete(f"/scheduled/{action_id}", headers=headers).status_code == 200
    assert client.delete(f"/scheduled/{action_id}", headers=headers).status_code == 409
    assert len(audit_calls) == 1
