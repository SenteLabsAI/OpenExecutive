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
    limit = max_seconds()
    if duration_s is not None and duration_s > limit:
        raise TranscriptionError(f"it's longer than {_minutes(limit)}")
    if len(data) > MAX_BYTES:
        raise TranscriptionError(f"it's larger than {MAX_BYTES // (1024 * 1024)} MB")
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
