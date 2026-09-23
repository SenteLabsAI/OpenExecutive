"""Tests for GET /sessions/{id}/followup — the composer's suggested next message.

Covers:
- A conversation ending on a persisted reply returns the model's suggestion.
- Model failure / bad output returns ``null`` with a 200, and is not cached.
- A conversation that does not end on a reply skips the model entirely.
- Unknown session → 404; someone else's session → 403.
- Repeat calls for the same reply hit the cache.
- The fast-model helper parses fenced JSON and rejects runaway answers.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes import sessions as sessions_route

_CONVO: list[dict[str, Any]] = [
    {"role": "user", "content": "Should we raise prices on the Pro tier?"},
    {"id": 7, "role": "assistant", "content": "Yes — a 15% increase, grandfathering current Pro accounts."},
]


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sessions_route.router)
    return app


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    sessions_route._followup_cache.clear()
    yield
    sessions_route._followup_cache.clear()


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[dict[str, Any]],
    *,
    exists: bool = True,
    allowed: bool = True,
) -> None:
    monkeypatch.setattr(sessions_route, "get_session_owner", lambda _sid: (exists, 1))
    monkeypatch.setattr(sessions_route, "_resolve_caller_person_id", lambda _req: 1)
    monkeypatch.setattr(sessions_route, "is_principal_or_self", lambda _c, _o: allowed)
    monkeypatch.setattr(sessions_route, "load_messages", lambda _sid: list(messages))


def _patch_llm(monkeypatch: pytest.MonkeyPatch, result: str | None) -> dict[str, Any]:
    calls: dict[str, Any] = {"n": 0, "transcript": None}

    async def _fake(transcript: str) -> str | None:
        calls["n"] += 1
        calls["transcript"] = transcript
        return result

    monkeypatch.setattr(chat_route, "_generate_followup_via_llm", _fake)
    return calls


def test_returns_suggestion_for_last_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _CONVO)
    calls = _patch_llm(monkeypatch, "Draft the customer email announcing the Pro increase.")

    with TestClient(_make_app()) as client:
        res = client.get("/sessions/s1/followup")

    assert res.status_code == 200
    assert res.json() == {"suggestion": "Draft the customer email announcing the Pro increase."}
    assert calls["n"] == 1
    assert "USER: Should we raise prices" in calls["transcript"]
    assert "EXECUTIVE: Yes — a 15% increase" in calls["transcript"]


def test_llm_failure_returns_null_and_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _CONVO)
    calls = _patch_llm(monkeypatch, None)

    with TestClient(_make_app()) as client:
        first = client.get("/sessions/s1/followup")
        second = client.get("/sessions/s1/followup")

    assert first.status_code == 200
    assert first.json() == {"suggestion": None}
    assert second.json() == {"suggestion": None}
    assert calls["n"] == 2, "a failure must not pin null for this reply"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "hello"}],
        # A reply the client never got an id for (no persisted row).
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
    ],
)
def test_no_reply_skips_llm(monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]]) -> None:
    _patch_session(monkeypatch, messages)
    calls = _patch_llm(monkeypatch, "should not be used")

    with TestClient(_make_app()) as client:
        res = client.get("/sessions/s1/followup")

    assert res.status_code == 200
    assert res.json() == {"suggestion": None}
    assert calls["n"] == 0


def test_unknown_session_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _CONVO, exists=False)
    calls = _patch_llm(monkeypatch, "x")

    with TestClient(_make_app()) as client:
        res = client.get("/sessions/nope/followup")

    assert res.status_code == 404
    assert calls["n"] == 0


def test_other_callers_session_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_session(monkeypatch, _CONVO, allowed=False)
    calls = _patch_llm(monkeypatch, "x")

    with TestClient(_make_app()) as client:
        res = client.get("/sessions/s1/followup")

    assert res.status_code == 403
    assert calls["n"] == 0


def test_same_reply_hits_cache_new_reply_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = list(_CONVO)
    _patch_session(monkeypatch, messages)
    calls = _patch_llm(monkeypatch, "Who should own the rollout?")

    with TestClient(_make_app()) as client:
        client.get("/sessions/s1/followup")
        client.get("/sessions/s1/followup")
        assert calls["n"] == 1

        newer = [*_CONVO, {"role": "user", "content": "ok"}, {"id": 9, "role": "assistant", "content": "Done."}]
        _patch_session(monkeypatch, newer)
        client.get("/sessions/s1/followup")

    assert calls["n"] == 2


def test_transcript_keeps_tail_and_truncates() -> None:
    long = "x" * 5000
    messages = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    messages.append({"id": 1, "role": "assistant", "content": long})

    transcript = chat_route._build_followup_transcript(messages)

    assert "USER: m4" not in transcript
    assert "USER: m5" in transcript
    assert "x" * chat_route._FOLLOWUP_MAX_CHARS_PER_MESSAGE + " […]" in transcript
    assert "x" * (chat_route._FOLLOWUP_MAX_CHARS_PER_MESSAGE + 1) not in transcript


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def messages_create(self, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(self._text)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    from openexecutive import providers
    from openexecutive.agents import utility_fast

    monkeypatch.setattr(utility_fast, "get_fast_model", lambda: "claude-haiku-test")
    monkeypatch.setattr(providers, "get_provider", lambda _model: _FakeProvider(text))


@pytest.mark.asyncio
async def test_generator_parses_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch, '```json\n{"suggestion": "  Draft the  Pro pricing email. "}\n```')
    assert await chat_route._generate_followup_via_llm("t") == "Draft the Pro pricing email."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["a list"]',
        '{"suggestion": ""}',
        '{"suggestion": 42}',
        '{"other": "key"}',
        '{"suggestion": "' + " ".join(["word"] * 30) + '"}',
    ],
)
async def test_generator_rejects_bad_output(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    _patch_provider(monkeypatch, raw)
    assert await chat_route._generate_followup_via_llm("t") is None
