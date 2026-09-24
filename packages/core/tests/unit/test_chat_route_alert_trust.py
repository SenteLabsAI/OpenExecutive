"""The web chat route must record which alert ids it showed the model.

`ack_alert` accepts only ids in `session.trusted_alert_ids`. The route is the
only thing that populates them for a browser turn, so if this wiring breaks,
the failure is silent in both directions: the Executive can no longer clear a
card the principal just approved, and (before the guard covered web at all)
it could clear any id an inbound email had written into an alert body.

Unit-level tests of `render_and_trust` cannot catch that — they pass a session
in directly. This drives the real route.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.alerts import store as alert_store
from openexecutive.api.routes import chat as chat_route
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    monkeypatch.setattr(alert_store, "DB_PATH", tmp_path / "alerts.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    alert_store.initialize_db(tmp_path / "alerts.db")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())

    from openexecutive.knowledge import retriever as retriever_mod

    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "")

    from openexecutive.orchestrator import executive as exec_mod

    class _StubExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

        async def stream_chat_with_committee(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _StubExecutive)
    return tmp_path


def _post(message: str = "what's on my plate?") -> None:
    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": message})
    assert resp.status_code == 200
    _ = resp.text  # drain the SSE stream so the turn completes


def _only_session() -> Any:
    assert len(chat_route._sessions) == 1
    return next(iter(chat_route._sessions.values()))


def test_web_turn_trusts_the_live_alert_it_was_shown(env: Path) -> None:
    people_store.upsert_person(full_name="Alex", is_principal=True)
    live = alert_store.insert_alert(
        source="email", external_id="live-1", severity="high",
        headline="Approve the Q3 budget", body="Needs a decision.",
        db_path=env / "alerts.db",
    )
    assert live is not None

    _post()

    assert _only_session().trusted_alert_ids == {live}


def test_web_turn_does_not_trust_a_closed_alert(env: Path) -> None:
    """A dismissed alert is named in the digest's handled tail so the Executive
    knows it is settled — but it must not become ackable."""
    people_store.upsert_person(full_name="Alex", is_principal=True)
    closed = alert_store.insert_alert(
        source="email", external_id="closed-1", severity="high",
        headline="St. Albans reconciliation gap", body="b",
        db_path=env / "alerts.db",
    )
    assert closed is not None
    alert_store.set_status(closed, "dismissed", db_path=env / "alerts.db")

    _post()

    assert _only_session().trusted_alert_ids == set()


def test_web_turn_with_no_alerts_trusts_nothing(env: Path) -> None:
    people_store.upsert_person(full_name="Alex", is_principal=True)

    _post()

    assert _only_session().trusted_alert_ids == set()


def test_turn_start_clears_a_stale_trusted_set(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that never reaches the recorder must not inherit the last one's set.

    Web sessions are long-lived, so if `render_and_trust` is skipped — the
    to_thread wrapper fails to schedule, the gather is cancelled — the previous
    turn's ids would still be sitting on the session and `ack_alert` would
    accept them. The clear happens before the gather, so it holds regardless.
    """
    people_store.upsert_person(full_name="Alex", is_principal=True)
    live = alert_store.insert_alert(
        source="email", external_id="live-1", severity="high",
        headline="Approve the Q3 budget", body="b", db_path=env / "alerts.db",
    )
    assert live is not None

    _post()
    assert _only_session().trusted_alert_ids == {live}

    # Same session, next turn, digest unavailable.
    from openexecutive.briefing import context as ctx

    def _boom(*_a: object, **_kw: object) -> str:
        raise RuntimeError("store down")

    monkeypatch.setattr(ctx, "render_and_trust", _boom)
    session_id = _only_session().session_id

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "and now?", "session_id": session_id})
    assert resp.status_code == 200
    _ = resp.text

    assert _only_session().trusted_alert_ids == set()


# --------------------------------------------------------------------- #
# What `ack_alert` actually reads
#
# Every test above asserts `session.trusted_alert_ids` — the attribute. That
# attribute was correct all along. `ack_alert` does not read it directly; it
# reads `current_session.get()`, and on the SSE path that returned None for
# every step after the first, so the tool fell back to an empty set and
# refused every ack. Asserting the attribute cannot see that gap. These drive
# the real route and assert what the tool resolves, from a LATER stream step.
# --------------------------------------------------------------------- #


def _post_with_tool_call_on_step(
    step: int, tool_input: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive the real route with a stub that calls `ack_alert` mid-stream.

    `step` is the 0-based yield index the tool call is made from. Step 0 is
    the only one a `current_session.set()` inside the executive's own
    generator survives to, so `step >= 1` is what reproduces production.
    """
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.orchestrator.schedule_tools import (
        current_session,
        handle_ack_alert,
    )

    results: list[str] = []

    class _ToolCallingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            # Faithful to the real `Executive.stream_chat`, which binds the
            # session with a bare `set()` at its top. That is what makes the
            # bug step-dependent: this binding survives into step 0 and is
            # discarded with the per-step Task that made it, so step 1 onward
            # sees None unless the ROUTE bound it from outside.
            current_session.set(kwargs.get("session"))
            for i in range(step + 2):
                if i == step:
                    results.append(await handle_ack_alert(tool_input))
                yield f"chunk-{i}"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            async for chunk in self.stream_chat(**kwargs):
                yield chunk

    monkeypatch.setattr(exec_mod, "Executive", _ToolCallingExecutive)
    _post()
    return results


@pytest.mark.parametrize("step", [0, 1, 3])
def test_ack_alert_resolves_the_session_on_every_stream_step(
    step: int, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression. `_sse_body` drives the executive with
    `asyncio.wait_for(stream.__anext__())`, which runs each step in a fresh
    Task holding a *copy* of the context — so a bare `current_session.set()`
    inside the executive's generator is gone by step 1. The tool-call loop
    runs on those later steps, so on the live tenant every ack the principal
    asked for was refused with "not among the open items you were shown this
    turn", on a card that was open the whole time.

    Parametrized over the step deliberately: a test that only ever acked on
    step 0 passes against the bug.
    """
    people_store.upsert_person(full_name="Alex", is_principal=True)
    live = alert_store.insert_alert(
        source="email", external_id="live-1", severity="high",
        headline="Approve the Q3 budget", body="Needs a decision.",
        db_path=env / "alerts.db",
    )
    assert live is not None

    results = _post_with_tool_call_on_step(
        step, {"alert_id": live, "status": "dismissed"}, monkeypatch
    )

    assert len(results) == 1
    assert "error" not in results[0], (
        f"ack on stream step {step} was refused: {results[0]}"
    )
    refreshed = alert_store.get_alert(live, db_path=env / "alerts.db")
    assert refreshed is not None and refreshed.status == "dismissed"


def test_a_closed_alert_is_still_refused_from_a_later_step(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix restores reach, it must not widen it: binding the session for
    the whole stream makes the trusted set visible, not permissive."""
    people_store.upsert_person(full_name="Alex", is_principal=True)
    closed = alert_store.insert_alert(
        source="email", external_id="closed-1", severity="high",
        headline="Already handled", body="b", db_path=env / "alerts.db",
    )
    assert closed is not None
    alert_store.set_status(closed, "dismissed", db_path=env / "alerts.db")

    results = _post_with_tool_call_on_step(
        2, {"alert_id": closed, "status": "ack"}, monkeypatch
    )

    assert len(results) == 1
    assert "error" in results[0]


def test_an_invented_id_is_still_refused_from_a_later_step(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    people_store.upsert_person(full_name="Alex", is_principal=True)
    alert_store.insert_alert(
        source="email", external_id="live-1", severity="high",
        headline="Approve the Q3 budget", body="b", db_path=env / "alerts.db",
    )

    results = _post_with_tool_call_on_step(
        2, {"alert_id": 9999, "status": "dismissed"}, monkeypatch
    )

    assert len(results) == 1
    assert "error" in results[0]
