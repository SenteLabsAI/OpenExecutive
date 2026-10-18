"""The chat mic's two routes: whether it shows, and turning a recording into
words for the message box."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import chat as chat_route
from openexecutive.providers import transcription


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_route.router)
    return TestClient(app)


@pytest.fixture()
def speech(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"available": True, "sent": [], "error": None}

    async def fake_transcribe(data: bytes, filename: str, content_type: str = "", **_: Any) -> str:
        if state["error"]:
            raise transcription.TranscriptionError(state["error"])
        state["sent"].append((data, filename, content_type))
        return "Move my call with Dana to Thursday."

    monkeypatch.setattr(transcription, "available", lambda: state["available"])
    monkeypatch.setattr(transcription, "max_seconds", lambda: 300)
    monkeypatch.setattr(transcription, "transcribe", fake_transcribe)
    return state


def test_voice_status_says_whether_the_mic_shows(speech) -> None:
    assert _client().get("/chat/voice").json() == {"available": True, "max_seconds": 300}
    speech["available"] = False
    assert _client().get("/chat/voice").json()["available"] is False


def test_a_recording_comes_back_as_words(speech) -> None:
    resp = _client().post(
        "/chat/transcribe", files={"file": ("recording.webm", b"webm-bytes", "audio/webm")}
    )
    assert resp.status_code == 200
    assert resp.json() == {"text": "Move my call with Dana to Thursday."}
    assert speech["sent"] == [(b"webm-bytes", "recording.webm", "audio/webm")]


def test_without_speech_to_text_the_route_is_absent(speech) -> None:
    speech["available"] = False
    resp = _client().post(
        "/chat/transcribe", files={"file": ("recording.webm", b"x", "audio/webm")}
    )
    assert resp.status_code == 404
    assert speech["sent"] == []


def test_only_recordings_are_taken(speech) -> None:
    resp = _client().post(
        "/chat/transcribe", files={"file": ("deck.pdf", b"%PDF", "application/pdf")}
    )
    assert resp.status_code == 400
    assert speech["sent"] == []


def test_a_recording_over_the_cap_is_refused(speech, monkeypatch) -> None:
    monkeypatch.setattr(transcription, "MAX_BYTES", 8)
    resp = _client().post(
        "/chat/transcribe", files={"file": ("recording.webm", b"0" * 9, "audio/webm")}
    )
    assert resp.status_code == 413
    assert speech["sent"] == []


def test_a_failed_transcription_says_why(speech) -> None:
    speech["error"] = "the speech service didn't answer"
    resp = _client().post(
        "/chat/transcribe", files={"file": ("recording.webm", b"x", "audio/webm")}
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Couldn't hear it: the speech service didn't answer"
