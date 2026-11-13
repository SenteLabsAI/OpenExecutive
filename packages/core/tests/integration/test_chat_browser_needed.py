"""The web chat's Add to Chrome card (api/routes/chat.py,
browser/tools.py): a turn whose browser tool found the person's
extension not connected ends with one `browser_needed` event, and the next
turn doesn't inherit it."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.browser import relay as browser_relay
from openexecutive.browser import tools as browser_tools
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.orchestrator.schedule_tools import current_session


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    chat_route._sessions.clear()
    browser_relay._reset_for_tests()
    browser_tools._NEEDED.clear()
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever
    from openexecutive.onboarding import profile_builder
    from openexecutive.utils import session_title

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_k: "")

    async def _no_title(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_title, "generate_session_title", _no_title)
    monkeypatch.setattr(browser_tools, "STORE_URL", None)
    monkeypatch.delenv(browser_tools.STORE_URL_ENV, raising=False)
    yield
    browser_relay._reset_for_tests()
    browser_tools._NEEDED.clear()


def _client(monkeypatch: pytest.MonkeyPatch, *, use_browser: list[bool]) -> TestClient:
    from openexecutive.orchestrator import executive as exec_mod

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, *, session: Any, **_kwargs: Any) -> AsyncIterator[str]:
            if use_browser.pop(0):
                # The real tool, as the speaker: nobody's extension is asking.
                token = current_session.set(
                    SimpleNamespace(session_id=session.session_id, caller_person_id=7)
                )
                try:
                    out = json.loads(await browser_tools._handler("browser_look")({}))
                finally:
                    current_session.reset(token)
                assert "isn't connected" in out["error"]
            yield "Done."

    monkeypatch.setattr(exec_mod, "Executive", _Executive)
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _events(body: str) -> list[dict[str, Any]]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def test_a_turn_that_needed_the_browser_ends_with_the_card_and_the_next_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, use_browser=[True, False])
    first = _events(client.post("/chat", json={"message": "Sort Amazon by best sellers"}).text)
    needed = [e for e in first if e.get("type") == "browser_needed"]
    assert len(needed) == 1 and needed[0]["store_url"] is None
    assert [e["type"] for e in first].index("browser_needed") < [e["type"] for e in first].index("done")
    session_id = next(e["session_id"] for e in first if e.get("type") == "done")

    second = _events(client.post("/chat", json={"message": "Thanks", "session_id": session_id}).text)
    assert not any(e.get("type") == "browser_needed" for e in second)
    assert browser_tools._NEEDED == {}


def test_a_turn_that_never_needed_the_browser_has_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, use_browser=[False])
    events = _events(client.post("/chat", json={"message": "Hi"}).text)
    assert not any(e.get("type") == "browser_needed" for e in events)
