"""HTTP tests for the conversational workflow designer (/workflows/designer/*).

``advance`` is scripted, so these pin the route's own contract: session
lifecycle (TTL, cap, no orphans on a failed start), phase transitions, the
rollback of an unanswered turn, fixed-string errors that never echo input,
and that the literal paths resolve on the full app next to ``/workflows/{name}``.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.api.models import (  # noqa: E402
    WORKFLOW_DESIGNER_MESSAGE_MAX_CHARS,
    WorkflowDesignerMessageRequest,
    WorkflowDesignerSessionRequest,
)
from openexecutive.api.routes import workflow_designer as route  # noqa: E402
from openexecutive.onboarding.interview import Turn  # noqa: E402
from openexecutive.workflows import designer as wd  # noqa: E402
from openexecutive.workflows import dynamic_store  # noqa: E402

SECRET = "zz_user_secret_zz"


def _draft(**overrides: Any) -> wd.WorkflowDraft:
    definition: dict[str, Any] = {
        "name": "weekly_competitor_digest",
        "title": "Weekly competitor digest",
        "section": "Growth & GTM",
        "input_fields": [{"name": "competitors", "label": "Competitors"}],
        "steps": [
            {"kind": "specialist", "id": "scan", "title": "Scan", "specialist": "cso",
             "goal": "Summarize {competitors}."},
            {"kind": "synthesis", "id": "assemble", "title": "Assemble"},
        ],
    }
    definition.update(overrides)
    return wd.WorkflowDraft.model_validate(
        {"definition": definition, "summary": "A weekly digest.", "assumptions": ["Memo."]}
    )


class _Queue(list):  # type: ignore[type-arg]
    """advance() results to return in order, plus the kwargs of every call."""

    calls: list[dict[str, Any]]


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    queue = _Queue()
    queue.calls = calls = []

    async def _advance(transcript: list[Any], **kwargs: Any) -> Any:
        calls.append({"transcript": list(transcript), **kwargs})
        if not queue:
            raise AssertionError("advance() called more times than scripted")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(route, "advance", _advance)
    return queue


@pytest.fixture()
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    db_path = tmp_path / "episodic.db"
    dynamic_store.initialize_dynamic_workflows_db(db_path)
    monkeypatch.setattr(dynamic_store, "DB_PATH", db_path)
    monkeypatch.setattr(route, "_designer_sessions", OrderedDict())
    monkeypatch.setattr(route, "build_context_block", lambda: "CTX")
    monkeypatch.delenv("BACKEND_SHARED_SECRET", raising=False)
    with TestClient(create_app()) as c:
        yield c


def _start(client: TestClient, message: str = "Weekly competitor digest.") -> dict[str, Any]:
    resp = client.post("/workflows/designer/start", json={"message": message})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_question_then_draft_then_resume(client: TestClient, seeded: _Queue) -> None:
    seeded += [
        wd.DesignerQuestion(question="Which competitors?", hint="Names.", options=["Acme"]),
        _draft(),
    ]
    first = _start(client)
    assert first["phase"] == "question"
    assert first["question"] == "Which competitors?"
    assert first["hint"] == "Names."
    assert first["options"] == ["Acme"]
    assert first["questions_asked"] == 1
    assert first["max_questions"] == wd.MAX_QUESTIONS
    sid = first["session_id"]

    resp = client.post("/workflows/designer/message", json={"session_id": sid, "message": "Acme."})
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["phase"] == "draft"
    assert second["draft"]["definition"]["name"] == "weekly_competitor_digest"
    assert second["draft"]["assumptions"] == ["Memo."]
    assert [t["role"] for t in second["transcript"]] == ["user", "assistant", "user", "assistant"]

    # The context block is rendered once and passed on every call.
    calls = seeded.calls
    assert all(c["context_block"] == "CTX" for c in calls)

    resumed = client.get(f"/workflows/designer/{sid}")
    assert resumed.status_code == 200
    assert resumed.json()["draft"]["definition"]["title"] == "Weekly competitor digest"


def test_draft_is_a_valid_create_body(client: TestClient, seeded: _Queue) -> None:
    """The UI posts draft.definition straight to POST /workflows/custom."""
    seeded.append(_draft())
    body = _start(client)["draft"]["definition"]
    resp = client.post("/workflows/custom", json=body)
    assert resp.status_code == 201, resp.text


def test_refining_passes_the_previous_draft(client: TestClient, seeded: _Queue) -> None:
    seeded += [
        _draft(),
        wd.DesignerQuestion(question="What should it be called?"),
        _draft(title="Renamed"),
    ]
    sid = _start(client)["session_id"]
    q = client.post("/workflows/designer/message", json={"session_id": sid, "message": "Rename it."})
    assert q.json()["phase"] == "question"
    d = client.post("/workflows/designer/message", json={"session_id": sid, "message": "Renamed."})
    assert d.json()["draft"]["definition"]["title"] == "Renamed"

    calls = seeded.calls
    assert calls[0]["previous_draft"] is None
    # Still refining the first draft even though a question came in between.
    assert calls[1]["previous_draft"].title == "Weekly competitor digest"
    assert calls[2]["previous_draft"].title == "Weekly competitor digest"


def test_force_draft(client: TestClient, seeded: _Queue) -> None:
    seeded += [wd.DesignerQuestion(question="How often?"), _draft()]
    sid = _start(client)["session_id"]
    resp = client.post("/workflows/designer/draft", json={"session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["phase"] == "draft"
    assert seeded.calls[1]["force_draft"] is True


@pytest.mark.parametrize("message", ["", "   ", "x" * (WORKFLOW_DESIGNER_MESSAGE_MAX_CHARS + 1)])
def test_start_rejects_bad_messages_without_echo(
    client: TestClient, seeded: _Queue, message: str
) -> None:
    resp = client.post("/workflows/designer/start", json={"message": message})
    assert resp.status_code == 422
    assert "xxxxxxxxxx" not in resp.text
    assert route._designer_sessions == {}


def test_transcript_cap(client: TestClient, seeded: _Queue) -> None:
    seeded.append(wd.DesignerQuestion(question="More?"))
    sid = _start(client)["session_id"]
    route._designer_sessions[sid].transcript[0].text = "x" * wd.MAX_TRANSCRIPT_CHARS
    resp = client.post("/workflows/designer/message", json={"session_id": sid, "message": "hi"})
    assert resp.status_code == 422
    assert "Draft the workflow now" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (wd.WorkflowDesignerTimeout("The workflow assistant took too long to respond. Try again."), 504),
        (wd.WorkflowDesignerError("The workflow assistant is unavailable right now. Try again."), 502),
    ],
)
def test_failed_start_leaves_no_orphan(
    client: TestClient, seeded: _Queue, exc: Exception, status: int
) -> None:
    seeded.append(exc)
    resp = client.post("/workflows/designer/start", json={"message": SECRET})
    assert resp.status_code == status
    assert SECRET not in resp.text
    assert route._designer_sessions == {}


def test_failed_message_rolls_back_the_turn(client: TestClient, seeded: _Queue) -> None:
    seeded += [
        wd.DesignerQuestion(question="Which competitors?"),
        wd.WorkflowDesignerError("The workflow assistant is unavailable right now. Try again."),
    ]
    sid = _start(client)["session_id"]
    resp = client.post("/workflows/designer/message", json={"session_id": sid, "message": SECRET})
    assert resp.status_code == 502
    assert SECRET not in resp.text
    transcript = route._designer_sessions[sid].transcript
    assert [t.role for t in transcript] == ["user", "assistant"]


def test_unknown_session_is_404(client: TestClient, seeded: _Queue) -> None:
    assert client.get("/workflows/designer/nope").status_code == 404
    resp = client.post("/workflows/designer/message", json={"session_id": "nope", "message": "hi"})
    assert resp.status_code == 404
    assert client.post("/workflows/designer/draft", json={"session_id": "nope"}).status_code == 404


def test_sessions_expire(client: TestClient, seeded: _Queue) -> None:
    seeded.append(wd.DesignerQuestion(question="How often?"))
    sid = _start(client)["session_id"]
    route._designer_sessions[sid].last_touched = time.monotonic() - route._DESIGNER_TTL_SECONDS - 1
    assert client.get(f"/workflows/designer/{sid}").status_code == 404


def test_session_cap_evicts_oldest(
    client: TestClient, seeded: _Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(route, "_MAX_DESIGNER_SESSIONS", 2)
    seeded += [wd.DesignerQuestion(question=f"Q{i}?") for i in range(3)]
    first, second, third = (_start(client)["session_id"] for _ in range(3))
    assert list(route._designer_sessions) == [second, third]
    assert client.get(f"/workflows/designer/{first}").status_code == 404


def test_designer_routes_are_mounted_on_the_full_app(
    client: TestClient, seeded: _Queue
) -> None:
    """The literal designer paths resolve on create_app(), not a /workflows/{name} route."""
    seeded.append(wd.DesignerQuestion(question="How often?"))
    sid = _start(client)["session_id"]
    resp = client.get(f"/workflows/designer/{sid}")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sid


def test_transcript_cap_wording_in_draft_phase(client: TestClient, seeded: _Queue) -> None:
    """Once a draft exists, the cap points at Create / the editor, not 'draft now'."""
    seeded.append(_draft())
    sid = _start(client)["session_id"]
    route._designer_sessions[sid].transcript[0].text = "x" * wd.MAX_TRANSCRIPT_CHARS
    resp = client.post("/workflows/designer/message", json={"session_id": sid, "message": "hi"})
    assert resp.status_code == 422
    assert "Create the workflow as it is" in resp.json()["detail"]


def _bare_session(monkeypatch: pytest.MonkeyPatch) -> tuple[str, route.DesignerSession]:
    """A session registered directly, for calling the route coroutines concurrently."""
    monkeypatch.setattr(route, "_designer_sessions", OrderedDict())
    session = route.DesignerSession(
        transcript=[Turn(role="user", text="Weekly digest."), Turn(role="assistant", text="How often?")],
        questions_asked=1,
    )
    route._designer_sessions["sid"] = session
    return "sid", session


@pytest.mark.asyncio
async def test_second_turn_while_one_is_in_flight_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    sid, session = _bare_session(monkeypatch)
    release = asyncio.Event()

    async def _slow_advance(transcript: list[Any], **kwargs: Any) -> Any:
        await release.wait()
        return wd.DesignerQuestion(question="Who receives it?")

    monkeypatch.setattr(route, "advance", _slow_advance)
    first = asyncio.create_task(
        route.designer_message(WorkflowDesignerMessageRequest(session_id=sid, message="Weekly."))
    )
    await asyncio.sleep(0)
    assert session.busy is True

    for call in (
        route.designer_draft(WorkflowDesignerSessionRequest(session_id=sid)),
        route.designer_message(WorkflowDesignerMessageRequest(session_id=sid, message="again")),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 409

    release.set()
    resp = await first
    assert resp.question == "Who receives it?"
    assert session.busy is False
    # The rejected calls left no trace in the transcript.
    assert [t.text for t in session.transcript] == [
        "Weekly digest.", "How often?", "Weekly.", "Who receives it?",
    ]


@pytest.mark.asyncio
async def test_rollback_removes_the_failed_turn_not_the_last_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if something else appended after it, only the failed user turn goes."""
    sid, session = _bare_session(monkeypatch)
    late = Turn(role="assistant", text="appended by someone else")

    async def _failing_advance(transcript: list[Any], **kwargs: Any) -> Any:
        transcript.append(late)
        raise wd.WorkflowDesignerError("The workflow assistant is unavailable right now. Try again.")

    monkeypatch.setattr(route, "advance", _failing_advance)
    with pytest.raises(HTTPException) as exc:
        await route.designer_message(WorkflowDesignerMessageRequest(session_id=sid, message=SECRET))
    assert exc.value.status_code == 502
    assert session.busy is False
    assert [t.text for t in session.transcript] == [
        "Weekly digest.", "How often?", "appended by someone else",
    ]
