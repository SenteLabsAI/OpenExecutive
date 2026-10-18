"""Speech to text: where it goes, what it sends, and what a voice note
becomes in a chat turn."""
from __future__ import annotations

import struct
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
        transcription_max_mb_per_day=200,
        openrouter_api_key=None,
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_referer=None,
        openrouter_app_title="Open Executive",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def fresh_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcription, "_SENT_TODAY", {})
    monkeypatch.setattr(transcription, "_in_flight", 0)


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


def _ogg_opus(seconds: float) -> bytes:
    def page(granule: int, payload: bytes) -> bytes:
        return b"OggS" + bytes([0, 0]) + struct.pack("<q", granule) + b"\0" * 12 + bytes([1, len(payload)]) + payload

    head = b"OpusHead" + bytes([1, 1]) + struct.pack("<H", 312) + b"\0" * 8
    return page(0, head) + page(312 + int(seconds * 48_000), b"audio")


def _wav(seconds: float) -> bytes:
    rate = 8_000
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"\0" * int(seconds * rate * 2)
    return b"RIFF" + struct.pack("<I", 36 + len(body)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(body)) + body


def _m4a(seconds: float) -> bytes:
    mvhd_body = b"\0" * 4 + b"\0" * 8 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\0" * 80
    mvhd = struct.pack(">I", 8 + len(mvhd_body)) + b"mvhd" + mvhd_body
    ftyp = struct.pack(">I", 16) + b"ftypM4A \0\0\0\0"
    mdat = struct.pack(">I", 12) + b"mdat" + b"mvhd"  # the name inside audio is not the box
    return ftyp + mdat + struct.pack(">I", 8 + len(mvhd)) + b"moov" + mvhd


def test_a_recordings_length_is_read_from_its_container() -> None:
    assert transcription.recording_seconds(_ogg_opus(42.0), "v.ogg") == pytest.approx(42.0)
    assert transcription.recording_seconds(_wav(1.5), "v.wav") == pytest.approx(1.5)
    assert transcription.recording_seconds(_m4a(90.0), "v.m4a") == pytest.approx(90.0)
    assert transcription.recording_seconds(b"ID3\x04" + b"\0" * 100, "v.mp3") is None
    assert transcription.recording_seconds(b"OggS", "v.ogg") is None


async def test_a_long_recording_is_refused_by_its_real_length(settings, server) -> None:
    # The cap is the server's, not the browser's: a long file posted
    # straight to the route is measured before anything is sent.
    settings(openrouter_api_key="or-key", transcription_max_seconds=300)
    with pytest.raises(transcription.TranscriptionError, match="5 minutes"):
        await transcription.transcribe(_ogg_opus(301), "voice.ogg", "audio/ogg")
    with pytest.raises(transcription.TranscriptionError, match="5 minutes"):
        await transcription.transcribe(_m4a(3600), "call.m4a")
    assert server["requests"] == []
    assert await transcription.transcribe(_ogg_opus(299), "voice.ogg") == "move my call"


async def test_a_recording_of_unreadable_length_is_held_to_a_size(settings, server) -> None:
    settings(openrouter_api_key="or-key", transcription_max_seconds=10)
    with pytest.raises(transcription.TranscriptionError, match="10 seconds"):
        await transcription.transcribe(b"ID3" + b"0" * 160_001, "call.mp3")
    assert server["requests"] == []
    assert await transcription.transcribe(b"ID3" + b"0" * 1000, "call.mp3") == "move my call"


async def test_the_days_audio_is_capped_by_bytes(settings, server) -> None:
    # A file can state any length, so bytes are the bound that holds.
    settings(openrouter_api_key="or-key", transcription_max_mb_per_day=1)
    half = _wav(0.1) + b"\0" * (400 * 1024)
    assert await transcription.transcribe(half, "a.wav") == "move my call"
    server["answer"] = (500, {"error": "down"})
    with pytest.raises(transcription.TranscriptionError, match="service"):
        await transcription.transcribe(half, "b.wav")  # still sent, still counted
    with pytest.raises(transcription.TranscriptionError, match="today's limit"):
        await transcription.transcribe(half, "c.wav")
    assert len(server["requests"]) == 2


def test_audio_is_known_by_type_or_name() -> None:
    assert transcription.is_audio("x.bin", "audio/ogg; codecs=opus")
    assert transcription.is_audio("voice.m4a")
    assert not transcription.is_audio("deck.pdf", "application/pdf")


# ── In a chat turn ──────────────────────────────────────────────────────────


async def test_a_voice_note_becomes_the_senders_words(settings, server) -> None:
    settings(openrouter_api_key="or-key")
    heard: list[str] = []

    text, blocks = await attachments.build_attachment_output(
        "voice.ogg", b"OggS", "audio/ogg", heard=heard, voice_note=True
    )

    assert blocks == []
    assert text == "[Voice note, transcribed]\nmove my call"
    assert heard == ["move my call"]


async def test_a_voice_note_without_transcription_asks_for_text(settings) -> None:
    heard: list[str] = []
    text, _ = await attachments.build_attachment_output(
        "voice.ogg", b"OggS", "audio/ogg", heard=heard, voice_note=True
    )
    assert "could not be transcribed" in text and "type it" in text
    assert heard == []


async def test_an_audio_file_is_read_as_a_files_words(settings, server) -> None:
    # A recorded call or a forwarded clip may hold someone else's voice, so
    # its words are wrapped like a document's, never taken as the sender's.
    settings(openrouter_api_key="or-key")
    heard: list[str] = []

    text, _ = await attachments.build_attachment_output(
        "board-call.m4a", b"m4a", "audio/mp4", heard=heard
    )

    assert heard == []
    assert "[Voice note" not in text
    assert "board-call.m4a" in text and "move my call" in text
    assert '<untrusted_content source="attachment"' in text


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
    assert files == [("V1", "voice.ogg", "audio/ogg", True)]
    _, files = _message_content({"video_note": {"file_id": "N1"}})
    assert files == [("N1", "video_note.mp4", "audio/mp4", True)]
    _, files = _message_content({"audio": {"file_id": "A1", "file_name": "call.mp3"}})
    assert files == [("A1", "call.mp3", "audio/mpeg", False)]


async def test_only_a_recorded_voice_note_is_the_senders_speech(settings, server, monkeypatch) -> None:
    # Discord voice messages and Slack clips are marked by the adapter; an
    # uploaded audio file on the same channel is not.
    settings(openrouter_api_key="or-key")

    async def fake_download(url: str, **_: Any) -> bytes:
        return b"OggS"

    monkeypatch.setattr(attachments, "download_bytes", fake_download)
    heard: list[str] = []
    text, _ = await attachments.process_attachments(
        [
            attachments.AttachmentItem(url="u1", filename="voice-message.ogg", content_type="audio/ogg", voice_note=True),
            attachments.AttachmentItem(url="u2", filename="podcast.mp3", content_type="audio/mpeg"),
        ],
        heard=heard,
    )
    assert heard == ["move my call"]
    assert "[Voice note, transcribed]\nmove my call" in text
    assert "[Attached: podcast.mp3" in text

