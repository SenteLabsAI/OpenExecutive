"""The /chat route must render the Ask OE panel's page_context into a
user-turn block and pass it to the Executive — and stay byte-identical to
the old behavior when no page_context is sent.

Fixture pattern mirrors test_chat_route_parallel_context.py (capturing
Executive, temp DBs, stubbed retriever/Honcho).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.models import PageContext, PageFormDescriptor, PageFormField
from openexecutive.api.routes import chat as chat_route
from openexecutive.api.routes.chat import (
    _PAGE_FORM_JSON_MAX_CHARS,
    _build_page_context_block,
)
from openexecutive.memory import episodic, session_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store


@pytest.fixture(autouse=True)
def _reset_route_state() -> None:
    chat_route._sessions.clear()
    chat_route._last_turn_events.clear()
    chat_route._last_turn_meta.clear()


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


@pytest.fixture()
def patched_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from openexecutive.knowledge import retriever as retriever_mod
    from openexecutive.memory import honcho_client as honcho_mod
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever_mod, "retrieve", lambda **_: "")

    async def fake_prefetch(*_a: Any, **_kw: Any) -> str:
        return ""

    monkeypatch.setattr(honcho_mod, "prefetch", fake_prefetch)


def _install_capturing_executive(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    from openexecutive.orchestrator import executive as exec_mod

    class _CapturingExecutive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat"] = kwargs
            yield "ok"

        async def stream_chat_with_committee(self, **kwargs: Any) -> AsyncIterator[str]:
            captured["stream_chat_with_committee"] = kwargs
            yield "ok"

    monkeypatch.setattr(exec_mod, "Executive", _CapturingExecutive)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


def _form_payload() -> dict[str, Any]:
    return {
        "route": "/jobs/new",
        "title": "New workflow",
        "guide_section_id": "jobs",
        "form": {
            "form_id": "workflow_builder",
            "title": "New workflow",
            "fields": [
                {"name": "title", "label": "Title", "type": "text", "value": ""},
                {
                    "name": "section",
                    "label": "Section",
                    "type": "select",
                    "options": ["Board", "Capital"],
                },
            ],
        },
    }


def test_page_context_block_reaches_stream_chat(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    people_store.upsert_person(full_name="Alex", is_principal=True)
    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    from openexecutive.guide import prebuilt as guide_prebuilt

    monkeypatch.setattr(
        guide_prebuilt._STORE,
        "get",
        lambda _id: {"section_id": "jobs", "markdown": "GUIDE-MARKDOWN-FOR-JOBS"},
    )

    resp = _client().post(
        "/chat",
        json={"message": "fill this in for me", "page_context": _form_payload()},
    )
    assert resp.status_code == 200
    _ = resp.text

    block = captured["stream_chat"]["page_context_block"]
    assert 'PAGE: /jobs/new — "New workflow"' in block
    assert "USER GUIDE FOR THIS PAGE (jobs):" in block
    assert "GUIDE-MARKDOWN-FOR-JOBS" in block
    assert "FORM ON SCREEN (form_id=workflow_builder)" in block
    assert '"name":"title"' in block.replace(" ", "")
    assert "propose_form_values" in block


def test_no_page_context_is_empty_block(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main-chat and integration turns must be unchanged: empty block."""
    people_store.upsert_person(full_name="Alex", is_principal=True)
    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    resp = _client().post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    _ = resp.text

    assert captured["stream_chat"]["page_context_block"] == ""


def test_committee_route_also_gets_page_context(
    temp_db: Path, patched_deps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    people_store.upsert_person(full_name="Alex", is_principal=True)
    captured: dict[str, Any] = {}
    _install_capturing_executive(monkeypatch, captured)

    resp = _client().post(
        "/chat",
        json={
            "message": "explain this page",
            "committee_review": True,
            "page_context": {"route": "/departments", "title": "Departments"},
        },
    )
    assert resp.status_code == 200
    _ = resp.text

    block = captured["stream_chat_with_committee"]["page_context_block"]
    assert 'PAGE: /departments — "Departments"' in block


def test_unknown_guide_section_is_skipped() -> None:
    """A guide id with no prebuilt file must not break the block — explain
    tier still works from route + title."""
    block = _build_page_context_block(
        PageContext(route="/people", title="People", guide_section_id="no-such-section")
    )
    assert 'PAGE: /people — "People"' in block
    assert "USER GUIDE" not in block


def test_summary_included_when_present() -> None:
    block = _build_page_context_block(
        PageContext(route="/audit", title="Audit log", summary="Read-only event log.")
    )
    assert "Read-only event log." in block


def test_oversized_form_descriptor_truncated() -> None:
    big_form = PageFormDescriptor(
        form_id="workflow_builder",
        title="New workflow",
        fields=[
            PageFormField(name=f"f{i}", label="x", description="y" * 200)
            for i in range(200)
        ],
    )
    assert len(big_form.model_dump_json()) > _PAGE_FORM_JSON_MAX_CHARS
    block = _build_page_context_block(
        PageContext(route="/jobs/new", title="New workflow", form=big_form)
    )
    assert "…[truncated]" in block
    # The instruction footer must survive truncation — it follows the JSON.
    assert "propose_form_values" in block
