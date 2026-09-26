"""Unit tests for the Google Chat webhook integration."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openexecutive.integrations.google_chat as gc_module
from openexecutive.integrations.google_chat import _process_and_reply, send_reply  # noqa: F401

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(configured: bool = True) -> Any:
    return SimpleNamespace(
        google_chat_project_number="123456789012" if configured else None,
        google_chat_service_account_file="/fake/key.json" if configured else None,
        google_chat_service_account_email=None,
    )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with JWT verification bypassed and settings configured."""
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda token, proj: {"sub": "google"})

    app = FastAPI()
    app.include_router(gc_module.router)

    # Patch get_settings inside the webhook handler's import path.
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        # Re-import so the patch is active when the module-level router executes.
        yield TestClient(app)


def _message_event(text: str = "What is our Q3 strategy?") -> dict:
    return {
        "type": "MESSAGE",
        "message": {
            "name": "spaces/AAA/messages/MSG1",
            "text": text,
            "thread": {"name": "spaces/AAA/threads/T1"},
        },
        "space": {"name": "spaces/AAA", "type": "ROOM"},
        "user": {"displayName": "Jane Exec"},
    }


# ---------------------------------------------------------------------------
# Endpoint routing tests (no Executive calls)
# ---------------------------------------------------------------------------


def test_missing_bearer_returns_401() -> None:
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post("/webhook/google-chat", json=_message_event())
    assert resp.status_code == 401


def test_invalid_jwt_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gc_module, "verify_google_jwt", lambda *_: (_ for _ in ()).throw(ValueError("bad"))
    )
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/webhook/google-chat",
            json=_message_event(),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert resp.status_code == 401


def test_unconfigured_returns_503() -> None:
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch(
        "openexecutive.integrations.google_chat.get_settings",
        return_value=_make_settings(configured=False),
    ):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/webhook/google-chat",
            json=_message_event(),
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 503


def test_added_to_space_returns_greeting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})
    app = FastAPI()
    app.include_router(gc_module.router)
    payload = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/AAA", "type": "ROOM"}}
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app)
        resp = c.post("/webhook/google-chat", json=payload, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert "text" in resp.json()
    assert resp.json()["text"].startswith("Hello!")


def test_removed_from_space_returns_empty_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app)
        resp = c.post(
            "/webhook/google-chat",
            json={"type": "REMOVED_FROM_SPACE", "space": {"name": "spaces/AAA"}},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {}


def test_unknown_event_type_returns_empty_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app)
        resp = c.post(
            "/webhook/google-chat",
            json={"type": "CARD_CLICKED"},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {}


def test_empty_text_after_mention_strip_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message consisting only of a @mention token produces no background task."""
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})

    app = FastAPI()
    app.include_router(gc_module.router)

    # Text is purely a mention token — after stripping, nothing remains.
    payload = _message_event(text="<users/12345>")
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app)
        resp = c.post(
            "/webhook/google-chat",
            json=payload,
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {}


def test_valid_message_queues_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid MESSAGE event returns {} and queues a background task."""
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})

    process_calls: list[dict] = []

    async def fake_process(**kwargs: Any) -> None:
        process_calls.append(kwargs)

    monkeypatch.setattr(gc_module, "_process_and_reply", fake_process)

    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app)
        resp = c.post(
            "/webhook/google-chat",
            json=_message_event(text="<users/111> What is our runway?"),
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    assert resp.json() == {}
    # TestClient runs BackgroundTasks synchronously.
    assert len(process_calls) == 1
    assert process_calls[0]["message_text"] == "What is our runway?"
    assert process_calls[0]["sender_name"] == "Jane Exec"
    assert process_calls[0]["space_name"] == "spaces/AAA"
    assert process_calls[0]["thread_name"] == "spaces/AAA/threads/T1"
    assert process_calls[0]["message_name"] == "spaces/AAA/messages/MSG1"


# ---------------------------------------------------------------------------
# _process_and_reply unit tests (no HTTP layer)
# ---------------------------------------------------------------------------


def test_process_calls_alert_pipeline_with_correct_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alert pipeline receives source=google_chat and external_id=message_name."""
    recorded: list = []

    monkeypatch.setattr(
        "openexecutive.alerts.pipeline.schedule_evaluation",
        lambda event: recorded.append(event),
    )
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")

    profile = SimpleNamespace(is_empty=lambda: True)
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile", lambda: profile
    )

    async def fake_chat(**kw: Any) -> str:
        return "Analysis done."

    mock_exec = MagicMock()
    mock_exec.chat = fake_chat

    # send_reply is a sync function; patch it to a no-op so asyncio.to_thread succeeds.
    def fake_send_reply(*_args: Any, **_kw: Any) -> None:
        pass

    with (
        patch("openexecutive.orchestrator.executive.Executive", return_value=mock_exec),
        patch("openexecutive.integrations.google_chat.send_reply", new=fake_send_reply),
    ):
        asyncio.run(
            _process_and_reply(
                message_text="Hello exec",
                sender_name="Bob",
                space_name="spaces/BBB",
                thread_name="spaces/BBB/threads/T2",
                message_name="spaces/BBB/messages/M99",
                service_account_file="/fake/key.json",
                service_account_email=None,
            )
        )

    assert len(recorded) == 1
    evt = recorded[0]
    assert evt.source == "google_chat"
    assert evt.external_id == "spaces/BBB/messages/M99"
    assert evt.body == "Hello exec"
    assert evt.user == "Bob"


def test_process_sends_error_reply_on_executive_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Executive.chat() raises, an error message is sent via send_reply."""
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda _: None)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")

    profile = SimpleNamespace(is_empty=lambda: True)
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile", lambda: profile
    )

    async def boom(**kw: Any) -> str:
        raise RuntimeError("LLM exploded")

    mock_exec = MagicMock()
    mock_exec.chat = boom

    error_replies: list[str] = []

    def fake_send_reply(
        space_name: str,
        thread_name: str,
        text: str,
        service_account_file: str | None,
        service_account_email: str | None,
    ) -> None:
        error_replies.append(text)

    with (
        patch("openexecutive.orchestrator.executive.Executive", return_value=mock_exec),
        patch("openexecutive.integrations.google_chat.send_reply", new=fake_send_reply),
    ):
        asyncio.run(
            _process_and_reply(
                message_text="crash me",
                sender_name="Tester",
                space_name="spaces/CCC",
                thread_name="spaces/CCC/threads/T3",
                message_name="spaces/CCC/messages/M1",
                service_account_file="/fake/key.json",
                service_account_email=None,
            )
        )

    assert len(error_replies) == 1
    assert "error" in error_replies[0].lower()


# ---------------------------------------------------------------------------
# send_reply unit test
# ---------------------------------------------------------------------------


def test_send_reply_constructs_correct_api_call() -> None:
    """send_reply calls the Chat REST API with correct parent, body, and reply option."""
    mock_service = MagicMock()
    mock_creds = MagicMock()

    with (
        patch("google.oauth2.service_account.Credentials.from_service_account_file", return_value=mock_creds),
        patch("googleapiclient.discovery.build", return_value=mock_service) as mock_build,
    ):
        send_reply(
            space_name="spaces/AAA",
            thread_name="spaces/AAA/threads/T1",
            text="Here is my analysis.",
            service_account_file="/fake/key.json",
        )

    mock_build.assert_called_once_with("chat", "v1", credentials=mock_creds, cache_discovery=False)
    create_call = mock_service.spaces().messages().create
    create_call.assert_called_once_with(
        parent="spaces/AAA",
        body={"text": "Here is my analysis.", "thread": {"name": "spaces/AAA/threads/T1"}},
        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
    )
    mock_service.spaces().messages().create().execute.assert_called_once()


def test_send_reply_omits_thread_for_dm() -> None:
    """send_reply omits thread key when thread_name is empty (DM case)."""
    mock_service = MagicMock()
    mock_creds = MagicMock()

    with (
        patch("google.oauth2.service_account.Credentials.from_service_account_file", return_value=mock_creds),
        patch("googleapiclient.discovery.build", return_value=mock_service),
    ):
        send_reply(
            space_name="spaces/DM123",
            thread_name="",
            text="Direct reply.",
            service_account_file="/fake/key.json",
        )

    create_call = mock_service.spaces().messages().create
    _, kwargs = create_call.call_args
    assert "thread" not in kwargs["body"]


def test_invalid_jwt_issuer_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JWT with wrong issuer is rejected even if signature is valid."""
    monkeypatch.setattr(
        gc_module,
        "verify_google_jwt",
        lambda *_: (_ for _ in ()).throw(ValueError("Unexpected issuer")),
    )
    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/webhook/google-chat",
            json=_message_event(),
            headers={"Authorization": "Bearer valid-sig-wrong-iss"},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

_UPLOADED = {
    "name": "spaces/AAA/messages/MSG1/attachments/A1",
    "contentName": "scan.pdf",
    "contentType": "application/pdf",
    "attachmentDataRef": {"resourceName": "spaces/AAA/attachments/RES1"},
    "source": "UPLOADED_CONTENT",
}
_DRIVE = {
    "name": "spaces/AAA/messages/MSG1/attachments/A2",
    "contentName": "Board deck",
    "contentType": "application/pdf",
    "driveDataRef": {"driveFileId": "1AbC"},
    "source": "DRIVE_FILE",
}


def test_attachment_only_message_is_no_longer_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc_module, "verify_google_jwt", lambda *_: {})
    process_calls: list[dict] = []

    async def fake_process(**kwargs: Any) -> None:
        process_calls.append(kwargs)

    monkeypatch.setattr(gc_module, "_process_and_reply", fake_process)
    payload = _message_event(text="<users/111>")
    payload["message"]["attachment"] = [_UPLOADED]

    app = FastAPI()
    app.include_router(gc_module.router)
    with patch("openexecutive.integrations.google_chat.get_settings", return_value=_make_settings()):
        resp = TestClient(app).post(
            "/webhook/google-chat", json=payload, headers={"Authorization": "Bearer x"}
        )

    assert resp.status_code == 200
    (call,) = process_calls
    assert call["message_text"] == "(Attached files: scan.pdf)"
    assert call["attachments"] == [_UPLOADED]


def _run_with_attachments(
    monkeypatch: pytest.MonkeyPatch, attachments: list[dict], download: Any
) -> dict[str, Any]:
    from openexecutive.knowledge.pdf_reader import PdfReadResult

    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda _: None)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: SimpleNamespace(is_empty=lambda: True),
    )
    monkeypatch.setattr(gc_module, "download_attachment", download)
    monkeypatch.setattr("openexecutive.integrations.attachments._schedule_ingest", lambda *_: None)

    async def fake_read(data: bytes, *, filename: str = "") -> PdfReadResult:
        return PdfReadResult("APPRAISED VALUE 4.2M", "ocr", 1)

    monkeypatch.setattr("openexecutive.knowledge.pdf_reader.read_pdf_text", fake_read)

    captured: dict[str, Any] = {}

    async def fake_chat(**kw: Any) -> str:
        captured.update(kw)
        return "Done."

    mock_exec = MagicMock()
    mock_exec.chat = fake_chat
    with (
        patch("openexecutive.orchestrator.executive.Executive", return_value=mock_exec),
        patch("openexecutive.integrations.google_chat.send_reply", new=lambda *a, **k: None),
    ):
        asyncio.run(
            _process_and_reply(
                message_text="What is this worth?",
                sender_name="Bob",
                space_name="spaces/AAA",
                thread_name="spaces/AAA/threads/T1",
                message_name="spaces/AAA/messages/MSG1",
                service_account_file="/fake/key.json",
                service_account_email=None,
                attachments=attachments,
            )
        )
    return captured


def test_uploaded_pdf_is_downloaded_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    downloads: list[str] = []

    def download(resource: str, sa_file: str | None, sa_email: str | None) -> bytes:
        downloads.append(resource)
        return b"%PDF-scan"

    captured = _run_with_attachments(monkeypatch, [_UPLOADED], download)

    assert downloads == ["spaces/AAA/attachments/RES1"]
    assert captured["user_message"] == (
        "[Attached: scan.pdf] (converted from scanned pages)\nAPPRAISED VALUE 4.2M"
        "\n\nWhat is this worth?"
    )


def test_drive_file_is_left_to_the_drive_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def download(*_a: Any) -> bytes:
        raise AssertionError("a Drive file has no bytes to download here")

    captured = _run_with_attachments(monkeypatch, [_DRIVE], download)

    assert captured["user_message"].startswith(
        "(Attached Board deck: a Google Drive file, id 1AbC — read it with the Drive tools)"
    )


def test_oversized_or_failed_downloads_become_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_big(*_a: Any) -> bytes:
        raise ValueError("attachment too large")

    captured = _run_with_attachments(monkeypatch, [_UPLOADED], too_big)
    assert captured["user_message"].startswith("(Skipped scan.pdf: file too large — limit 20 MB)")

    def broken(*_a: Any) -> bytes:
        raise RuntimeError("403")

    captured = _run_with_attachments(monkeypatch, [_UPLOADED], broken)
    assert captured["user_message"].startswith("(Could not download scan.pdf)")


def test_download_attachment_stops_past_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is checked per chunk, so an oversized file is never held whole."""
    requested: list[str] = []
    service = MagicMock()
    service.media.return_value.download_media.side_effect = (
        lambda resourceName: requested.append(resourceName) or "REQUEST"
    )
    chunks_read: list[int] = []

    class FakeDownloader:
        def __init__(self, buf: Any, request: Any, chunksize: int) -> None:
            assert request == "REQUEST"
            self.buf = buf

        def next_chunk(self) -> tuple[None, bool]:
            chunks_read.append(1)
            self.buf.write(b"x" * 10)
            return None, len(chunks_read) >= 100

    monkeypatch.setattr(gc_module, "_build_credentials", lambda *_: "creds")
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: service)
    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", FakeDownloader)

    assert gc_module.download_attachment("res/1", None, None, max_bytes=1000) == b"x" * 1000
    assert requested == ["res/1"]

    chunks_read.clear()
    with pytest.raises(ValueError):
        gc_module.download_attachment("res/1", None, None, max_bytes=25)
    assert len(chunks_read) == 3
