"""Unit tests for the department-keyed Honcho client surfaces.

Mirrors ``test_honcho_client`` for the person-keyed wrapper but covers
``prefetch_department`` / ``sync_department_turn`` / ``append_department_note``.
Same fake-client pattern — no real Honcho server required.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from openexecutive.memory import honcho_client

# --------------------------------------------------------------------------- #
# Fakes (mirror the shape used in test_honcho_client.py)
# --------------------------------------------------------------------------- #


class _FakeAioPeer:
    def __init__(self, last_call: dict[str, Any], answer: str | None = "dept memory") -> None:
        self._last = last_call
        self._answer = answer

    async def chat(
        self,
        query: str,
        *,
        target: Any = None,
        session: Any = None,
        reasoning_level: str | None = None,
    ) -> str | None:
        self._last["chat"] = {
            "query": query,
            "session": session,
            "target": target,
            "reasoning_level": reasoning_level,
        }
        return self._answer


class _FakePeer:
    def __init__(self, aio: _FakeAioPeer, peer_id: str = "") -> None:
        self.aio = aio
        self.peer_id = peer_id

    def message(self, content: str) -> dict[str, Any]:
        return {"content": content, "from": self.peer_id}


class _FakeAioSession:
    def __init__(self, last_call: dict[str, Any]) -> None:
        self._last = last_call

    async def add_peers(self, peers: list[_FakePeer]) -> None:
        self._last.setdefault("added_peers", []).extend(
            getattr(p, "peer_id", "") for p in peers
        )

    async def add_messages(self, msgs: list[dict[str, Any]]) -> None:
        self._last.setdefault("add_messages", []).extend(msgs)


class _FakeSession:
    def __init__(self, aio: _FakeAioSession) -> None:
        self.aio = aio


class _FakeAio:
    def __init__(self, last_call: dict[str, Any], answer: str | None = "dept memory") -> None:
        self._last = last_call
        self._answer = answer

    async def peer(self, peer_id: str) -> _FakePeer:
        self._last.setdefault("peers", []).append(peer_id)
        return _FakePeer(_FakeAioPeer(self._last, self._answer), peer_id=peer_id)

    async def session(self, session_id: str) -> _FakeSession:
        self._last.setdefault("sessions", []).append(session_id)
        return _FakeSession(_FakeAioSession(self._last))


class _FakeClient:
    def __init__(self, last_call: dict[str, Any], answer: str | None = "dept memory") -> None:
        self.aio = _FakeAio(last_call, answer)


def _patched_client(fake: Any) -> Any:
    async def _f() -> Any:
        return fake

    return patch.object(honcho_client, "_get_client", _f)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONCHO_ENABLED", "true")
    monkeypatch.setenv("HONCHO_API_KEY", "test-key")
    monkeypatch.setenv("HONCHO_BASE_URL", "http://localhost:8000")


@pytest.fixture(autouse=True)
def _reset_client_singleton() -> Generator[None, None, None]:
    honcho_client.reset_client_for_tests()
    yield
    honcho_client.reset_client_for_tests()


# --------------------------------------------------------------------------- #
# _department_peer_id
# --------------------------------------------------------------------------- #


def test_department_peer_id_basic() -> None:
    assert honcho_client._department_peer_id("marketing") == "department_marketing"


def test_department_peer_id_hyphenated_slug() -> None:
    # Hyphens are allowed in Honcho ids, so dept slugs like
    # "marketing-and-sales" should pass through unchanged.
    assert (
        honcho_client._department_peer_id("marketing-and-sales")
        == "department_marketing-and-sales"
    )


def test_department_peer_id_sanitizes_bad_chars() -> None:
    # Defensive: an out-of-charset slug (future CSV import, manual edit)
    # gets sanitized rather than 422-ing the wrapper at runtime.
    assert (
        honcho_client._department_peer_id("legal/finance")
        == "department_legal_finance"
    )


# --------------------------------------------------------------------------- #
# prefetch_department
# --------------------------------------------------------------------------- #


def test_prefetch_department_disabled_returns_empty() -> None:
    result = asyncio.run(
        honcho_client.prefetch_department("q", department_slug="marketing")
    )
    assert result == ""


def test_prefetch_department_no_slug_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    result = asyncio.run(
        honcho_client.prefetch_department("q", department_slug=None)
    )
    assert result == ""


def test_prefetch_department_returns_chat_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last, answer="Marketing has been deprioritizing enterprise.")
    with _patched_client(fake):
        result = asyncio.run(
            honcho_client.prefetch_department(
                "what's our enterprise stance?",
                department_slug="marketing",
                session_id="slack:C1:t1",
            )
        )
    assert result == "Marketing has been deprioritizing enterprise."
    # Peer resolved against the dept-prefixed id, not a raw slug.
    assert "department_marketing" in last["peers"]
    # No session= passed to chat() — we want the dept peer's global rep.
    assert last["chat"]["session"] is None
    assert last["chat"]["reasoning_level"] == "low"


def test_prefetch_department_honours_reasoning_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last, answer="deep dept answer")
    with _patched_client(fake):
        result = asyncio.run(
            honcho_client.prefetch_department(
                "deep dive",
                department_slug="finance",
                reasoning_level="high",
            )
        )
    assert result == "deep dept answer"
    assert last["chat"]["reasoning_level"] == "high"


def test_prefetch_department_swallows_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    class _BoomClient:
        class _Aio:
            async def peer(self, peer_id: str) -> Any:  # noqa: ARG002
                raise RuntimeError("boom")

        aio = _Aio()

    with _patched_client(_BoomClient()):
        result = asyncio.run(
            honcho_client.prefetch_department("q", department_slug="hr")
        )
    assert result == ""


def test_prefetch_department_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv("HONCHO_PREFETCH_TIMEOUT_S", "0.05")

    class _SlowAioPeer:
        async def chat(self, *args: Any, **kwargs: Any) -> str:  # noqa: ARG002
            await asyncio.sleep(5)
            return "never"

    class _SlowAio:
        async def peer(self, peer_id: str) -> Any:  # noqa: ARG002
            return _FakePeer(_SlowAioPeer())  # type: ignore[arg-type]

    class _SlowClient:
        aio = _SlowAio()

    with _patched_client(_SlowClient()):
        result = asyncio.run(
            honcho_client.prefetch_department("q", department_slug="ops")
        )
    assert result == ""


def test_prefetch_department_disabled_audits_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv("HONCHO_ENABLED", "false")
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        asyncio.run(
            honcho_client.prefetch_department("q", department_slug="marketing")
        )
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert len(peer_rows) == 1
    details = peer_rows[0]["details"]
    assert details["op"] == "prefetch_department"
    assert details["outcome"] == "disabled"
    assert details["department_slug"] == "marketing"


def test_sync_department_turn_disabled_schedules_nothing_and_audits_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HONCHO_ENABLED=false: sync_department_turn short-circuits — no task is
    scheduled and one peer_memory audit row lands with outcome=disabled.
    Full-parity guard so the off path can't silently regress."""
    monkeypatch.setenv("HONCHO_ENABLED", "false")
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        honcho_client.sync_department_turn(
            "hi", "hello", department_slug="marketing", session_id="s1",
            originating_person_id=7,
        )

    assert not honcho_client._pending_sync_tasks
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert len(peer_rows) == 1
    assert peer_rows[0]["details"]["op"] == "sync_department_turn"
    assert peer_rows[0]["details"]["outcome"] == "disabled"
    assert peer_rows[0]["details"]["department_slug"] == "marketing"


def test_prefetch_department_no_slug_audits_no_department(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        asyncio.run(honcho_client.prefetch_department("q", department_slug=None))
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert len(peer_rows) == 1
    assert peer_rows[0]["details"]["outcome"] == "no_department"


def test_prefetch_department_ok_audits_with_dept_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last, answer="something useful")
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with _patched_client(fake), patch.object(honcho_client, "audit_log", _spy):
        asyncio.run(
            honcho_client.prefetch_department(
                "q",
                department_slug="finance",
                reasoning_level="medium",
            )
        )
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert len(peer_rows) == 1
    d = peer_rows[0]["details"]
    assert d["op"] == "prefetch_department"
    assert d["outcome"] == "ok"
    assert d["department_slug"] == "finance"
    assert d["reasoning_level"] == "medium"
    assert d["response_chars"] == len("something useful")
    assert "duration_ms" in d


# --------------------------------------------------------------------------- #
# sync_department_turn
# --------------------------------------------------------------------------- #


def test_sync_department_turn_noop_without_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    # Must not raise even without a running loop.
    honcho_client.sync_department_turn(
        "u", "a", department_slug=None, session_id="s"
    )


def test_sync_department_turn_writes_to_scoped_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "Hi marketing",
                "Marketing-relevant response",
                department_slug="marketing",
                session_id="slack:C9:t2",
                originating_person_id=11,
                co_present_person_ids=[22, 11],  # 11 = originator → deduped
                co_present_department_slugs=["finance"],
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    # Session id is dept-scoped and sanitized (colons → underscores).
    assert last["sessions"] == ["dept-marketing-slack_C9_t2"]
    # Dept peer + originator + extra co-present + other dept + executive.
    assert "department_marketing" in last["peers"]
    assert "executive" in last["peers"]
    assert "11" in last["peers"]
    assert "22" in last["peers"]
    assert "department_finance" in last["peers"]
    # The dept peer must be added explicitly (it authors no message
    # here, so Honcho wouldn't implicitly attach it to the session).
    # Originator and other co-present extras follow.
    added = last.get("added_peers") or []
    assert added[0] == "department_marketing"
    assert set(added) == {"department_marketing", "11", "22", "department_finance"}
    # Inbound is authored by the originating person (not the dept peer),
    # so future peer.chat() on the dept peer doesn't paraphrase user
    # words back as if the dept held those views.
    msgs = last.get("add_messages") or []
    assert len(msgs) == 2
    assert msgs[0]["from"] == "11"
    assert msgs[0]["content"] == "Hi marketing"
    assert msgs[1]["from"] == "executive"
    assert not honcho_client._pending_sync_tasks


def test_sync_department_turn_drops_inbound_when_no_originator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No originating_person_id → no peer to author user_message against.
    We must drop the inbound rather than misattribute it to the dept
    peer (which would pollute the dept's representation extraction)."""
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "Some user message",
                "Assistant response",
                department_slug="ops",
                session_id="s1",
                # originating_person_id intentionally omitted
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    msgs = last.get("add_messages") or []
    # Only the assistant_response should land; the inbound is dropped.
    assert len(msgs) == 1
    assert msgs[0]["from"] == "executive"
    # No person peer was resolved.
    peers = last.get("peers", [])
    assert not any(p.isdigit() for p in peers)


def test_sync_department_turn_session_fallback_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When session_id is None, the dept session id falls back to a
    stable per-dept slug. This is intentional — the originating turn
    has no thread to scope by, so all such turns land in one bucket."""
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "u",
                "a",
                department_slug="legal",
                session_id=None,
                originating_person_id=5,
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    assert last["sessions"] == ["dept-legal"]


def test_sync_department_turn_empty_messages_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both messages empty → audit 'empty' and skip the SDK entirely."""
    _enable(monkeypatch)
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        honcho_client.sync_department_turn(
            "",
            "   ",
            department_slug="hr",
            session_id="s",
        )
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert peer_rows[0]["details"]["outcome"] == "empty"
    # No task scheduled — the SDK is never touched.
    assert not honcho_client._pending_sync_tasks


def test_sync_department_turn_copresent_list_safe_to_mutate_after_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper snapshots the co-present list so caller mutation
    after return can't corrupt the in-flight task's peer additions."""
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)
    co_present = [33, 44]

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "u",
                "a",
                department_slug="finance",
                session_id="s1",
                originating_person_id=11,
                co_present_person_ids=co_present,
            )
            # Mutate the caller's list before the background task runs.
            co_present.append(99)
            co_present.clear()
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    # The original 33 and 44 must still appear in peer additions.
    added = last.get("added_peers") or []
    assert "33" in added
    assert "44" in added
    assert "99" not in added


def test_sync_department_turn_excludes_self_from_copresent_depts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owning dept is always added to its own session (it authors no
    message so Honcho wouldn't otherwise attach it), but it must appear
    exactly once even when the caller fumbles and lists it again in
    co_present_department_slugs."""
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "Hi",
                "Hello",
                department_slug="marketing",
                session_id="s1",
                co_present_department_slugs=["marketing", "finance"],
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    added = last.get("added_peers") or []
    # Marketing appears exactly once (as the owner-add), not twice via
    # the co-present list. Finance is added as a separate co-present.
    assert added.count("department_marketing") == 1
    assert "department_finance" in added


def test_sync_department_turn_safe_from_sync_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    # No asyncio.run wrapper — must not raise.
    honcho_client.sync_department_turn(
        "u", "a", department_slug="finance", session_id="s"
    )


# --------------------------------------------------------------------------- #
# append_department_note
# --------------------------------------------------------------------------- #


def test_append_department_note_no_slug_audits_no_department(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        honcho_client.append_department_note(
            department_slug=None, kind="decision", body="something"
        )
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert len(peer_rows) == 1
    assert peer_rows[0]["details"]["outcome"] == "no_department"


def test_append_department_note_empty_body_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        captured.append({"event_type": event_type, "summary": summary, **kwargs})

    with patch.object(honcho_client, "audit_log", _spy):
        honcho_client.append_department_note(
            department_slug="marketing", kind="decision", body="   "
        )
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    assert peer_rows[0]["details"]["outcome"] == "empty"


def test_append_department_note_writes_tagged_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            honcho_client.append_department_note(
                department_slug="finance",
                kind="decision",
                body="Slow hiring through Q3; runway is the binding constraint.",
                person_id=42,
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    # Single notes-session per dept, regardless of kind.
    assert last["sessions"] == ["dept-finance-notes"]
    # Dept peer + executive + person co-presence.
    assert "department_finance" in last["peers"]
    assert "executive" in last["peers"]
    assert "42" in last["peers"]
    # The dept peer itself must be added to the notes session so its
    # representation extraction picks up the notes. Person follows when
    # given.
    assert last.get("added_peers") == ["department_finance", "42"]
    msgs = last.get("add_messages") or []
    assert len(msgs) == 1
    assert msgs[0]["from"] == "executive"
    # Kind is inlined into the body so the dept peer's representation
    # extraction can later distinguish committee_decision from
    # goal_update without a side-channel.
    assert msgs[0]["content"].startswith("[decision] ")
    assert "Slow hiring through Q3" in msgs[0]["content"]


def test_append_department_note_disabled_skips_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # HONCHO_ENABLED defaults to false; the scheduler call should be
    # short-circuited entirely (no task scheduled).
    honcho_client.append_department_note(
        department_slug="marketing", kind="goal_update", body="Q3 target lowered to 150 enterprise leads"
    )
    # No tasks should have been scheduled.
    assert not honcho_client._pending_sync_tasks


def test_append_department_note_safe_from_sync_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    # No asyncio.run wrapper — must not raise.
    honcho_client.append_department_note(
        department_slug="marketing", kind="advice", body="Try inbound channels"
    )


# --------------------------------------------------------------------------- #
# Audit ContextVar propagation across the fire-and-forget boundary
#
# Each scheduling function snapshots the audit (session_id, turn_id) at
# call time via ``get_active_ids()`` and re-binds them inside the
# background coroutine via ``with set_turn(...)``. Without this, the
# parent's ``with set_turn(...)`` block in stream_chat() has already
# exited by the time the task runs, leaving every emitted ``peer_memory``
# row with ``session_id=None`` and invisible in the per-session audit
# view at ``/audit/sessions/{sid}``.
# --------------------------------------------------------------------------- #


def _capture_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on the audit_log call site inside honcho_client so we can
    assert what session_id/turn_id landed on the row. The real audit
    logger pulls those from the ContextVars; we use the ContextVar
    directly here to mimic that read."""
    from openexecutive.audit.context import get_active_ids

    captured: list[dict[str, Any]] = []

    def _spy(event_type: str, summary: str, **kwargs: Any) -> None:
        sid, tid = get_active_ids()
        captured.append({
            "event_type": event_type,
            "summary": summary,
            "ctx_session_id": sid,
            "ctx_turn_id": tid,
            **kwargs,
        })

    monkeypatch.setattr(honcho_client, "audit_log", _spy)
    return captured


def test_sync_department_turn_audit_row_carries_snapshotted_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent's set_turn block exits before the background task runs,
    so without snapshotting, ctx_session_id would be None. This test
    locks down that the snapshot survives the create_task hop."""
    _enable(monkeypatch)
    from openexecutive.audit.context import set_turn

    captured = _capture_audit(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        # _patched_client wraps the entire body — the background task
        # fires AFTER set_turn exits, so the patch must outlive it.
        # The audit-ContextVar snapshot is what we're testing.
        with _patched_client(fake):
            with set_turn(session_id="sess-abc", turn_id="t-xyz"):
                honcho_client.sync_department_turn(
                    "u",
                    "a",
                    department_slug="finance",
                    session_id="sess-abc",
                    originating_person_id=11,
                )
            # Out here, ContextVars are back to None — but the snapshot
            # we took inside the with block must still drive the audit row.
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    ok_rows = [r for r in peer_rows if r["details"]["outcome"] == "ok"]
    # A seed_identity row may follow the persist row. Every row a sync emits
    # must carry the same snapshotted ids, so check they all agree, then
    # inspect the persist row itself.
    assert len({(r["ctx_session_id"], r["ctx_turn_id"]) for r in ok_rows}) == 1
    assert len([r for r in ok_rows if r["details"]["op"] == "seed_identity"]) <= 1
    sync_rows = [r for r in ok_rows if r["details"]["op"] != "seed_identity"]
    assert len(sync_rows) == 1
    row = sync_rows[0]
    assert row["ctx_session_id"] == "sess-abc"
    assert row["ctx_turn_id"] == "t-xyz"


def test_sync_turn_audit_row_carries_snapshotted_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same propagation requirement for the person-level sync_turn —
    parent's set_turn exits before the background task runs; the
    snapshot must survive."""
    _enable(monkeypatch)
    from openexecutive.audit.context import set_turn

    captured = _capture_audit(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            with set_turn(session_id="sess-person", turn_id="t-person"):
                honcho_client.sync_turn(
                    "u",
                    "a",
                    person_id=42,
                    session_id="sess-person",
                )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    ok_rows = [r for r in peer_rows if r["details"]["outcome"] == "ok"]
    # A seed_identity row may follow the persist row. Every row a sync emits
    # must carry the same snapshotted ids, so check they all agree, then
    # inspect the persist row itself.
    assert len({(r["ctx_session_id"], r["ctx_turn_id"]) for r in ok_rows}) == 1
    assert len([r for r in ok_rows if r["details"]["op"] == "seed_identity"]) <= 1
    sync_rows = [r for r in ok_rows if r["details"]["op"] != "seed_identity"]
    assert len(sync_rows) == 1
    row = sync_rows[0]
    assert row["ctx_session_id"] == "sess-person"
    assert row["ctx_turn_id"] == "t-person"


def test_append_department_note_audit_row_carries_snapshotted_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    from openexecutive.audit.context import set_turn

    captured = _capture_audit(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        with _patched_client(fake):
            with set_turn(session_id="sess-note", turn_id="t-note"):
                honcho_client.append_department_note(
                    department_slug="finance",
                    kind="decision",
                    body="something memorable",
                )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    ok_rows = [r for r in peer_rows if r["details"]["outcome"] == "ok"]
    # A seed_identity row may follow the persist row. Every row a sync emits
    # must carry the same snapshotted ids, so check they all agree, then
    # inspect the persist row itself.
    assert len({(r["ctx_session_id"], r["ctx_turn_id"]) for r in ok_rows}) == 1
    assert len([r for r in ok_rows if r["details"]["op"] == "seed_identity"]) <= 1
    sync_rows = [r for r in ok_rows if r["details"]["op"] != "seed_identity"]
    assert len(sync_rows) == 1
    row = sync_rows[0]
    assert row["ctx_session_id"] == "sess-note"
    assert row["ctx_turn_id"] == "t-note"


def test_snapshot_is_none_when_called_outside_set_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the scheduling site has no active turn, the snapshot is None
    and the audit row still emits cleanly (no crash, no spurious IDs).
    Models the case of a workflow runner or CLI tool that hasn't
    wrapped its call in set_turn."""
    _enable(monkeypatch)
    captured = _capture_audit(monkeypatch)
    last: dict[str, Any] = {}
    fake = _FakeClient(last)

    async def runner() -> None:
        # No set_turn wrapper at all.
        with _patched_client(fake):
            honcho_client.sync_department_turn(
                "u",
                "a",
                department_slug="finance",
                session_id="sess-abc",
                originating_person_id=11,
            )
            pending = list(honcho_client._pending_sync_tasks)
            if pending:
                await asyncio.gather(*pending)

    asyncio.run(runner())
    peer_rows = [r for r in captured if r["event_type"] == "peer_memory"]
    ok_rows = [r for r in peer_rows if r["details"]["outcome"] == "ok"]
    # A seed_identity row may follow the persist row. Every row a sync emits
    # must carry the same snapshotted ids, so check they all agree, then
    # inspect the persist row itself.
    assert len({(r["ctx_session_id"], r["ctx_turn_id"]) for r in ok_rows}) == 1
    assert len([r for r in ok_rows if r["details"]["op"] == "seed_identity"]) <= 1
    sync_rows = [r for r in ok_rows if r["details"]["op"] != "seed_identity"]
    assert len(sync_rows) == 1
    row = sync_rows[0]
    assert row["ctx_session_id"] is None
    assert row["ctx_turn_id"] is None
