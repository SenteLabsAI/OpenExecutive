from __future__ import annotations

import os

import pytest

# Required env vars for Settings() — set here so individual test modules
# don't each have to remember. Real values come from .env in dev/prod.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")
os.environ.setdefault("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")


@pytest.fixture(autouse=True)
def reset_active_gateway():
    """Ensure the module-level MCP gateway singleton is cleared between tests."""
    from openexecutive.orchestrator.mcp_gateway import set_active_gateway
    set_active_gateway(None)
    yield
    set_active_gateway(None)


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
