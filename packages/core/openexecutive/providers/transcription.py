"""Speech to text: voice notes from the chat apps and email, and the chat mic.

Claude takes no audio, so a voice note becomes words here first. Any server
with the standard ``POST /audio/transcriptions`` call works (a multipart
``file`` and ``model``, answering ``{"text": …}``): OpenAI, Groq, a Whisper
server run locally, or OpenRouter, which is the default whenever
``OPENROUTER_API_KEY`` is set and nothing else is. With no server there is
no transcription: ``available()`` is false, the chat mic is hidden and a
voice note gets a line asking for text.

Each call is recorded like a model call (one ``cache_event`` row, actor
``transcription``, with the cost the server reports), so ``/audit/usage``
counts it. A deployment that keeps its own ledger can also hear of every
call through ``add_usage_listener``.
"""
from __future__ import annotations

import logging
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# What the transcription servers take, by suffix. Telegram and Discord voice
# notes are Ogg Opus; Slack clips and phone recordings are mp4 / m4a; a
# browser records webm (Chrome, Firefox) or mp4 (Safari).
AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".wav": "audio/wav", ".webm": "audio/webm", ".flac": "audio/flac",
    ".aac": "audio/aac",
}
# The servers' own upload cap.
MAX_BYTES = 25 * 1024 * 1024
OPENROUTER_MODEL = "elevenlabs/scribe-v2"
DEFAULT_MODEL = "whisper-1"
_TIMEOUT_S = 90.0
# The servers bill by length, and a file's size says little about it (25 MB
# of 8 kbps audio is seven hours). A recording whose length can't be read
# from its container (mp3, flac, aac) is held to the size it would have at
# 128 kbps over the time limit, so a low-bitrate file can't run far past it.
_UNREAD_BYTES_PER_SECOND = 16_000


class TranscriptionError(Exception):
    """A voice note that could not be turned into words; ``str()`` is a
    short reason fit for the person who sent it."""


@dataclass(frozen=True)
class Target:
    base_url: str
    api_key: str | None
    model: str
    via_openrouter: bool


@dataclass(frozen=True)
class Usage:
    """One transcription, for a usage listener."""

    model: str
    via_openrouter: bool
    seconds: float | None
    cost_usd: float | None


_listeners: list[Callable[[Usage], None]] = []


def add_usage_listener(listener: Callable[[Usage], None]) -> None:
    """Call ``listener`` after every transcription (it must not raise)."""
    _listeners.append(listener)


def target() -> Target | None:
    """Where transcriptions go, or None when nothing is set up."""
    from openexecutive.config import get_settings

    s = get_settings()
    if s.transcription_base_url:
        return Target(
            base_url=s.transcription_base_url.rstrip("/"),
            api_key=s.transcription_api_key or None,
            model=s.transcription_model or DEFAULT_MODEL,
            via_openrouter=False,
        )
    if s.openrouter_api_key:
        return Target(
            base_url=s.openrouter_base_url.rstrip("/"),
            api_key=s.openrouter_api_key,
            model=s.transcription_model or OPENROUTER_MODEL,
            via_openrouter=True,
        )
    return None


def available() -> bool:
    return target() is not None


def max_seconds() -> int:
    from openexecutive.config import get_settings

    return max(1, int(get_settings().transcription_max_seconds))


def is_audio(filename: str, content_type: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct.startswith("audio/") or Path(filename).suffix.lower() in AUDIO_MEDIA_TYPES


def _media_type(filename: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("audio/"):
        return ct
    return AUDIO_MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


async def transcribe(
    data: bytes,
    filename: str,
    content_type: str = "",
    *,
    duration_s: float | None = None,
) -> str:
    """The words in one recording. Raises ``TranscriptionError`` when there
    is no server, the recording is too long or too large, or the server
    fails; returns ``""`` when it heard nothing."""
    where = target()
    if where is None:
        raise TranscriptionError("voice notes aren't set up here")
    if len(data) > MAX_BYTES:
        raise TranscriptionError(f"it's larger than {MAX_BYTES // (1024 * 1024)} MB")
    limit = max_seconds()
    too_long = TranscriptionError(f"it's longer than {_minutes(limit)}")
    if duration_s is not None and duration_s > limit:
        raise too_long
    measured = recording_seconds(data, filename, content_type)
    if measured is not None:
        if measured > limit:
            raise too_long
    elif len(data) > limit * _UNREAD_BYTES_PER_SECOND:
        raise too_long
    if not data:
        raise TranscriptionError("it was empty")

    headers: dict[str, str] = {}
    if where.api_key:
        headers["Authorization"] = f"Bearer {where.api_key}"
    if where.via_openrouter:
        from openexecutive.config import get_settings

        s = get_settings()
        headers["HTTP-Referer"] = s.openrouter_referer or "https://github.com/"
        headers["X-Title"] = s.openrouter_app_title
    name = Path(filename).name or "voice-note"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                f"{where.base_url}/audio/transcriptions",
                headers=headers,
                data={"model": where.model, "response_format": "json"},
                files={"file": (name, data, _media_type(name, content_type))},
            )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("transcription failed: HTTP %s", exc.response.status_code)
        raise TranscriptionError("the speech service didn't answer") from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("transcription failed (%s)", type(exc).__name__)
        raise TranscriptionError("the speech service didn't answer") from exc
    if not isinstance(body, dict):
        raise TranscriptionError("the speech service didn't answer")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    _record(where, usage or {})
    return str(body.get("text") or "").strip()


def recording_seconds(data: bytes, filename: str, content_type: str = "") -> float | None:
    """How long a recording runs, read from its container: Ogg (Opus or
    Vorbis), WebM / Matroska, MP4 / M4A and WAV. None when it can't be read.
    Never raises."""
    try:
        if data[:4] == b"OggS":
            return _ogg_seconds(data)
        if data[:4] == b"\x1a\x45\xdf\xa3":
            return _webm_seconds(data)
        if data[4:8] == b"ftyp":
            return _mp4_seconds(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return _wav_seconds(data)
    except (IndexError, ValueError, struct.error, ZeroDivisionError):
        return None
    return None


def _ogg_seconds(data: bytes) -> float | None:
    # The first page names the codec (and Vorbis its sample rate); the last
    # page's granule position counts samples from the start.
    nsegs = data[26]
    head = data[27 + nsegs: 27 + nsegs + 30]
    if head.startswith(b"OpusHead"):
        rate, skip = 48_000, struct.unpack_from("<H", head, 10)[0]
    elif head.startswith(b"\x01vorbis"):
        rate, skip = struct.unpack_from("<I", head, 12)[0], 0
    else:
        return None
    # "OggS" can turn up inside a packet too: a real page has version 0.
    end = len(data)
    for _ in range(16):
        last = data.rfind(b"OggS", 0, end)
        if last <= 0:
            return None
        if data[last + 4] == 0:
            granule = struct.unpack_from("<q", data, last + 6)[0]
            if granule >= 0 and rate > 0:
                return max(0, granule - skip) / rate
        end = last
    return None


def _vint(data: bytes, pos: int, *, keep_marker: bool) -> tuple[int, int]:
    """One EBML variable-length integer at ``pos``: (value, next pos)."""
    first = data[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8:
        raise ValueError("bad vint")
    value = first if keep_marker else first & (0xFF >> length)
    for b in data[pos + 1: pos + length]:
        value = (value << 8) | b
    return value, pos + length


def _webm_seconds(data: bytes) -> float | None:
    # A browser's recording usually has no Duration; each Cluster starts
    # with its Timecode (ms at the default scale), so the last one is close.
    best: float | None = None
    header = data[:4096]
    at = header.find(b"\x44\x89")  # Segment Info Duration, when written
    if at != -1:
        size, pos = _vint(header, at + 2, keep_marker=False)
        if size in (4, 8):
            scale = 1_000_000
            ts = header.find(b"\x2a\xd7\xb1")
            if ts != -1:
                n, p = _vint(header, ts + 3, keep_marker=False)
                scale = int.from_bytes(header[p:p + n], "big") or scale
            value = struct.unpack_from(">f" if size == 4 else ">d", header, pos)[0]
            if math.isfinite(value) and value >= 0:
                best = value * scale / 1e9
    # The cluster id can turn up inside audio too: a real one is followed
    # by its Timecode element.
    end = len(data)
    for _ in range(16):
        cluster = data.rfind(b"\x1f\x43\xb6\x75", 0, end)
        if cluster == -1:
            break
        _, pos = _vint(data, cluster + 4, keep_marker=False)
        if data[pos] == 0xE7:
            n, pos = _vint(data, pos + 1, keep_marker=False)
            if 1 <= n <= 8:
                seconds = int.from_bytes(data[pos:pos + n], "big") / 1000
                return max(best or 0.0, seconds)
        end = cluster
    return best


def _boxes(data: bytes, start: int, end: int) -> dict[bytes, tuple[int, int]]:
    """The MP4 boxes between ``start`` and ``end``: type -> (body, end)."""
    found: dict[bytes, tuple[int, int]] = {}
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        body = pos + 8
        if size == 1:
            size = struct.unpack_from(">Q", data, body)[0]
            body += 8
        elif size == 0:
            size = end - pos
        if size < body - pos:
            break
        found.setdefault(kind, (body, min(pos + size, end)))
        pos += size
    return found


def _mp4_seconds(data: bytes) -> float | None:
    # moov > mvhd carries the timescale and the length in it.
    moov = _boxes(data, 0, len(data)).get(b"moov")
    if moov is None:
        return None
    mvhd = _boxes(data, *moov).get(b"mvhd")
    if mvhd is None:
        return None
    at = mvhd[0]
    if data[at] == 1:
        timescale, duration = struct.unpack_from(">IQ", data, at + 4 + 16)
    else:
        timescale, duration = struct.unpack_from(">II", data, at + 4 + 8)
    return duration / timescale if timescale else None


def _wav_seconds(data: bytes) -> float | None:
    byte_rate = struct.unpack_from("<I", data, 28)[0]
    at = data.find(b"data", 12)
    if at == -1 or not byte_rate:
        return None
    size = struct.unpack_from("<I", data, at + 4)[0]
    return min(size, len(data) - at - 8) / byte_rate


def _minutes(seconds: int) -> str:
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _record(where: Target, usage: dict[str, Any]) -> None:
    """One usage row for the call, and the listeners told. Never raises."""
    from openexecutive.audit.usage import log_model_usage

    cost = _number(usage.get("cost"))
    seconds = _number(usage.get("seconds"))
    try:
        log_model_usage(
            SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=int(_number(usage.get("input_tokens")) or 0),
                    output_tokens=int(_number(usage.get("output_tokens")) or 0),
                    cost=cost,
                ),
                stop_reason=None,
            ),
            model=where.model,
            actor="transcription",
        )
    except Exception:  # noqa: BLE001 — recording never fails the transcription
        logger.exception("transcription usage not recorded")
    event = Usage(model=where.model, via_openrouter=where.via_openrouter, seconds=seconds, cost_usd=cost)
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001
            logger.exception("transcription usage listener failed")


def failure_line(reason: str) -> str:
    """What a voice note that could not be heard becomes in the turn."""
    return f"(A voice note could not be transcribed: {reason}. Ask them to type it.)"
