"""Speech to text: where it goes, what it sends, and what a voice note
becomes in a chat turn."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from openexecutive import config
from openexecutive.integrations import attachments
from openexecutive.providers import transcription


def _settings(**over: Any) -> SimpleNamespace:
    base = dict(
        transcription_base_url=None,
        transcription_api_key=None,
        transcription_model=None,
        transcription_max_seconds=300,
        openrouter_api_key=None,
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_referer=None,
        openrouter_app_title="Open Executive",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch):
    current = {"s": _settings()}
    monkeypatch.setattr(config, "get_settings", lambda: current["s"])

    def use(**over: Any) -> None:
        current["s"] = _settings(**over)

    return use


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch):
    """A fake /audio/transcriptions; every request is kept."""
    seen: dict[str, Any] = {"requests": [], "answer": (200, {"text": " move my call ", "usage": {"seconds": 4.2, "cost": 0.0003}})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request)
        status, body = seen["answer"]
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        transcription.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "openexecutive.audit.usage.log_model_usage",
        lambda message, **kw: rows.append({"usage": message.usage, **kw}),
    )
    seen["rows"] = rows
    return seen


def test_nothing_set_up_means_no_transcription(settings) -> None:
    assert transcription.target() is None
    assert transcription.available() is False


def test_openrouter_is_the_default_when_its_key_is_set(settings) -> None:
    settings(openrouter_api_key="or-key")
    where = transcription.target()
    assert where is not None and where.via_openrouter
    assert where.base_url == "https://openrouter.ai/api/v1"
    assert where.model == transcription.OPENROUTER_MODEL


def test_a_named_server_wins_over_openrouter(settings) -> None:
    settings(openrouter_api_key="or-key", transcription_base_url="http://whisper.local:8000/v1/")
    where = transcription.target()
    assert where is not None and not where.via_openrouter
    assert where.base_url == "http://whisper.local:8000/v1"
    assert where.api_key is None
    assert where.model == transcription.DEFAULT_MODEL


async def test_a_recording_is_sent_as_a_standard_upload(settings, server) -> None:
    settings(openrouter_api_key="or-key")
    text = await transcription.transcribe(b"OggS-voice", "voice.ogg", "audio/ogg")

    assert text == "move my call"
    [req] = server["requests"]
    assert str(req.url) == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer or-key"
    body = req.read()
    assert b'name="model"' in body and transcription.OPENROUTER_MODEL.encode() in body
    assert b'filename="voice.ogg"' in body and b"OggS-voice" in body
    [row] = server["rows"]
    assert row["actor"] == "transcription"
    assert row["usage"].cost == 0.0003


async def test_usage_listeners_hear_of_each_call(settings, server) -> None:
    settings(openrouter_api_key="or-key")
    heard: list[transcription.Usage] = []
    transcription.add_usage_listener(heard.append)
    try:
        await transcription.transcribe(b"x", "voice.ogg")
    finally:
        transcription._listeners.remove(heard.append)
    assert heard == [transcription.Usage(transcription.OPENROUTER_MODEL, True, 4.2, 0.0003)]


async def test_a_server_error_is_a_transcription_error(settings, server) -> None:
    settings(transcription_base_url="http://whisper.local/v1")
    server["answer"] = (500, {"error": "down"})
    with pytest.raises(transcription.TranscriptionError):
        await transcription.transcribe(b"x", "voice.ogg")


async def test_too_long_or_too_large_is_refused_before_sending(settings, server) -> None:
    settings(openrouter_api_key="or-key", transcription_max_seconds=300)
    with pytest.raises(transcription.TranscriptionError, match="5 minutes"):
        await transcription.transcribe(b"x", "voice.ogg", duration_s=301)
    with pytest.raises(transcription.TranscriptionError, match="MB"):
        await transcription.transcribe(b"0" * (transcription.MAX_BYTES + 1), "voice.ogg")
    assert server["requests"] == []


def test_audio_is_known_by_type_or_name() -> None:
    assert transcription.is_audio("x.bin", "audio/ogg; codecs=opus")
    assert transcription.is_audio("voice.m4a")
    assert not transcription.is_audio("deck.pdf", "application/pdf")


# ── In a chat turn ──────────────────────────────────────────────────────────


async def test_a_voice_note_becomes_the_senders_words(settings, server) -> None:
    settings(openrouter_api_key="or-key")
    heard: list[str] = []

    text, blocks = await attachments.build_attachment_output(
        "voice.ogg", b"OggS", "audio/ogg", heard=heard
    )

    assert blocks == []
    assert text == "[Voice note, transcribed]\nmove my call"
    assert heard == ["move my call"]


async def test_a_voice_note_without_transcription_asks_for_text(settings) -> None:
    heard: list[str] = []
    text, _ = await attachments.build_attachment_output("voice.ogg", b"OggS", "audio/ogg", heard=heard)
    assert "could not be transcribed" in text and "type it" in text
    assert heard == []


def test_the_reply_opens_with_what_was_heard() -> None:
    assert attachments.with_heard("On it.", ["move my call"]) == 'Heard: "move my call"\n\nOn it.'
    assert attachments.with_heard("On it.", []) == "On it."
    assert attachments.with_heard("", ["x"]) == ""
    long = attachments.with_heard("ok", ["word " * 100])
    assert long.startswith('Heard: "word') and "…" in long.split("\n")[0]


def test_telegram_voice_notes_are_read_as_audio() -> None:
    from openexecutive.integrations.telegram_bot import _message_content

    text, files = _message_content({"voice": {"file_id": "V1", "mime_type": "audio/ogg", "duration": 4}})
    assert text == ""
    assert files == [("V1", "voice.ogg", "audio/ogg")]
    _, files = _message_content({"video_note": {"file_id": "N1"}})
    assert files == [("N1", "video_note.mp4", "audio/mp4")]
