from __future__ import annotations

import os

import pytest

# Required env vars for Settings() — set here so individual test modules
# don't each have to remember. Real values come from .env in dev/prod.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")
os.environ.setdefault("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")

# Tests run as a local, non-public process. If this leaks in from the developer's
# shell, create_app() fails closed on the missing BACKEND_SHARED_SECRET and every
# full-app test errors at construction — the same trap BACKEND_SHARED_SECRET sets
# (see CLAUDE.md → Testing). Clear it so the suite matches CI either way.
os.environ.pop("OE_PUBLIC_DEPLOYMENT", None)
# Same for signed callers: with public keys in the shell, every full-app test
# that names its caller with x-caller-email is refused with 401.
os.environ.pop("CALLER_ASSERTION_PUBLIC_KEYS", None)
# The RAG distance gates are read by retrieve() at call time and several
# retrieval tests hard-code distances either side of the 0.55 default. Because
# config.py loads a developer's .env, putting a tuning value there — the
# documented way to use these levers — would otherwise change the gate for the
# whole suite and fail those tests for a reason nothing in them mentions.
os.environ.pop("KNOWLEDGE_DISTANCE_THRESHOLD", None)
os.environ.pop("KNOWLEDGE_BUILTIN_DISTANCE_THRESHOLD", None)

# The same trap, one layer down. `Settings` reads the deployment's `.env`, so
# the suite inherits whatever the developer's machine happens to be running.
#
# That is untidy until a `.env` is valid for a service and not for a test, at
# which point it breaks collection. A deployment that keeps its secrets in a
# manager and hydrates them at boot writes `HONCHO_ENABLED=true` with no
# `HONCHO_API_KEY` beside it — correct for that service, and fatal for a
# `Settings()` built in a test: "HONCHO_ENABLED=true requires HONCHO_API_KEY",
# raised at COLLECTION time, so 32 tests across 7 modules died before one of
# them ran. Nothing in the suite touched any of them.
#
# Cutting the file out is the fix, not clearing whichever flags happen to hurt
# today: os.environ is something this file controls and a test can monkeypatch,
# `.env` is neither, and a test that does `monkeypatch.delenv("HONCHO_ENABLED")`
# to assert the default would have the deployment's value quietly reappear
# underneath it. Everything Settings genuinely requires is set above.
from openexecutive.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _no_ai_disclosure(monkeypatch: pytest.MonkeyPatch):
    """Leave the AI disclosure line (orchestrator.disclosure) off what tests
    send, so a test's exact-text assertions don't depend on it, and forget
    which chats have seen it. test_ai_disclosure.py turns it back on."""
    from openexecutive.orchestrator import disclosure

    monkeypatch.setenv("AI_DISCLOSURE", "false")
    disclosure.reset()
    yield
    disclosure.reset()


@pytest.fixture(autouse=True)
def _outside_content_asks(monkeypatch: pytest.MonkeyPatch):
    """Hold every outside read, as "Ask every time" does
    (orchestrator.outside_mode), whatever the test database stores, so the
    hold tests don't depend on the default. test_outside_mode.py picks the
    other settings."""
    from openexecutive.orchestrator import outside_mode

    monkeypatch.setattr(outside_mode, "get_mode", lambda **_: outside_mode.ASK)


@pytest.fixture(autouse=True)
def _content_scan_unchecked(monkeypatch: pytest.MonkeyPatch):
    """Keep the content check (orchestrator.content_scan) from calling a
    model: every read comes back unchecked, which holds under Careful (the
    suite's level, ``_outside_content_asks``) and, below it, holds nothing
    and puts ``UNCHECKED_NOTICE`` before the result. test_content_scan.py
    and the level tests give it verdicts of their own."""
    from openexecutive.orchestrator import content_scan

    async def unchecked(content: object, *, model: str | None = None) -> content_scan.Verdict:
        return content_scan.UNCHECKED

    async def list_unchecked(entries: list[object], *, model: str | None = None) -> content_scan.Verdict:
        return content_scan.UNCHECKED

    monkeypatch.setattr(content_scan, "scan", unchecked)
    monkeypatch.setattr(content_scan, "scan_list", list_unchecked)
    content_scan.reset()


@pytest.fixture(autouse=True)
def _no_claim_check(monkeypatch: pytest.MonkeyPatch):
    """Keep the end-of-turn claim check (orchestrator.claim_check) from making
    a provider call in tests that script a turn; test_claim_check.py turns it
    back on. The conversation review (memory.chat_review) likewise records
    nothing and schedules no review; test_chat_review.py turns it on."""
    monkeypatch.setenv("CLAIM_CHECK_ENABLED", "false")
    monkeypatch.setenv("CHAT_REVIEW_ENABLED", "false")


@pytest.fixture(autouse=True)
def _no_logfire_tracing(monkeypatch: pytest.MonkeyPatch):
    """Keep a developer's LOGFIRE_TOKEN (in .env) from turning tracing on.

    Settings reads .env directly, so popping the env var is not enough; with
    tracing on, get_provider() returns a wrapper and registry identity tests
    fail for a reason nothing in them mentions.
    """
    import openexecutive.observability as observability

    monkeypatch.setattr(observability, "_logfire_token", lambda: None)
    monkeypatch.setattr(observability, "_enabled", False)


@pytest.fixture(autouse=True)
def reset_active_gateway():
    """Ensure the module-level MCP gateway singleton is cleared between tests."""
    from openexecutive.orchestrator.mcp_gateway import set_active_gateway
    set_active_gateway(None)
    yield
    set_active_gateway(None)


@pytest.fixture(autouse=True)
def _no_background_open_loop_pass(monkeypatch: pytest.MonkeyPatch):
    """Keep the post-turn open-loop pass from firing in unrelated tests.

    Every Executive turn schedules it fire-and-forget; left live it would make
    a provider call with the fake key and write audit / usage rows into the
    default ``./episodic_memory.db`` (the audit-pollution trap in CLAUDE.md).
    Tests of the pass itself call ``run_open_loop_pass`` directly, or undo
    this patch."""
    monkeypatch.setattr(
        "openexecutive.attunement.open_loops.schedule_open_loop_pass",
        lambda *args, **kwargs: None,
    )
    # Same for the working-style pass (attunement.style): tests of it call
    # run_style_pass directly.
    monkeypatch.setattr(
        "openexecutive.attunement.style.schedule_style_pass",
        lambda *args, **kwargs: None,
    )
    yield


@pytest.fixture
def install_source_feed(monkeypatch: pytest.MonkeyPatch):
    """Point one monitoring source adapter's bounded fetch at a canned body.

    Every feed adapter (``vendor_status``, ``rss``, ``edgar``, …) reaches
    the network through its own module-level ``fetch_bounded`` +
    ``validate_target_url`` pair, so each test module used to carry its own
    near-identical monkeypatch helper. Yields a setter:

        captured = install_source_feed("edgar", b"<feed>…</feed>")
        ...
        assert captured["user_agent"] == ...

    The returned dict records the arguments of the LAST fetch — ``url``,
    ``max_bytes``, and any keyword the adapter passes (edgar sends
    ``user_agent``; keys an adapter doesn't send are absent).
    """
    def _install(module: str, body: bytes | str) -> dict:
        captured: dict = {}
        payload = body.encode() if isinstance(body, str) else body

        async def fake_fetch(url: str, max_bytes: int, **kwargs) -> bytes:
            captured.clear()
            captured.update({"url": url, "max_bytes": max_bytes, **kwargs})
            return payload

        base = f"openexecutive.monitoring.sources.{module}"
        monkeypatch.setattr(f"{base}.fetch_bounded", fake_fetch)
        monkeypatch.setattr(f"{base}.validate_target_url", lambda u: (True, ""))
        return captured

    return _install


@pytest.fixture(autouse=True)
def _no_artifact_indexing(monkeypatch: pytest.MonkeyPatch):
    """Keep draft_artifact from indexing into a real ChromaDB store.

    `draft_artifact` (and the alert review's draft move, which calls it)
    indexes every artifact into the knowledge store. Left live, any test
    that drafts one would create ./chroma_db and load an embedding model.
    Tests that exercise indexing patch `_knowledge_store` to a fake.
    """
    monkeypatch.setattr(
        "openexecutive.orchestrator.artifact_tools._knowledge_store", lambda: None
    )
    yield


@pytest.fixture(autouse=True)
def _chat_topics_title_only(monkeypatch: pytest.MonkeyPatch):
    """Naming a chat (utils.chat_topics.name_chat) asks for its title and
    topic in one call. Route it through session_title.generate_session_title,
    looked up at call time, so a test's stub of that keeps naming the chat
    and nothing reaches the provider; no topic is filed.
    test_chat_topics.py restores the real call."""
    from openexecutive.utils import chat_topics, session_title

    async def _title_only(user_msg: str, assistant_msg: str, names: list[str]):
        return await session_title.generate_session_title(user_msg, assistant_msg), None

    monkeypatch.setattr(chat_topics, "_title_and_topic", _title_only)
