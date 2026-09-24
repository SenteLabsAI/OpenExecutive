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
# The RAG distance gates are read by retrieve() at call time and several
# retrieval tests hard-code distances either side of the 0.55 default. Because
# config.py loads a developer's .env, putting a tuning value there — the
# documented way to use these levers — would otherwise change the gate for the
# whole suite and fail those tests for a reason nothing in them mentions.
os.environ.pop("KNOWLEDGE_DISTANCE_THRESHOLD", None)
os.environ.pop("KNOWLEDGE_BUILTIN_DISTANCE_THRESHOLD", None)


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
