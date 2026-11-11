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
from openexecutive.orchestrator.content_trust import wrap_untrusted

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turns keep no history in the shared default DB."""
    monkeypatch.setattr(gc_module, "_load_history", lambda _sid: [])
    monkeypatch.setattr(gc_module, "_save_turn", lambda *a, **k: None)


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
    payload["user"]["email"] = "jane@acme.com"

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
    assert call["sender_email"] == "jane@acme.com"


def _run_with_attachments(
    monkeypatch: pytest.MonkeyPatch,
    attachments: list[dict],
    download: Any,
    *,
    rostered: bool = True,
    direct: bool = False,
    one_on_one: bool = False,
    briefing: Any = None,
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
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_email",
        lambda email, **_kw: SimpleNamespace(id=3) if rostered and email == "bob@acme.com" else None,
    )
    monkeypatch.setattr("openexecutive.integrations.attachments._schedule_ingest", lambda *_: None)
    if briefing is not None:
        monkeypatch.setattr("openexecutive.integrations.channel_context.attach_briefing_context", briefing)

    async def fake_read(data: bytes, *, filename: str = "", inbound: bool = False) -> PdfReadResult:
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
                sender_email="bob@acme.com",
                direct=direct,
                one_on_one=one_on_one,
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
    assert captured["user_message"] == wrap_untrusted(
        "[Attached: scan.pdf] (converted from scanned pages)\nAPPRAISED VALUE 4.2M",
        source="attachment", author="scan.pdf",
    ) + "\n\nWhat is this worth?"


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


def test_files_from_someone_off_the_roster_are_not_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook authenticates Google, not the sender: anyone in a space
    can post. Their files are named to the Executive, never downloaded."""
    def download(*_a: Any) -> bytes:
        raise AssertionError("an off-roster sender's file must not be downloaded")

    captured = _run_with_attachments(monkeypatch, [_UPLOADED], download, rostered=False)

    assert captured["user_message"] == (
        "(Attached files, not read — files are read only from people on the team: scan.pdf)"
        "\n\nWhat is this worth?"
    )


@pytest.mark.parametrize(
    ("direct", "one_on_one"),
    [(True, True), (False, False), (True, False)],
    ids=["dm-with-the-app", "space", "two-peoples-dm"],
)
def test_a_sender_on_people_is_signed_in_as_on_slack(
    monkeypatch: pytest.MonkeyPatch, direct: bool, one_on_one: bool
) -> None:
    """Google signed the event and named its sender: someone on People is the
    turn's verified caller, in a space or a DM. Only a DM between the app and
    them alone is private; anywhere else others write in, so it is shared."""
    from openexecutive.delegation.settings import private_conversation
    from openexecutive.orchestrator.content_trust import (
        PRINCIPAL_ONLY_TOOLS,
        principal_only_withheld,
    )
    from openexecutive.orchestrator.people_tools import _is_verified_speaker_surface

    briefed: list[bool] = []

    def briefing(_session: Any, *, is_dm: bool, person: Any) -> str:
        briefed.append(is_dm)
        return "BOARD" if is_dm else ""

    captured = _run_with_attachments(
        monkeypatch, [], lambda *_a: b"", rostered=True, direct=direct, one_on_one=one_on_one,
        briefing=briefing,
    )

    session = captured["session"]
    assert session.caller_person_id == 3
    assert captured["person_id"] == 3
    assert session.origin_channel_ref == "spaces/AAA"
    assert _is_verified_speaker_surface(session) is True
    assert private_conversation(session) is one_on_one
    assert session.shared_chat is (not one_on_one)
    assert session.others_spoke_last is (not one_on_one)
    if not one_on_one:
        # Whoever speaks: others write into what the turn reads.
        assert principal_only_withheld(session) == PRINCIPAL_ONLY_TOOLS
    assert briefed == [one_on_one]
    assert captured["briefing_context"] == ("BOARD" if one_on_one else "")


@pytest.mark.parametrize("direct", [True, False])
def test_a_sender_off_people_stays_unverified(monkeypatch: pytest.MonkeyPatch, direct: bool) -> None:
    """Anyone in a space can post, and the email of someone not on People
    proves nothing about who they are to this company: no caller, nothing
    private, and the department gate holds what they ask to send."""
    from openexecutive.delegation.settings import private_conversation
    from openexecutive.orchestrator.department_gate import speaker
    from openexecutive.orchestrator.people_tools import _is_verified_speaker_surface

    captured = _run_with_attachments(
        monkeypatch, [], lambda *_a: b"", rostered=False, direct=direct, one_on_one=direct,
        briefing=lambda _s, *, is_dm, person: "BOARD" if is_dm else "",
    )

    session = captured["session"]
    assert session.caller_person_id is None
    assert captured["person_id"] is None
    assert _is_verified_speaker_surface(session) is False
    assert private_conversation(session) is False
    assert captured["briefing_context"] == ""
    held = speaker(session)
    assert held is not None and held.id is None


@pytest.mark.parametrize(("person", "expected"), [(SimpleNamespace(id=42), 42), (None, None)])
def test_the_inbound_row_carries_the_senders_people_id(
    monkeypatch: pytest.MonkeyPatch, person: Any, expected: int | None,
) -> None:
    """The brief tells the owner's own Google Chat messages from a namesake's
    by this id (briefing.live_signals), never by the display name."""
    rows: list[tuple[str, dict[str, Any]]] = []
    looked_up: list[str] = []

    def _find(email: str) -> Any:
        looked_up.append(email)
        return person

    def _log(kind: str, _summary: str, **kw: Any) -> None:
        rows.append((kind, kw.get("details") or {}))

    monkeypatch.setattr("openexecutive.people.store.find_person_by_email", _find)
    monkeypatch.setattr("openexecutive.audit.log_event", _log)
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda _e: None)
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: (_ for _ in ()).throw(RuntimeError("stop")),
    )

    with patch("openexecutive.integrations.google_chat.send_reply", new=lambda *a, **k: None):
        asyncio.run(
            _process_and_reply(
                message_text="Any word on the offer?",
                sender_name="Pat Owner",
                space_name="spaces/AAA",
                thread_name="spaces/AAA/threads/T1",
                message_name="spaces/AAA/messages/M1",
                service_account_file="/fake/key.json",
                service_account_email=None,
                sender_email="pat@x.com",
            )
        )

    assert looked_up == ["pat@x.com"]
    [inbound] = [d for kind, d in rows if kind == "integration_inbound"]
    assert inbound["sender_person_id"] == expected


def _run_turn(monkeypatch: pytest.MonkeyPatch, store: dict, seen: list, text: str, thread: str, *, direct: bool) -> None:
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda _e: None)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: SimpleNamespace(is_empty=lambda: True),
    )
    monkeypatch.setattr(gc_module, "_load_history", lambda sid: list(store.get(sid, [])))

    def save(sid: str, _name: str, _person: Any, _session: Any, said: str, reply: str) -> None:
        store.setdefault(sid, []).extend(
            [{"role": "user", "content": said}, {"role": "assistant", "content": reply}]
        )

    monkeypatch.setattr(gc_module, "_save_turn", save)

    async def fake_chat(**kw: Any) -> str:
        seen.append((kw["session"].session_id, list(kw["session"].conversation_history)))
        return f"reply to {text}"

    mock_exec = MagicMock()
    mock_exec.chat = fake_chat
    with (
        patch("openexecutive.orchestrator.executive.Executive", return_value=mock_exec),
        patch("openexecutive.integrations.google_chat.send_reply", new=lambda *a, **k: None),
    ):
        asyncio.run(
            _process_and_reply(
                message_text=text, sender_name="Bob", space_name="spaces/DM1",
                thread_name=thread, message_name=f"{thread}/messages/M",
                service_account_file=None, service_account_email=None, direct=direct,
            )
        )


def test_a_dm_is_one_conversation_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict = {}
    seen: list = []
    _run_turn(monkeypatch, store, seen, "draft a welcome note", "spaces/DM1/threads/A", direct=True)
    _run_turn(monkeypatch, store, seen, "add a few more bullets", "spaces/DM1/threads/B", direct=True)
    assert seen[0] == ("google_chat:spaces/DM1", [])
    sid, history = seen[1]
    assert sid == "google_chat:spaces/DM1"
    assert [m["content"] for m in history] == ["draft a welcome note", "reply to draft a welcome note"]


def test_in_a_space_each_thread_is_its_own_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict = {}
    seen: list = []
    _run_turn(monkeypatch, store, seen, "first", "spaces/DM1/threads/A", direct=False)
    _run_turn(monkeypatch, store, seen, "second", "spaces/DM1/threads/B", direct=False)
    _run_turn(monkeypatch, store, seen, "third", "spaces/DM1/threads/A", direct=False)
    assert seen[1] == ("google_chat:spaces/DM1:spaces/DM1/threads/B", [])
    assert [m["content"] for m in seen[2][1]] == ["first", "reply to first"]


def test_the_webhook_marks_a_dm_as_direct(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    calls: list = []
    monkeypatch.setattr(gc_module, "_process_and_reply", lambda **kw: calls.append(kw))
    event = _message_event()
    event["space"] = {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}
    assert client.post("/webhook/google-chat", json=event, headers={"Authorization": "Bearer t"}).status_code == 200
    room = _message_event()
    assert client.post("/webhook/google-chat", json=room, headers={"Authorization": "Bearer t"}).status_code == 200
    assert [c["direct"] for c in calls] == [True, False]
    assert [c["one_on_one"] for c in calls] == [False, False]
    alone = _message_event()
    alone["space"] = {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE", "singleUserBotDm": True}
    assert client.post("/webhook/google-chat", json=alone, headers={"Authorization": "Bearer t"}).status_code == 200
    assert calls[-1]["direct"] is True and calls[-1]["one_on_one"] is True


def test_the_token_is_checked_against_chats_own_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat signs with chat@system.gserviceaccount.com's keys, not Google's
    OAuth ones that ``verify_token`` reads by default."""
    seen: dict[str, Any] = {}

    def verify_token(token: str, _request: Any, **kw: Any) -> dict[str, str]:
        seen.update(kw, token=token)
        return {"iss": "chat@system.gserviceaccount.com"}

    monkeypatch.setattr("google.oauth2.id_token.verify_token", verify_token)
    gc_module.verify_google_jwt("tok", "123")
    assert seen == {
        "token": "tok",
        "audience": "123",
        "certs_url": "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com",
    }


@pytest.mark.parametrize(("principal", "expected"), [(True, (True, None)), (False, (False, 3))])
def test_rows_before_the_session_follow_the_senders_turn(
    monkeypatch: pytest.MonkeyPatch, principal: bool, expected: tuple[bool, int | None]
) -> None:
    """As on the other chat channels (``audit_rows_on_senders_turn``): the
    principal's turn scopes rows that name a contact to them, a teammate's
    rows are theirs alone."""
    from openexecutive.audit import context as audit_context

    person = SimpleNamespace(id=3, is_principal=principal, archived=False, kind="team")
    monkeypatch.setattr("openexecutive.people.store.find_person_by_email", lambda _e, **_k: person)
    monkeypatch.setattr("openexecutive.orchestrator.people_tools.team_member_id", lambda pid: None if principal else pid)
    seen: list[tuple[bool, int | None]] = []

    async def answer(**_kw: Any) -> None:
        seen.append((audit_context.rows_on_principal_turn(), audit_context.rows_owner()))

    monkeypatch.setattr(gc_module, "_answer", answer)
    asyncio.run(
        _process_and_reply(
            message_text="hi", sender_name="Pat", space_name="spaces/AAA", thread_name="",
            message_name="m", service_account_file=None, service_account_email=None,
            sender_email="pat@x.com",
        )
    )
    assert seen == [expected]


# ---------------------------------------------------------------------------
# Approve and Skip buttons (integrations.channel_cards)
# ---------------------------------------------------------------------------


def _tap_event(*, alone: bool = True, user_type: str = "HUMAN", data: str = "card:12:a") -> dict:
    space: dict[str, Any] = {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}
    if alone:
        space["singleUserBotDm"] = True
    return {
        "type": "CARD_CLICKED",
        "common": {"invokedFunction": "oe_card", "parameters": {"card": data}},
        "message": {"name": "spaces/DM1/messages/C1", "text": "Meeting to book\nBoard prep"},
        "space": space,
        "user": {"name": "users/111", "displayName": "Pat", "email": "pat@acme.com", "type": user_type},
    }


def test_a_tap_in_the_apps_own_dm_is_taken(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """The card says it's being worked on at once, without its buttons, and
    the tap is decided after the answer."""
    taps: list[dict] = []

    async def decide(**kw: Any) -> None:
        taps.append(kw)

    monkeypatch.setattr(gc_module, "_decide_tap", decide)
    resp = client.post("/webhook/google-chat", json=_tap_event(), headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert resp.json() == {
        "actionResponse": {"type": "UPDATE_MESSAGE"},
        "text": "Meeting to book\nBoard prep\n\nWorking on it…",
        "cardsV2": [],
    }
    [tap] = taps
    assert tap["data"] == "card:12:a"
    assert tap["message_name"] == "spaces/DM1/messages/C1"
    assert tap["sender_email"] == "pat@acme.com"
    assert tap["space_name"] == "spaces/DM1"
    assert tap["user_name"] == "users/111"


@pytest.mark.parametrize(
    "event",
    [_tap_event(alone=False), _tap_event(user_type="BOT"), _tap_event(user_type="")],
    ids=["two-peoples-dm", "a-bot", "no-user-type"],
)
def test_a_tap_anywhere_else_decides_nothing(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, event: dict
) -> None:
    """Only a human, in a DM between the app and them alone: elsewhere the
    tapper isn't proven to be the card's person."""
    monkeypatch.setattr(gc_module, "_decide_tap", lambda **_kw: pytest.fail("decided a tap it shouldn't"))
    resp = client.post("/webhook/google-chat", json=event, headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Meeting to book\nBoard prep"


def test_a_tap_on_something_else_is_left_alone(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setattr(gc_module, "_decide_tap", lambda **_kw: pytest.fail("not one of ours"))
    event = _tap_event()
    event["common"] = {"invokedFunction": "something_else"}
    assert client.post("/webhook/google-chat", json=event, headers={"Authorization": "Bearer t"}).json() == {}


def test_the_older_tap_shape_is_read_too() -> None:
    """A classic event may carry the button's data as ``action.parameters``."""
    body = {"action": {"actionMethodName": "oe_card", "parameters": [{"key": "card", "value": "card:7:s"}]}}
    assert gc_module._tapped(body) == "card:7:s"
    assert gc_module._tapped({"action": {"actionMethodName": "oe_card"}}) is None


@pytest.mark.parametrize(
    ("person", "confirmed", "decided"),
    [(SimpleNamespace(id=3), "Meeting to book", True), (None, "Meeting to book", False),
     (SimpleNamespace(id=3), None, False)],
    ids=["confirmed", "not-on-people", "chat-didnt-confirm"],
)
def test_a_tap_is_decided_only_once_chat_confirms_it(
    monkeypatch: pytest.MonkeyPatch, person: Any, confirmed: str | None, decided: bool
) -> None:
    """The tapper is the person on People with the event's email, and only
    when Chat itself confirms the message, the space and its one person. The
    card is rewritten from Chat's own words, with the outcome under them;
    a tap that isn't confirmed decides nothing, and its card says so."""
    from openexecutive.integrations import channel_cards

    resolved: list[tuple] = []
    updated: list[tuple] = []
    checked: list[tuple] = []
    rows: list[dict] = []

    async def resolve(decision_id: int, who: Any, *, approve: bool, allow: bool = False) -> str:
        resolved.append((decision_id, who, approve, allow))
        return "Approved."

    def verify(*a: Any) -> str | None:
        checked.append(a)
        return confirmed

    monkeypatch.setattr("openexecutive.people.store.find_person_by_email", lambda _e, **_k: person)
    monkeypatch.setattr(channel_cards, "resolve", resolve)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: rows.append(k.get("details") or {}))
    monkeypatch.setattr(gc_module, "verify_tap", verify)
    monkeypatch.setattr(gc_module, "update_card", lambda *a: updated.append(a))

    asyncio.run(gc_module._decide_tap(
        data="card:12:a", space_name="spaces/DM1", message_name="spaces/DM1/messages/C1",
        sender_email="pat@acme.com", user_name="users/111",
        service_account_file="/k.json", service_account_email=None,
    ))

    assert checked == ([("spaces/DM1", "spaces/DM1/messages/C1", "card:12:a", "users/111", "/k.json", None)]
                       if person else [])
    assert resolved == ([(12, person, True, False)] if decided else [])
    assert updated == [(
        "spaces/DM1/messages/C1",
        "Meeting to book\n\nApproved." if decided else gc_module._UNCONFIRMED,
        "/k.json", None,
    )]
    [row] = rows
    assert row["google_user"] == "users/111" and row["space_name"] == "spaces/DM1"
    assert row.get("rejected", False) is (not decided)


class _FakeChat:
    """``spaces().messages().get``, ``spaces().get`` and ``spaces().members().list``."""

    def __init__(self, message: dict, space: dict, members: list[dict]) -> None:
        self._answers = {"message": message, "space": space, "members": {"memberships": members}}

    def _call(self, key: str) -> Any:
        return SimpleNamespace(execute=lambda: self._answers[key])

    def spaces(self) -> Any:
        return SimpleNamespace(
            messages=lambda: SimpleNamespace(get=lambda name: self._call("message")),
            get=lambda name: self._call("space"),
            members=lambda: SimpleNamespace(list=lambda parent: self._call("members")),
        )


_OUR_CARD = {
    "sender": {"type": "BOT"},
    "text": "Meeting to book\n\nWorking on it…",
    "cardsV2": gc_module._card_buttons(12, ("Approve", "Skip")),
}
_PAT = [{"member": {"name": "users/111", "type": "HUMAN"}}, {"member": {"name": "users/app", "type": "BOT"}}]


@pytest.mark.parametrize(
    ("message", "space", "members", "message_name", "expected"),
    [
        (_OUR_CARD, {"singleUserBotDm": True}, _PAT, "spaces/DM1/messages/C1", "Meeting to book"),
        ({**_OUR_CARD, "sender": {"type": "HUMAN"}}, {"singleUserBotDm": True}, _PAT, "spaces/DM1/messages/C1", None),
        ({**_OUR_CARD, "cardsV2": gc_module._card_buttons(13, ("Approve", "Skip"))}, {"singleUserBotDm": True},
         _PAT, "spaces/DM1/messages/C1", None),
        (_OUR_CARD, {"singleUserBotDm": False}, _PAT, "spaces/DM1/messages/C1", None),
        (_OUR_CARD, {"singleUserBotDm": True}, [{"member": {"name": "users/222", "type": "HUMAN"}}],
         "spaces/DM1/messages/C1", None),
        (_OUR_CARD, {"singleUserBotDm": True}, _PAT, "spaces/OTHER/messages/C1", None),
    ],
    ids=["ours", "not-the-apps", "another-card", "not-a-dm-alone", "someone-else", "another-space"],
)
def test_chat_itself_confirms_a_tap(
    message: dict, space: dict, members: list[dict], message_name: str, expected: str | None
) -> None:
    with (
        patch.object(gc_module, "_build_credentials", return_value="creds"),
        patch("googleapiclient.discovery.build", return_value=_FakeChat(message, space, members)),
    ):
        assert gc_module.verify_tap("spaces/DM1", message_name, "card:12:a", "users/111", None) == expected


def test_an_unconfirmed_tap_elsewhere_rewrites_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message outside the event's space is never touched."""
    updated: list[tuple] = []
    monkeypatch.setattr("openexecutive.people.store.find_person_by_email", lambda _e, **_k: SimpleNamespace(id=3))
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr(gc_module, "verify_tap", lambda *a: None)
    monkeypatch.setattr(gc_module, "update_card", lambda *a: updated.append(a))
    asyncio.run(gc_module._decide_tap(
        data="card:12:a", space_name="spaces/DM1", message_name="spaces/OTHER/messages/C1",
        sender_email="pat@acme.com", user_name="users/111", service_account_file=None, service_account_email=None,
    ))
    assert updated == []


def test_a_check_chat_cant_answer_is_a_no() -> None:
    with (
        patch.object(gc_module, "_build_credentials", side_effect=RuntimeError("no creds")),
    ):
        assert gc_module.verify_tap("spaces/DM1", "spaces/DM1/messages/C1", "card:12:a", "users/111", None) is None


def test_a_cards_words_cannot_hide_a_link() -> None:
    assert "<" not in gc_module.card_safe("Agenda: <https://evil.example|Open the agenda>")


def test_a_card_is_sent_with_its_buttons() -> None:
    service = MagicMock()
    with (
        patch.object(gc_module, "_build_credentials", return_value="creds"),
        patch("googleapiclient.discovery.build", return_value=service),
    ):
        gc_module.send_card("spaces/DM1", "spaces/DM1/threads/T", 12, "Meeting to book", ("Approve", "Skip"), None)

    kwargs = service.spaces.return_value.messages.return_value.create.call_args.kwargs
    assert kwargs["parent"] == "spaces/DM1"
    body = kwargs["body"]
    assert body["text"] == "Meeting to book"
    assert body["thread"] == {"name": "spaces/DM1/threads/T"}
    buttons = body["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Approve", "Skip"]
    assert [b["onClick"]["action"]["parameters"][0]["value"] for b in buttons] == ["card:12:a", "card:12:s"]
    assert {b["onClick"]["action"]["function"] for b in buttons} == {"oe_card"}


@pytest.mark.parametrize("one_on_one", [True, False])
def test_the_replys_cards_follow_it_only_in_the_apps_own_dm(
    monkeypatch: pytest.MonkeyPatch, one_on_one: bool
) -> None:
    """The cards a reply leaves go under it as messages with buttons in a DM
    between the app and its person alone; anywhere else none are collected."""
    monkeypatch.setattr(
        "openexecutive.integrations.channel_cards._dm_proves_taps", lambda channel: channel == "google_chat"
    )
    monkeypatch.setattr("openexecutive.integrations.channel_cards.card_text", lambda _i: "Meeting to book")
    monkeypatch.setattr("openexecutive.integrations.channel_cards.card_buttons", lambda _i: ("Approve", "Skip"))
    sent: list[tuple] = []
    monkeypatch.setattr(gc_module, "send_card", lambda *a: sent.append(a))
    monkeypatch.setattr("openexecutive.alerts.pipeline.schedule_evaluation", lambda _e: None)
    monkeypatch.setattr("openexecutive.audit.log_event", lambda *a, **k: None)
    monkeypatch.setattr("openexecutive.knowledge.retriever.retrieve", lambda **_: "")
    monkeypatch.setattr("openexecutive.memory.episodic.format_for_prompt", lambda: "")
    monkeypatch.setattr(
        "openexecutive.onboarding.profile_builder.load_or_create_profile",
        lambda: SimpleNamespace(is_empty=lambda: True),
    )
    monkeypatch.setattr(
        "openexecutive.people.store.find_person_by_email", lambda _e, **_k: SimpleNamespace(id=3),
    )
    monkeypatch.setattr("openexecutive.integrations.channel_context.attach_briefing_context", lambda *a, **k: "")
    seen: dict[str, Any] = {}

    async def fake_chat(**kw: Any) -> str:
        seen.update(kw)
        if kw["cards"] is not None:
            kw["cards"].append(12)
        return "Booked if you say so."

    mock_exec = MagicMock()
    mock_exec.chat = fake_chat
    with (
        patch("openexecutive.orchestrator.executive.Executive", return_value=mock_exec),
        patch("openexecutive.integrations.google_chat.send_reply", new=lambda *a, **k: None),
    ):
        asyncio.run(
            _process_and_reply(
                message_text="book the board prep", sender_name="Pat", space_name="spaces/DM1",
                thread_name="spaces/DM1/threads/T", message_name="spaces/DM1/messages/M",
                service_account_file=None, service_account_email=None, sender_email="pat@acme.com",
                direct=True, one_on_one=one_on_one,
            )
        )

    assert (seen["cards"] is not None) is one_on_one
    assert ("Approve and Skip buttons" in seen["channel_context_block"]) is one_on_one
    assert sent == ([("spaces/DM1", "spaces/DM1/threads/T", 12, "Meeting to book", ("Approve", "Skip"), None, None)]
                    if one_on_one else [])
