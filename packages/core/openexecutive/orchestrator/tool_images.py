"""A picture a tool hands the model along with its text answer.

Tool handlers answer with a string, so a tool that has a picture (the
browser's screenshot, ``browser.tools``) keeps it here and puts a
short marker at the start of its answer. The chat loop takes the marker off
before it does anything else with the answer (``take``), and puts the
picture beside the text in the tool result (``content``) when the turn's
model can see images; otherwise the model gets the text alone. A picture is
held only until its own turn's loop takes it: never saved, never in the
conversation's history, and the store keeps only the last 64 (a marker whose picture was dropped still comes off).
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
from collections import OrderedDict
from typing import Any

MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
# About 300 KB of base64: a screenshot at most 1280 pixels wide.
MAX_DATA_CHARS = 400_000
# Pictures waiting for their turn's loop, across every turn in this process.
_KEEP = 64

_held: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MARK = re.compile(r"\[\[image:([0-9a-f]{32})\]\]\n")


def valid(media_type: Any, data: Any) -> bool:
    """A picture the model can be shown: a JPEG or PNG in base64, not too big."""
    if media_type not in MEDIA_TYPES or not isinstance(data, str) or not data or len(data) > MAX_DATA_CHARS:
        return False
    try:
        head = base64.b64decode(data, validate=True)[:8]
    except (binascii.Error, ValueError):
        return False
    return head.startswith(b"\xff\xd8\xff") if media_type == "image/jpeg" else head.startswith(b"\x89PNG")


def attach(text: str, media_type: str, data: str, note: str) -> str:
    """``text`` with the picture held for the loop to put beside it.
    ``note`` is said with the picture when the model sees it."""
    token = secrets.token_hex(16)
    _held[token] = {"media_type": media_type, "data": data, "note": note}
    while len(_held) > _KEEP:
        _held.popitem(last=False)
    return f"[[image:{token}]]\n{text}"


def take(result: Any) -> tuple[Any, dict[str, Any] | None]:
    """The tool's answer without its marker, and the picture it held."""
    if not isinstance(result, str):
        return result, None
    # Anywhere in it: the content check may put a notice before it. A page
    # can't fake one (its text reaches here inside JSON, so never with a
    # bare newline, and a token is only good once).
    found = _MARK.search(result)
    if found is None:
        return result, None
    # A picture dropped to make room: the text still goes without its marker.
    return result[: found.start()] + result[found.end():], _held.pop(found.group(1), None)


def content(text: Any, image: dict[str, Any] | None, *, sees: bool) -> Any:
    """The tool result's content: the text, and the picture beside it when
    the model can see it."""
    if image is None or not sees or not isinstance(text, str):
        return text
    return [
        {"type": "text", "text": text},
        {"type": "text", "text": str(image.get("note") or "A picture from the tool above.")},
        {"type": "image", "source": {"type": "base64", "media_type": image["media_type"], "data": image["data"]}},
    ]


# Said in place of a picture taken out of an earlier round.
DROPPED = "(An earlier screenshot, left out: the newer one is what is on screen.)"


def drop_older(messages: list[dict[str, Any]]) -> None:
    """Take the pictures out of the tool results already in ``messages``
    (each in place, a short line instead), before a round adds newer ones."""
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            inner = block.get("content") if isinstance(block, dict) and block.get("type") == "tool_result" else None
            if isinstance(inner, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in inner):
                block["content"] = [
                    {"type": "text", "text": DROPPED} if isinstance(b, dict) and b.get("type") == "image" else b
                    for b in inner
                ]


def _reset_for_tests() -> None:
    _held.clear()
