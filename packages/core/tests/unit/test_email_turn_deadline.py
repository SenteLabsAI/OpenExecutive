"""The email poller's turn deadline (EMAIL_TURN_TIMEOUT_S) and give-up rule.

The poller handles mail one message at a time. A turn with no ceiling held
every message behind it, and a message whose handling kept raising was tried
again every poll for the life of the process. Now a turn past the ceiling is
cut off, recorded, and its message marked read; a message that fails
``_MAX_ATTEMPTS`` times is given up and marked read.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import openexecutive.integrations.email_poller as poller
from openexecutive.integrations.workspace.mail import MessageRef
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

EXEC = "exec@example.com"
TEAMMATE = "alice@example.com"


@pytest.fixture(autouse=True)
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email="olivia@example.com")
    people_store.upsert_person(full_name="Alice", email=TEAMMATE)
    people_registry.invalidate()
    poller._processed_ids.clear()
    poller._failed_attempts.clear()
    # Audited code: keep its rows out of the shared default DB.
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)


def _settings(timeout_s: float = 0.05) -> Any:
    return SimpleNamespace(
        exec_email_address=EXEC, email_poll_interval_seconds=60, email_turn_timeout_s=timeout_s,
    )


class _SlowExecutive:
    cancelled = False

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def chat(self, **_kwargs: Any) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            _SlowExecutive.cancelled = True
            raise
        return "never"


def _run_executive_patches() -> Any:
    return (
        patch("openexecutive.orchestrator.executive.Executive", new=_SlowExecutive),
        patch(
            "openexecutive.onboarding.profile_builder.load_or_create_profile",
            return_value=SimpleNamespace(is_empty=lambda: True),
        ),
        patch("openexecutive.knowledge.retriever.retrieve", new=lambda **_k: ""),
        patch("openexecutive.memory.episodic.format_for_prompt", new=lambda: ""),
    )


def test_a_turn_past_the_ceiling_is_cut_off() -> None:
    _SlowExecutive.cancelled = False
    a, b, c, d = _run_executive_patches()
    with (
        a, b, c, d,
        patch.object(poller, "get_settings", return_value=_settings()),
        pytest.raises(poller.EmailTurnTimeout) as info,
    ):
        asyncio.run(asyncio.wait_for(
            poller._run_executive(
                gateway=AsyncMock(),
                raw_email=f"Subject: hi\nFrom: {TEAMMATE}\n\n--- BODY ---\nhello\n",
                message_id="m1",
                thread_id="t1",
                from_addr=TEAMMATE,
            ),
            timeout=5,
        ))
    assert info.value.timeout_s == 0.05
    assert _SlowExecutive.cancelled is True


def test_a_timeout_from_inside_the_turn_is_not_reported_as_the_deadline() -> None:
    """Only the turn's own deadline reads as EmailTurnTimeout."""
    class _Raises:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def chat(self, **_kwargs: Any) -> str:
            raise TimeoutError("something inside")

    _a, b, c, d = _run_executive_patches()
    with (
        patch("openexecutive.orchestrator.executive.Executive", new=_Raises),
        b, c, d,
        patch.object(poller, "get_settings", return_value=_settings(timeout_s=5)),
        pytest.raises(TimeoutError) as info,
    ):
        asyncio.run(poller._run_executive(
            gateway=AsyncMock(),
            raw_email=f"Subject: hi\nFrom: {TEAMMATE}\n\n--- BODY ---\nhello\n",
            message_id="m1",
            thread_id="t1",
            from_addr=TEAMMATE,
        ))
    assert not isinstance(info.value, poller.EmailTurnTimeout)


def test_a_cut_off_turn_is_recorded_and_its_message_marked_read() -> None:
    rows: list[dict[str, Any]] = []
    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(
        return_value=f"Subject: Pilot\nFrom: {TEAMMATE}\nTo: {EXEC}\n\n--- BODY ---\nhello\n"
    )
    mark_read = AsyncMock()
    with (
        patch(
            "openexecutive.audit.log_event",
            side_effect=lambda event_type, summary, **kw: rows.append(
                {"event_type": event_type, "summary": summary, **kw}
            ),
        ),
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_run_executive", new=AsyncMock(side_effect=poller.EmailTurnTimeout(0.05))),
        patch.object(poller, "_mark_read", new=mark_read),
    ):
        asyncio.run(poller._handle_email(gateway, "m1", "t1", EXEC))

    cut = [r for r in rows if r["details"].get("outcome") == "turn_timeout"]
    assert len(cut) == 1
    assert cut[0]["details"]["message_id"] == "m1"
    assert cut[0]["details"]["from"] == TEAMMATE
    assert cut[0]["session_id"] == "email:t1"
    mark_read.assert_awaited_once()


class _Provider:
    name = "google"

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    async def list_unread(self, _gateway: Any, _email: str, _n: int) -> list[MessageRef]:
        return [MessageRef(i, f"t-{i}") for i in self.ids]


def test_a_message_that_keeps_failing_is_given_up_after_three_tries() -> None:
    provider = _Provider(["bad", "good"])
    handled: list[str] = []

    async def handle(_gw: Any, mid: str, *_a: Any, **_k: Any) -> None:
        handled.append(mid)
        if mid == "bad":
            raise RuntimeError("boom")

    mark_read = AsyncMock()
    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_handle_email", new=handle),
        patch.object(poller, "_mark_read", new=mark_read),
    ):
        for _ in range(4):
            asyncio.run(poller.poll_once(AsyncMock(), provider))  # type: ignore[arg-type]

    # The good one ran once, after the bad one, on the first poll.
    assert handled.count("good") == 1
    assert handled.count("bad") == poller._MAX_ATTEMPTS == 3
    assert "bad" in poller._processed_ids
    assert "bad" not in poller._failed_attempts
    mark_read.assert_awaited_once()
    assert mark_read.await_args.args[1] == "bad"


def test_a_message_that_succeeds_on_retry_forgets_its_failures() -> None:
    provider = _Provider(["flaky"])
    calls = {"n": 0}

    async def handle(*_a: Any, **_k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")

    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_handle_email", new=handle),
        patch.object(poller, "_mark_read", new=AsyncMock()),
    ):
        asyncio.run(poller.poll_once(AsyncMock(), provider))  # type: ignore[arg-type]
        assert poller._failed_attempts == {"flaky": 1}
        asyncio.run(poller.poll_once(AsyncMock(), provider))  # type: ignore[arg-type]
    assert poller._failed_attempts == {}
    assert "flaky" in poller._processed_ids


def test_the_cut_off_row_says_a_reply_may_have_gone() -> None:
    rows: list[dict[str, Any]] = []
    gateway = AsyncMock()
    gateway.call_tool = AsyncMock(
        return_value=f"Subject: Pilot\nFrom: {TEAMMATE}\nTo: {EXEC}\n\n--- BODY ---\nhello\n"
    )
    with (
        patch(
            "openexecutive.audit.log_event",
            side_effect=lambda event_type, summary, **kw: rows.append({"summary": summary, **kw}),
        ),
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_run_executive", new=AsyncMock(side_effect=poller.EmailTurnTimeout(0.05))),
        patch.object(poller, "_mark_read", new=AsyncMock()),
    ):
        asyncio.run(poller._handle_email(gateway, "m1", "t1", EXEC))
    cut = next(r for r in rows if r["details"].get("outcome") == "turn_timeout")
    assert "may already have gone" in cut["summary"]
